# Bare-Metal Install — L0 Lab Kit

How a blank server becomes a fully registered Proxmox node with **one job run**
— and how to prove the whole loop in a lab with **no special hardware**.

## The moving parts

```
┌───────────────┐   identity POST (DMI serial, MACs)   ┌───────────────────┐
│ blank machine │ ───────────────────────────────────▶ │  answer service    │
│ boots prepared│ ◀─────────────────────────────────── │  (container beside │
│ auto-install  │        per-node answer.toml          │  nautobot-composer)│
│ artifact      │                                      │        │ ▲         │
└──────┬────────┘                                      │ lookup │ │ write-  │
       │ unattended install                            │ serial ▼ │ back    │
       │ ──▶ webhook ────────────────────────────────▶ │     Nautobot       │
       │     (state → bm_installed)                    │  (SoT: Device,     │
       │ ──▶ firstboot: pveum bootstrap ─────────────▶ │  Secrets, state)   │
       │     (per-node API token → SecretsGroup)       └───────────────────┘
```

- **Prepared artifact** ([scripts/prepare-install-iso.sh](../scripts/prepare-install-iso.sh)):
  the stock PVE ISO transformed once by `proxmox-auto-install-assistant` with
  the answer service's URL baked in. **One artifact serves the whole fleet** —
  node identity travels in the installer's POST, never in the image.
- **Answer service** ([bmc/answer_service/](../bmc/answer_service/)): the
  SoT-backed brain, a dedicated container in the nautobot-composer project.
  **Delivery-agnostic by construction** — nested VM, Redfish virtual media,
  and PXE all hit the same endpoints; it cannot tell them apart.
- **Install job** ([jobs/baremetal/install_node.py](../jobs/baremetal/install_node.py)):
  one input (the Device). Boots the installer via the delivery adapter named
  in the DeviceType's profile ([bmc/profiles/](../bmc/profiles/)), then
  watches the state machine.
- **Delivery adapters** ([jobs/lib/install_delivery.py](../jobs/lib/install_delivery.py)):
  the ONLY delivery-specific code. `pve-nested` (lab), `redfish-vmedia`
  (physical, decision #41 primary). PXE needs no adapter at all — it boots the
  same artifact from your lab's DHCP/boot server (secondary path, official
  since PVE 9.2 via `prepare-iso --pxe`).

## Security model

- **Serial allowlist**: only Devices with role Hypervisor and
  `provisioning_state=awaiting_install` get answers. Any other machine that
  boots the installer gets a 403 and installs nothing — which is also what
  makes a standing PXE boot service safe to run in the lab.
- **One-time keys**: the firstboot URL and its credentials phone-home key are
  minted per answer and consumed on use.
- **Optional shared bearer** (`--answer-auth-token` ↔ `ANSWER_AUTH_TOKEN`,
  PVE 9.2+) authenticates the answer request itself.
- The service holds the root password **hash** (never plaintext); per-node API
  tokens go straight into text-file Secrets; nothing secret is logged.

## One-time setup

1. **Answer service container** — copy
   [docker-compose.answer-service.example.yml](../bmc/answer_service/docker-compose.answer-service.example.yml)
   into your nautobot-composer project, set `NAUTOBOT_URL`/`NAUTOBOT_TOKEN`/
   `PUBLIC_URL`, and share the `node_secrets` volume with the Nautobot and
   worker containers (mounted at `/opt/nautobot/secrets`). Generate the TLS
   cert and root password hash into the shared volumes (from the compose
   project directory, service running):

   ```bash
   docker compose exec -T answer-service sh -c 'apt-get -qq update && apt-get -qq install -y openssl >/dev/null; openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=answer-service" -keyout /tls/answer-service.key -out /tls/answer-service.crt && openssl x509 -in /tls/answer-service.crt -outform der | sha256sum'
   ```

   Put the printed SHA256 in `ANSWER_CERT_FINGERPRINT` (compose `.env`) and
   restart the service. Then the root hash (generated on any Linux box):

   ```bash
   mkpasswd -m sha-512 | docker compose exec -T answer-service sh -c 'umask 077; cat > /secrets/root_password_hash'
   ```

2. **Prepared artifact** — on any PVE 9.x box (the lab NUC works):

   ```bash
   ./scripts/prepare-install-iso.sh --iso proxmox-ve_9.2-1.iso --url https://<svc>:8800/answer --fingerprint <sha256-from-step-1>
   ```

   (Plain `http://` also works and skips the cert steps — but the answer file
   and the firstboot token phone-home then transit in cleartext. Acceptable
   only on an isolated lab VLAN; say so out loud if you choose it.)

   Publish the output to the composer firmware server (plain-HTTP vhost if
   XCC1 will mount it — the ISO *mount* is plain HTTP on XCC1 while the answer
   fetch inside the installer stays HTTPS) and register it in Nautobot:
   SoftwareVersion under platform **proxmox-ve** (status Staged) +
   SoftwareImageFile with filename, SHA256, `download_url`. Promote to
   **Active** — installer images ride the same promotion gate as golden VM
   images.
3. **Bootstrap** — re-run `Bootstrap NFV Data Model` (adds the `Nested Lab
   Node` DeviceType and `proxmox-ve` platform).

## The nested lab loop (no hardware needed)

Create the SoT intent for a pseudo-server:

1. Device: DeviceType **Nested Lab Node**, role **Hypervisor**, any location,
   **serial set** (e.g. `NESTED-0001` — identity matching key),
   `provisioning_state=awaiting_install`, `software_version` = the Active
   prepared-ISO version, and CFs `vm_storage`/`vm_bridge`/`import_storage`
   for its future life as a "hypervisor".
2. **Hosted On**: relate it to the real lab host (the NUC) — that's the
   carrier the nested VM runs on.
3. Management IP: interface (e.g. `mgmt`) with the primary IPv4; a
   `DefaultGW`-role IP must exist in the prefix (contract §3). Pin the
   interface MAC if you want the answer's NIC filter exact.
4. Run **`Install Proxmox Node (SoT-driven)`**, tick Confirm.

What you should observe: the job creates a VM on the NUC with the Device's
SMBIOS serial and boots the prepared ISO → answer service logs `ANSWERED` →
unattended install (~10 min) → webhook flips `provisioning_state=bm_installed`
→ VM powers off (nested profile) → job detaches the ISO and boots from disk →
firstboot creates `svc-nfv@pve!deploy` with role NFVAutomation (granted to
BOTH user and token) and phones the token home → answer service writes the
text-file Secrets, creates SecretsGroup `<name>-proxmox`, and sets the
Device's `secrets_group` CF. **The node is now deployable by the existing VM
jobs with zero manual credential steps.**

## Physical servers (SE350 and beyond)

Same loop; only delivery differs. The Device needs an `xcc` interface with the
BMC IP (contract §4) and Secrets `xcc_username`/`xcc_password`. The
`redfish-vmedia` adapter auto-detects XCC1 (PATCH-on-EXT, plain-HTTP ISO) vs
XCC2 (standard InsertMedia), arms a one-shot CD boot, and powers on.
Remaining `[lab-verify]` on a real SE350: the vmedia write test + boot dress
rehearsal (already built into `SE350 Platform Discovery` as opt-in checks) and
the RAID volume's `ID_MODEL` string for the
[profile's disk filter](../bmc/profiles/thinksystem-se350.yaml).

Other vendors (iDRAC/iLO/Supermicro) = a new profile + at most a small vmedia
quirk in the client; the answer service and job don't change. Note every
vendor licenses remote vmedia (XCC Enterprise FoD is fleet-confirmed for us);
**PXE is the escape hatch for unlicensed BMCs** — same artifact, boot it from
the lab netboot server instead.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Installer sits at answer fetch | Answer service log (`docker compose logs answer-service`): `REFUSED` lines say exactly why (unknown serial, wrong state, missing DefaultGW, no profile) |
| `500 root password hash not provisioned` in the log | Step 1's `mkpasswd` command wasn't run — the hash file is per-request, the service starts without it |
| Install finished but state didn't flip | `docker compose logs answer-service` — webhook arrives before reboot/power-off; payload archived in `/data/install-<serial>.json` |
| No credentials after first boot | Node's journal: `journalctl -u proxmox-first-boot`; the phone-home retries for ~10 min, and its one-time key stays valid until success — but a consumed key needs a fresh install (by design) |
| Phone-home 403 `source does not match` | The node reached the service from an IP other than its SoT primary_ip4 (NAT?) — fix the record or set `VERIFY_PHONE_HOME_SOURCE=false` |
| Nested VM reinstalls in a loop | The nested profile must keep `reboot_mode: power-off` so the job can detach the ISO |
