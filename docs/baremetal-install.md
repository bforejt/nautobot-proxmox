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

- **Serial allowlist**: only Devices with role `NFV` and
  `provisioning_state=awaiting_install` get answers. Any other machine that
  boots the installer gets a 403 and installs nothing — which is also what
  makes a standing PXE boot service safe to run in the lab.
- **One-time keys**: the firstboot URL and its credentials phone-home key are
  minted per answer and consumed on use.
- **Optional shared bearer** (`--answer-auth-token` ↔ `ANSWER_AUTH_TOKEN`,
  PVE 9.2+) authenticates the answer request itself.
- The service holds the root password **hash** (never plaintext); per-node API
  tokens go straight into text-file Secrets; nothing secret is logged.
- **Fail closed on ambiguity** (decision #52): a serial matching several
  Devices, a static install without the mgmt MAC pinned (or with a MAC the
  installer did not report) and a broken profile key are refused at answer
  time; the firstboot phone-home checks the service cert's fingerprint on the
  same TLS connection that carries the token.
- **BMC identity**: before any BMC write (RAID layout, media mount, boot
  override, power) the jobs read the BMC's system serial and refuse unless it
  is the Device's serial — a stale `xcc` IP can never point an install at
  another machine.

## The three knobs: prepared media, answer discovery, boot delivery

Keep these distinct — conflating them is the most common way to misread the
process:

| Knob | What it does | Required? |
|---|---|---|
| **Prepare the installer media** — the [Media Forge job](#preparing-media-from-nautobot-the-media-forge) (preferred) or [the script](../scripts/prepare-install-iso.sh) (manual) | Turns the stock PVE ISO into the auto-installer and sets *how* the answer is fetched (`--fetch-from http`) | **Always.** A stock ISO never auto-installs; every proven path (nested, PXE, vmedia) used a prepared artifact |
| **Answer-URL discovery** | How the installer learns *where* the answer service is | A choice **within** the prepare step: **bake it** (`--url` + `--fingerprint` — the default, decision #11) so DHCP needs nothing; or prepare URL-less and publish **DHCP option 250/251** (or DNS TXT) — one endpoint-agnostic artifact, in exchange for a DHCP dependency |
| **Boot delivery** (vmedia / PXE / nested) | How a machine boots the artifact at all | Per-DeviceType profile. Only **PXE** involves DHCP (options 66/67 or the proxyDHCP sidecar) — and that DHCP layer has nothing to do with the answer file |

Terminology guardrail for network-team conversations: DHCP never carries an
answer *file* — at most the answer *URL*. The answer file itself is always
rendered **per node by the answer service at install time**, keyed on the
machine's serial, identically under every discovery and delivery combination.
(The upstream tool can also embed a static answer file in the ISO or on a USB
partition — per-node media, the opposite of this fleet design; unused here.)

## One-time setup

1. **Answer service container** — the supported deployment is
   **nautobot-composer's `answer-service` profile** (see composer's README):
   `./setup.sh` with the profile enabled generates the TLS keypair, seeds
   `ANSWER_CERT_FINGERPRINT` and `ANSWER_PUBLIC_URL`, and creates the node
   root-password hash — add `--enable-forge` on the lab/build instance to
   also enable and credential the media forge in the same run. Then
   `docker compose --profile answer-service up -d --build`.

   Running the service on some other stack is unsupported-but-possible:
   it is one container with documented requirements — every environment
   variable in [the service README](../bmc/answer_service/README.md), plus a
   secrets directory shared read-only into the Nautobot + worker containers
   at `/opt/nautobot/secrets`. (An overlay kit for arbitrary compose
   projects existed briefly but was removed untested — decision #45; the
   durable answer for non-composer portability is the planned native
   Nautobot App.)

2. **Bootstrap** — re-run `Bootstrap NFV Data Model` (creates the
   `proxmox-ve` platform, the promotion statuses, the standard Secret
   records, and the forge's integration records — everything the next step
   registers against).
3. **Prepared artifact** — preferred: with the forge enabled, run the
   **`Prepare Installer Media (Media Forge)`** job (see the media forge
   section below) — it prepares, publishes, and registers the Staged version
   in one run. Manual alternative (and the only path on non-forge
   instances) — on any PVE 9.x box (the lab NUC works):

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

## The nested lab loop (no hardware needed)

Create the SoT intent for a pseudo-server:

1. Device: DeviceType **Nested Lab Node**, role **NFV**, any location,
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
   Re-running it (reinstall) destroys the previous install VM — but only
   one tagged `l0-lab` (the job tags every install VM `nfv;l0-lab`) or at
   the Device's recorded `vmid`; a same-named VM without either, e.g. a VNF
   VM, is refused, never destroyed.

What you should observe: the job creates a VM on the NUC with the Device's
SMBIOS serial and boots the prepared ISO → answer service logs `ANSWERED` →
unattended install (~10 min) → webhook flips `provisioning_state=bm_installed`
→ VM powers off (nested profile) → job detaches the ISO and boots from disk →
firstboot creates `svc-nfv@pve!deploy` with role NFVAutomation (granted to
BOTH user and token) and phones the token home → answer service writes the
text-file Secrets, creates SecretsGroup `<name>-proxmox`, and sets the
Device's `secrets_group` CF. **The node is now deployable by the existing VM
jobs with zero manual credential steps.** On a reinstall the CF already
names `<name>-proxmox` from the previous install, so the job does not
trust it: it reports the token stored only once the phone-home has rewritten
both of that group's Secrets after the job started (their `last_updated`);
otherwise its result says `credentials=unverified (...)`.

## The PXE path (real hardware, no BMC needed)

Same loop, self-delivering: there is **no job run at all** — set the Device to
`awaiting_install` and power the machine on. Blank disks fall through to
netboot; the answer service does everything else. The serial allowlist is what
makes a *standing* boot service safe: unknown machines get the installer menu,
POST their identity, get a 403, and install nothing.

One-time boot-server setup (any Debian box on the target L2 — a lab Proxmox
host works; **no changes to the site's DHCP server**, dnsmasq answers only
PXE-requesting clients in proxy mode):

```bash
apt-get install -y dnsmasq ipxe
# snponly.efi drives the NIC through the UEFI firmware's own driver — use it
# for the chainload. Field-debugged: native-driver ipxe.efi stalled mid-
# download ("Connection timed out") on an Intel NUC's i219 during sustained
# transfers, while small fetches and TFTP worked; snponly is the standard fix.
mkdir -p /srv/tftp && cp /usr/lib/ipxe/snponly.efi /srv/tftp/
cat > /etc/dnsmasq.d/nfv-pxe.conf <<'EOF'
port=0
interface=vmbr0
bind-interfaces
dhcp-range=10.40.2.0,proxy,255.255.254.0     # your subnet
enable-tftp
tftp-root=/srv/tftp
dhcp-match=set:ipxe,175
pxe-service=tag:!ipxe,X86-64_EFI,"Chainload iPXE",snponly.efi
dhcp-boot=tag:ipxe,http://<composer>/images/pxe/boot.ipxe
pxe-prompt="NFV auto-install",3
EOF
systemctl restart dnsmasq
```

Artifacts: `prepare-install-iso.sh --pxe` emits `pxe/` (vmlinuz, initrd.img,
the prepared ISO, `boot.ipxe` with relative paths — publish the whole
directory, no URL editing needed). The iPXE menu defaults to the automated
target after 10 s.

**Serve the PXE payloads from the boot server itself** (e.g.
`/srv/pxe-http` behind a trivial HTTP server on the same box running
dnsmasq), not from a container port-forward on a workstation. Learned the
hard way: iPXE's minimal TCP stack fetched flakily (1-in-3) through Docker
Desktop's macOS port-forwarding proxy while every full OS fetched the same
URL perfectly — the DHCP/TFTP side looked healthy and only the HTTP hop
failed. A plain Linux HTTP server on wired L2 is the battle-tested iPXE
path. (The installer itself — a full Linux stack — fetches its answer from
the containerized answer service without issue; only the iPXE stage is
picky.)

```bash
mkdir -p /srv/pxe-http && cp pxe/* /srv/pxe-http/
# The generated boot.ipxe starts with a bare `dhcp` command — needed for
# Proxmox's USB/embedded flows, redundant AND harmful in a chainload (the
# NIC is already configured; re-opening it mid-script wedged the NUC's SNP
# stack: boot.ipxe fetched fine, then every later fetch timed out without
# a single packet reaching the server). Strip it for the netboot copy:
sed -i '/^dhcp$/d' /srv/pxe-http/boot.ipxe
# systemd unit: python3 -m http.server 8077 --directory /srv/pxe-http
# dnsmasq: dhcp-boot=tag:ipxe,http://<boot-server>:8077/boot.ipxe
```

Target machine: UEFI boot mode, Secure Boot off for the netboot (the
*installed* system is SB-signed regardless), ≥4 GB RAM (the whole installer
runs from RAM on PXE), one-time boot menu (F12) or netboot-first order. Serial
discovery trick: netboot the machine once *before* creating its Device — the
answer service's `REFUSED: unknown serial '...'` log line is the exact string
to put in the Device's serial field. After the install, the allowlist also
prevents reinstall loops: a machine that netboots again in `bm_installed`
state is refused and boots from disk.

## Physical servers (SE350 and beyond)

Same loop; only delivery differs. The Device needs an `xcc` interface with the
BMC IP (contract §4) and Secrets `xcc_username`/`xcc_password` (records
pre-created by the bootstrap; supply values via `./add-secret.sh` or
composer's `./setup.sh --nfv-secrets` — see getting-started §3). The
`redfish-vmedia` adapter PATCHes the first free `EXT{N}` member on any Lenovo
XCC (XCC1 and XCC2 both document that path; plain-HTTP ISO URLs on XCC1,
http/https/NFS/CIFS on XCC2) and falls back to a standard `InsertMedia` POST
only where no EXT members exist, arms a one-shot CD boot, and powers on.
Remaining `[lab-verify]` on a real SE350: the vmedia write test + boot dress
rehearsal (already built into `SE350 Platform Discovery` as opt-in checks —
**tester procedure: [se350-verification-checklist.md §1
runbook](se350-verification-checklist.md)**) and the RAID volume's `ID_MODEL`
string for the [profile's disk filter](../bmc/profiles/thinksystem-se350.yaml).

**SE455 V3 (XCC2)** — same adapter, second profile
([thinkedge-se455-v3.yaml](../bmc/profiles/thinkedge-se455-v3.yaml)). XCC2
exposes the same `EXT{N}` members and takes the same PATCH-on-member insert,
so the client's EXT-first branch runs unchanged; it additionally accepts
HTTPS/NFS/CIFS image URLs (`delivery.iso_url_schemes: [http, https]` in the
profile) and gates remote media behind the **XCC2 Platinum** license
(fleet-wide, decision #49 — the discovery job reads
`/redfish/v1/LicenseService/Licenses/XCC2_Platinum`). Storage is where the
automation grows: the unit's four SATA SSDs sit behind a ThinkSystem RAID
540-8i / 940-8i, and the two RAID1 virtual drives the admin used to create
in UEFI are now **created out-of-band by the install job** from the profile's
`storage` section (decision #50 — `Apply Storage Layout` does the same on its
own, dry-run first). Boot VD: ext4 + LVM-thin as everywhere (#27); data VD:
LVM-thin `DataDrive` at firstboot. See "Disk layout" below and
[research/se455-v3-platform-notes.md](research/se455-v3-platform-notes.md).

Other vendors (iDRAC/iLO/Supermicro) = a new profile + at most a small vmedia
quirk in the client; the answer service and job don't change. Note every
vendor licenses remote vmedia (XCC Enterprise FoD on the SE350 and XCC2
Platinum on the SE455 V3 are fleet-confirmed for us); **PXE is the escape
hatch for unlicensed BMCs** — same artifact, boot it from the lab netboot
server instead.

## The first real SE350 install (runbook)

Every mechanism below is individually field-proven (nested, PXE, and the
SE350 vmedia checks); this sequence just chains them. **The target's ~119G
boot volume is wiped**; the 1.92T data volume cannot be selected (filter
verified with the installer's own matcher).

Pre-flight, one-time in the lab:

1. **Answer service up in the lab composer** ([its README](../bmc/answer_service/)
   / composer's Answer Service section): cert generated, fingerprint in
   `.env`, root password hash written, profile in `COMPOSE_PROFILES`. Rebuild
   with `--build` — the default git build context fetches this repo's current
   `main` automatically (the install profiles bake into the image); if
   `ANSWER_SERVICE_BUILD_CONTEXT` points at a local checkout, pull that
   checkout first. Verify
   `curl -k https://<svc>:8800/healthz` from the node's subnet.
2. **Prepared ISO for THIS lab**: on any PVE box (the burn-in unit works),
   `prepare-install-iso.sh --iso <stock PVE ISO> --url https://<svc>:8800/answer
   --fingerprint <sha256>`; publish the output on the lab firmware server —
   the mount URL must be **plain HTTP** (XCC1).
3. **Nautobot**: re-run `Bootstrap NFV Data Model` (adds the `proxmox-ve`
   platform + Staged/Retired statuses), then register the prepared ISO as a
   SoftwareVersion (platform `proxmox-ve`, Staged → promote **Active**) +
   SoftwareImageFile whose `download_url` is the plain-HTTP mount URL.
4. **DHCP question**: the first install should run the DHCP path — confirm
   the mgmt VLAN offers DHCP during install. (Static needs `primary_ip4` +
   a `DefaultGW`-role IP in the prefix + the mgmt interface's MAC pinned so
   the NIC filter is exact — do that on later installs, with real layout
   data. The pinned MAC is **required** for static: without it the answer
   service refuses rather than guess a NIC, and the install job refuses
   before booting anything.)

Device records for the target unit:

- Device: DeviceType **ThinkSystem SE350**, role **NFV**, the unit's
  **DMI serial** (host-verification job reports it; the rehearsal unit's is
  `J101YCEB`), `provisioning_state=awaiting_install`, `software_version` =
  the Active prepared ISO, CFs `vm_bridge`/`vm_storage`/`import_storage` for
  its post-install life.
- Interface named **`xcc`** with the BMC IP assigned (contract §4) — the
  delivery adapter reads it. Secrets `xcc_username`/`xcc_password` as before.
  The BMC at that IP must report this Device's serial; the job checks it
  before its first BMC write and refuses on a mismatch.

Run **`Install Proxmox Node (SoT-driven)`** with Confirm ticked. Expected:
mount via PATCH-EXT → one-shot CD → ForceRestart → `ANSWERED` in the service
log → unattended install (**the ISO streams through the BMC NIC for the whole
install — allow 20–40 min**, slower than PXE/nested) → webhook flips
`bm_installed` → reboot to disk → firstboot creates the service account and
phones the token home → SecretsGroup set. The job ejects the spent installer
media the moment the webhook flips `bm_installed` (before the phone-home).
The node is then deployable by the VM jobs.

**Watch window.** After the boot step the job follows the state machine for
**75 min on `redfish-vmedia`** (30 min on `pve-nested`, which has already
waited for the installer's power-off) — long enough for the slow end of a
vmedia install. A unit that is slower still can raise it per profile with
`delivery.watch_timeout_seconds` (integer seconds, 300–6000 for vmedia,
300–3600 for nested; anything else is refused before the BMC is touched —
the caps keep the job inside its time limit). Running out of the window is
not an install failure: the job ends with `state machine incomplete within
the N-min watch window` and leaves the media mounted (the installer may
still be reading it) — see Troubleshooting.

## The first SE455 V3 install (runbook)

Same chain as the SE350 runbook; the differences are the RAID adapter and
the XCC2 checks. Pre-flight adds, on top of the SE350 list:

**Before anything else: rebuild the answer service.** Install profiles bake
into its image at build time, and the jobs arrive separately through the Git
sync — so a service built before this profile merged still refuses the node
with `403 ... no install profile for DeviceType 'ThinkEdge SE455 V3'` and the
installer aborts at the answer fetch (exactly what the first tester run hit
on 2026-09-16, after the storage step and the vmedia mount had already
succeeded). On the composer host:

```bash
docker compose --profile answer-service up -d --build answer-service
```

(`git pull` the checkout first if `ANSWER_SERVICE_BUILD_CONTEXT` points at a
local one.) Then confirm the profile is inside before booting anything:

```bash
docker exec answer-service ls /app/profiles
```

The install job now checks this itself: before it touches the BMC it reads
the service's `GET /info` through the `nfv-answer-service`
ExternalIntegration and refuses with `answer service at … has no install
profile 'thinkedge-se455-v3'` (or `does not support profile feature(s) …`)
when the image is stale. No integration or an unreachable service only logs
a warning — the node, not the worker, is what must reach the service.

1. **Discovery first, before any Device edits**: run `SE350 Platform Discovery`
   (it is generic — any Lenovo XCC) against the XCC2 IP with the host powered
   on. Read from its log: the **serial** (goes in the Device), **XCC2
   Platinum ENABLED**, **EXT members present**, and the **drive inventory** —
   four drives on the `RAID_Slot<n>` controller, the 480 GB pair and the
   1.92 TB pair, ideally all `Unconfigured good`. Drives shown as `JBOD` must
   be converted to Unconfigured Good once (XCC storage page or UEFI) — the
   layout step never converts drives itself. A drive in `Unconfigured bad`
   (failed or foreign) or whose Redfish state is not `Enabled` is never used
   for a new volume: replace it, or clear/import its foreign config, first. Drives already `Online` in
   admin-made volumes are fine: the step **adopts** a RAID1 over the two
   smallest drives as `boot` and one over the two largest as `DataDrive`
   whatever the adapter calls them (Lenovo defaults are `VD_0`/`VD_1`);
   other shapes (a RAID10 over all four, a lone RAID1 over the big pair
   with the small pair also in use) make it refuse.
2. **Device record**: DeviceType **ThinkEdge SE455 V3** (bootstrap-created),
   role NFV, the XCC-reported serial, `provisioning_state=awaiting_install`,
   `software_version` = the Active prepared ISO, CFs `vm_bridge`,
   **`vm_storage=DataDrive`** (the firstboot-created LVM-thin storage),
   `import_storage=local`; interface `xcc` with the BMC IP; and the
   management path with `primary_ip4` and the mgmt port's **MAC pinned** (no
   onboard NIC on this box — the NIC filter is the only thing naming the
   port). Either a plain `mgmt` interface carrying the IP and the MAC, or —
   the fleet model the Host Baseline job needs (contract §4c) — the bridge
   `vmbr0` carrying the IP over the LAG `bond1` whose member ports record
   their MACs, the install port flagged `primary_member`: the answer service
   derives the install NIC through that chain (decision #55). With interface
   name pinning every port that records its MAC gets its Nautobot name as its
   Linux name; the rest come up as `nic<N>`. For the serial console, set
   `host_baseline.serial_console.speed` in the config context (the profile
   declares `ttyS0`); without it firstboot logs and skips the console.
3. **Storage layout dry run**: `Apply Storage Layout (SoT-driven)` with the
   default dry run prints the plan (`create boot RAID1` over the two 480 GB
   drives, `create DataDrive RAID1` over the 1.92 TB pair) without touching
   the adapter. Untick dry run + Confirm to create them now, or let the
   install job do it as its first step — same code, same rules.
4. **Confirm the boot pin the first time**: the profile selects the boot VD
   as the adapter's first virtual drive (`ID_PATH: "*-scsi-0:2:0:0"`). Boot
   the prepared media once, switch to the tty3 root shell (`Ctrl+Alt+F3`) and
   run `proxmox-auto-install-assistant device-match disk
   ID_PATH='*-scsi-0:2:0:0'` — it must list exactly the ~480 GB volume. If the
   layout step warned that `boot` is not the first volume (hand-made units),
   fix the order (delete and re-create by hand, boot first): the install job
   **refuses** a unit whose boot volume is not the adapter's first VD,
   because the target-0 pin would then wipe the data volume.
5. Run **`Install Proxmox Node (SoT-driven)`** with Confirm. Expected: RAID
   layout ensured (host powered on into UEFI Setup if it was off) → EXT mount
   (XCC2 also takes https URLs) → one-shot CD → `ANSWERED` (source static, fs
   ext4) → install onto the boot VD → webhook → reboot → firstboot: service
   account, credentials phone-home, then **LVM-thin `big-vg/big-lv` on the
   data VD** + `pvesm add lvmthin DataDrive` (an existing volume group is
   reused on reinstall, and registered only if it holds the thin pool `big-lv`). Check `journalctl -u proxmox-first-boot` for the
   `data volume big-vg/big-lv created` / `PVE storage DataDrive registered`
   lines, then `pvesm status`.

## Host Baseline (after verification)

The install leaves a reachable, self-credentialed node; the
**`Host Baseline (SoT-driven)`** job turns it into a fleet node — the
tester's hand-run post-deploy steps, driven from Nautobot (decisions
#54/#55; design in [host-baseline.md](host-baseline.md), SoT contract in
[sot-data-contract.md §4c](sot-data-contract.md#4c-host-baseline-decisions-5455)).
Firstboot has already done the one-shot part from the same SoT: `lldpd` +
`snmpd` (+ `host_baseline.packages`), the serial console when the profile
declares a port and the config context its speed, the ZFS ARC limit, and
the subscription-nag hook when `host_baseline.remove_subscription_nag` is
true — `journalctl -u proxmox-first-boot` shows a line per step.

**SoT data to fill in first** (the job refuses, naming each missing fact):

1. **Bootstrap**: re-run `Bootstrap NFV Data Model` — it adds the interface
   custom fields `lag_mode`, `lag_xmit_hash`, `primary_member`, the Secret
   records `ad_bind_password` / `snmp_community` (and `snmpv3_<user>_auth` /
   `_priv` for every v3 user a config context names — re-run it after adding
   users), and the ConfigContextSchema `nfv-host-baseline`.
2. **Config context** `host_baseline` (fleet + site contexts; attach the
   `nfv-host-baseline` schema): `root_email`, `snmp` (contact, community
   Secret and/or v3 users), `ad` (realm, domain, servers, mode, base/bind DN,
   sync job name + schedule, admin group + role), `service_accounts`,
   `network` (`bond_miimon`, `lacp_rate`) — a section may say
   `enabled: false`. Complete example: contract §4c.
3. **Secrets values** (`./add-secret.sh <name>` on composer):
   `ad_bind_password`, the community, each `snmpv3_<user>_auth` / `_priv`,
   and the **root** login in `host_ssh_username` / `host_ssh_password`.
4. **Interfaces** on the Device: physical ports with their MACs; LAG
   interfaces `bond<N>` (`lag_mode`; `lag_xmit_hash` for `802.3ad` /
   `balance-xor`; `mtu`), each port's LAG field set; bridge interfaces
   `vmbr<N>` with the bond (or a port) as member via its Bridge field, the
   data bridge `mode: tagged-all`, `primary_ip4` on the management bridge; on
   a multi-port active-backup bond one member flagged `primary_member`.
5. **State**: `provisioning_state=bm_installed` (set by the install; set it by
   hand for a node built another way, e.g. the tester's hand-built unit).
6. **Composer**: the worker must write `secrets/nodes/` — pull a composer
   carrying nautobot-composer#66 and `./setup.sh` (it makes the directory
   group-writable), then recreate the worker. Without it the job refuses
   before touching the node (service accounts configured).

**Run**:

1. `Host Baseline (SoT-driven)` with **Dry run** ticked (the default): it
   checks the SoT, proves the node's identity (hostname + DMI serial), and
   reports per step what would change — the packages to install, a masked
   `snmpd.conf` diff, realm options that drift, tokens to create or rotate,
   ACLs to add or remove, and the `/etc/network/interfaces` diff. Nothing is
   written. Fix what it refuses or reports, re-run until it reads as you
   expect.
2. Untick Dry run, tick **Confirm**, run again. Steps apply in order —
   packages, SNMP, AD + root e-mail, service accounts — and the **network
   last**: the new file is staged, syntax-checked, the old one backed up, a
   rollback timer armed (`host_baseline.network.rollback_seconds`, default
   180 s), `ifreload -a` applied; the job reconnects on `primary_ip4`,
   cancels the timer and checks the bonds. LACP partner/aggregation state is
   reported as a warning only (the switch side is a separate work item). On
   success the Device moves to `provisioning_state=baseline_done`.
3. Tokens land in SecretsGroups `<node>-<name>` (e.g. `pve-se455-01-datadog`,
   Generic/Username = token id, Generic/Secret = value) — hand them to
   Datadog / PDM from there; their values are never in a log.

Re-run any time: it converges — a rotated community or bind password is
re-applied, an SNMPv3 user is re-created when its Secrets change, a token
whose Secrets went missing is rotated, extra ACL roles on the managed
accounts are removed. Run it as a dry run to report drift on an in-service
node (`fabric_done` and later states accept dry runs only).

## Preparing media from Nautobot (the media forge)

The **`Prepare Installer Media (Media Forge)`** job replaces the manual
prepare→copy→register chain with one run. The job is a thin trigger — the
**answer service does the work against its own identity** (decision #44): it
downloads the stock ISO (SHA256SUMS-verified, cached in its volume), runs
`proxmox-auto-install-assistant` with **its own** `PUBLIC_URL` and cert
fingerprint (never job inputs — a stale-URL/fingerprint artifact is
structurally impossible), publishes into the firmware storage, and registers
a **Staged** SoftwareVersion + ImageFile. The human promotion gate is
unchanged: promote Staged → **Active** in the lab, validate one install (the
install job refuses non-Active versions), and roll back by flipping the
previous version back if needed. Cert rotation therefore collapses to:
rotate cert → run this job → promote in the lab → validate.

Setup (once):

1. **Enable the forge on the lab/build instance only** — it ships
   **disabled** (composer: `ANSWER_ADMIN_ENABLED=false`, the service-internal
   name is `ADMIN_ENABLED` — see the service README for the mapping; the
   `/admin/*` surface answers 404 while off). The forge publishes into the
   composer **firmware server's** storage — enable that profile too
   (`--with-firmware`), or point the publish dir/base URL at your own server.
   Composer: **`./setup.sh --enable-forge`** does it all (generates the
   admin token once, mirrors it into the secrets file for the job, defaults
   the publish dir and base URL), then
   `docker compose --profile answer-service up -d --build`. Manual
   equivalent: the forge variables in
   [the service README's media-forge table](../bmc/answer_service/README.md)
   (`ANSWER_`-prefixed in composer's `env.example`). Field-deployed
   instances keep the default: they serve installs, nothing else.
2. **Integration records** — created by `Bootstrap NFV Data Model`
   (re-run it after updating): the `nfv-answer-service` ExternalIntegration
   (remote URL seeded `https://answer-service:8800`, the compose-network
   address — edit it if your layout differs, bootstrap never overwrites),
   its Secrets Group, and the token Secret *record*. The token **value**:
   if step 1 used `--enable-forge`, it is **already in
   `secrets/answer_service_admin_token`** — do NOT overwrite it; just verify
   with the Secret's "Check Secret" button. Only on a manually-configured
   forge do you write it yourself (`./add-secret.sh
   answer_service_admin_token`, same bearer as `ANSWER_ADMIN_TOKEN`).

Then run the job: release (e.g. `9.2-1`), optional PXE artifact set,
optional version override. Registration **fail-closes on an existing
SoftwareVersion** — a version already in devices' intent is never silently
re-pointed at a new artifact; re-run with an explicit new version instead.
Failure modes are precise: forge disabled → the job says which instance to
enable; bad bearer → check the integration's Secrets Group; version exists →
override. The manual script remains fully supported (and is what non-forge
environments use).

## DHCP options reference

**Default posture: none.** The shipped design needs no options on the site's
DHCP server — PXE bootstrap comes from the proxyDHCP sidecar (above), and the
answer-service URL + cert fingerprint are baked into the prepared artifact
(`prepare-iso --url --fingerprint`). This section exists for labs that
*prefer* configuring their real DHCP server instead of running the proxy.

**Layer 1 — PXE bootstrap** (replaces the proxyDHCP sidecar; a TFTP server
for the ~100 KB chainload binary is still required):

| Option | Value | Condition |
|---|---|---|
| 66 (next-server) | TFTP server IP | UEFI x64 PXE clients (option 93 client-arch = `0x0007` or `0x0009`) |
| 67 (bootfile) | `snponly.efi` | same clients, **except** iPXE |
| 67 (bootfile) | `http://<boot-server>:8077/boot.ipxe` | clients with user class `iPXE` (option 77) — breaks the chainload loop by handing the loaded iPXE its script URL |

ISC dhcpd sketch:

```
if exists user-class and option user-class = "iPXE" {
    filename "http://<boot-server>:8077/boot.ipxe";
} elsif option arch = 00:07 or option arch = 00:09 {
    next-server <tftp-server>;
    filename "snponly.efi";
}
```

### Windows Server DHCP, step by step

The branching is done with **DHCP policies** (Server 2012+). Everything below
is scope-level on the install VLAN's scope — smallest blast radius; nothing
touches other scopes. PowerShell (elevated, on the DHCP server; substitute
the scope, TFTP IP, and boot-server URL):

```powershell
# 1. Classes the conditions match on. iPXE identifies itself with user class
#    "iPXE"; UEFI x64 PXE ROMs send vendor class PXEClient:Arch:00007 (some
#    firmware: 00009) followed by a variable UNDI suffix — hence prefix match.
Add-DhcpServerv4Class -Name "iPXE"                -Type User   -Data "iPXE"
Add-DhcpServerv4Class -Name "PXEClient-UEFI-x64"  -Type Vendor -Data "PXEClient:Arch:00007"
Add-DhcpServerv4Class -Name "PXEClient-UEFI-x64b" -Type Vendor -Data "PXEClient:Arch:00009"

# 2. Policy 1 (must process FIRST): loaded iPXE gets its script URL. An iPXE
#    client ALSO matches the vendor-class policy below — processing order is
#    what guarantees it gets the URL, not the chainloader again (loop).
Add-DhcpServerv4Policy -Name "NFV-iPXE" -ScopeId 10.96.112.0 `
  -Condition OR -UserClass EQ,"iPXE" -ProcessingOrder 1
Set-DhcpServerv4OptionValue -ScopeId 10.96.112.0 -PolicyName "NFV-iPXE" `
  -OptionId 67 -Value "http://<boot-server>:8077/boot.ipxe"

# 3. Policy 2: plain UEFI x64 PXE ROMs chainload snponly.efi over TFTP.
#    Trailing * = prefix match against the variable UNDI suffix.
Add-DhcpServerv4Policy -Name "NFV-PXE-UEFI64" -ScopeId 10.96.112.0 `
  -Condition OR -VendorClass EQ,"PXEClient:Arch:00007*",EQ,"PXEClient:Arch:00009*" `
  -ProcessingOrder 2
Set-DhcpServerv4OptionValue -ScopeId 10.96.112.0 -PolicyName "NFV-PXE-UEFI64" `
  -OptionId 66 -Value "<tftp-server-ip>"
Set-DhcpServerv4OptionValue -ScopeId 10.96.112.0 -PolicyName "NFV-PXE-UEFI64" `
  -OptionId 67 -Value "snponly.efi"

# Verify
Get-DhcpServerv4Policy -ScopeId 10.96.112.0
Get-DhcpServerv4OptionValue -ScopeId 10.96.112.0 -PolicyName "NFV-iPXE","NFV-PXE-UEFI64"
```

GUI equivalent: DHCP console → IPv4 → *define the classes* under **User
Classes** / **Vendor Classes** (right-click IPv4) with the exact ASCII values
above → the scope → **Policies** → New Policy → condition *User Class equals
iPXE* (policy 1) / *Vendor Class equals PXEClient-UEFI-x64 with "Append
wildcard(*)" checked, OR'd with the 00009 class* (policy 2) → on the options
page set 067 (and 066 for policy 2) → order the iPXE policy above the vendor
policy.

Windows-specific cautions:

- **Do NOT set option 60 (`PXEClient`)** on the scope — that's only for
  WDS-on-the-DHCP-host setups and makes clients solicit boot service from
  the DHCP server itself.
- Keep `snponly.efi`, not `ipxe.efi` (the field lesson above), and no
  scope-level 66/67 — boot options must exist **only inside the policies**,
  or every DHCP client on the VLAN sees them.
- Machines that are *not* in the SoT still chainload and reach the installer
  menu; the answer-service serial allowlist is what keeps that harmless
  (they 403 and install nothing).

**Routed segments / `ip helper-address` labs.** Only the client's DISCOVER
broadcast cares about L2 adjacency — TFTP, the HTTP payloads, and the answer
fetch are all unicast and route normally. Decision guide:

- proxyDHCP box **on the install VLAN**: works unchanged — helpers relay the
  lease request to central DHCP while the on-link proxy answers boot info
  from the same broadcast. No helper changes.
- proxyDHCP box **on another subnet**: possible by adding a second
  `ip helper-address` pointing at it (dnsmasq serves relayed proxy requests,
  inferring the subnet from `giaddr`), but this is the least reliable
  variant — PXE firmware handling of *relayed* proxy offers varies. Avoid
  for anything you depend on.
- **You run the central DHCP server** (which a helper-based lab does): skip
  proxyDHCP entirely and configure the options above on it. The proxy
  exists for networks you *don't* control; when you do, real options are
  simpler and firmware-proof.

**Layer 2 — auto-installer answer discovery** (only if you deliberately stop
baking the URL into the artifact; verified against the installer source):

| Option | Name the installer defines | Value |
|---|---|---|
| 250 (text) | `proxmox-auto-installer-manifest-url` | the answer URL, e.g. `https://<svc>:8800/answer` |
| 251 (text) | `proxmox-auto-installer-cert-fingerprint` | SHA256 of the answer service's TLS cert |

DNS alternative to both: TXT records `proxmox-auto-installer.<search-domain>`
/ `proxmox-auto-installer-cert-fingerprint.<search-domain>` (the search domain
must come via DHCP). Trade-off to state before choosing this over baking: a
DHCP/DNS-discovered URL makes one prepared artifact fully endpoint-agnostic,
but ties installs to DHCP infrastructure the field design deliberately avoids
depending on (decision #11 pairs baked-URL with the vmedia/field path, option
250 with PXE/lab setups).

## Disk layout of an installed node

The `ext4` profile (fleet standard, decision #27) produces this layout —
verified empirically on a node installed by this loop (values from a 32 GiB
lab disk; proportions scale with disk size):

| Piece | What it is |
|---|---|
| GPT + BIOS-boot + **ESP** (~1 GiB, vfat) | Boot partitions; `/boot/efi` |
| Rest of the disk → **LVM PV**, VG **`pve`** | Everything else, three LVs |
| **`root`** LV — ext4, `/` | The OS **and** the `local` directory storage (`iso`, `import`, `vztmpl`, `backup`) — the contract's `import_storage=local`. Observed 13.5G of 32G |
| **`swap`** LV | ≈ RAM size, capped at 8 GiB on larger disks (observed ~4G) |
| **`data`** LV — **LVM-thin pool** | The remainder; surfaces as the `local-lvm` storage for VM disks — the contract's `vm_storage=local-lvm`. Observed 11.8G |

Consequences worth knowing:

- **A fresh node needs zero post-install storage work**: `local` and
  `local-lvm` exist with the right content types, so the hypervisor Device's
  `vm_storage`/`import_storage` CFs match out of the box and the VM deploy
  jobs can target it immediately.
- The **whole selected disk is wiped** — the disk filter in the profile is the
  only thing standing between the installer and a data disk, which is why the
  SE350 profile pins the RAID volume's model string and single-disk boxes pin
  `DEVNAME`.
- Splits above are the installer's **auto-sizing**. For deterministic sizes,
  declare them in the profile's `install.lvm` block (`hdsize`, `swapsize`,
  `maxroot`, `maxvz`, `minfree` — GiB); they render into the answer file and
  every node of that DeviceType gets the identical layout. That is the
  SoT-honest mechanism: layout policy lives in the profile, not in anyone's
  head. Rough expectation for a 500 GB NVMe at defaults: 1G ESP, 8G swap,
  ~100G root, ~380G thin pool.
- ZFS/Btrfs profiles use the same mechanism with their own option families
  (`install.zfs` / `install.btrfs` — e.g. `zfs: {raid: raid1, ashift: 12}`);
  the fleet standard stays ext4 + LVM-thin where the hardware presents a
  single RAID volume (SE350, decision #27).
- `install.filter_match` (`any`, the installer default, or `all`) renders the
  answer file's `filter-match` for profiles that combine several filter keys.
- `install.interface_name_pinning: true` (decision #51; PVE ≥ 9.1 answer
  format) makes the installer pin every physical NIC's name by MAC at install
  time — `nic<N>` by enumeration order, exactly like the hand-built units —
  and the answer service adds a **mapping from the SoT**: a Device interface
  that records its MAC gets its Nautobot name as the Linux name (`mgmt` stays
  `mgmt` after every upgrade), the rest keep `nic<N>`. Only MACs the installer
  actually reported are mapped. Nautobot names that break the Linux rule are
  **transliterated** deterministically (decision #52): lowercase; each run of
  characters outside `[a-z0-9_]` becomes `_`; leading/trailing `_` stripped;
  `p_` prefixed unless the result starts with a letter; truncated to 15 —
  `OCP-1` → `ocp_1`, `1GbE-4` → `p_1gbe_4` (logged as `'OCP-1' -> 'ocp_1'`).
  A result shorter than 2 chars, in the installer's `nic<N>` namespace, or
  already taken (first interface wins) is skipped with a log line and the
  port keeps `nic<N>`. The `xcc` interface is never a host NIC. The webhook log line
  `INSTALLED <node>: interfaces …` records the final name-to-MAC map.

### RAID-adapter platforms: two hardware mirrors (SE455 V3, decision #50)

The SE455 V3's four SATA SSDs (2 × 480 GB, 2 × 1.92 TB) sit behind a RAID
540-8i / 940-8i. The profile's top-level `storage` section is the layout the
install job (or `Apply Storage Layout`) makes the XCC create over Redfish
before the installer boots — "boot is always the smaller pair":

```yaml
storage:
  controller: "RAID_*"        # Storage member Id glob (Lenovo: RAID_Slot<n>)
  volumes:                    # creation order = VD target order
    - {name: boot,      raid: RAID1, select: smallest, count: 2}
    - {name: DataDrive, raid: RAID1, select: largest,  count: 2}
```

| Piece | What it is |
|---|---|
| **`boot`** — RAID1 over the two smallest unconfigured drives, created first | The adapter's first VD (SCSI target 0 → `ID_PATH *-scsi-0:2:0:0`, the profile's boot filter). The installer lays ext4 + LVM-thin on it exactly as on the SE350: `local` (iso/import/backup) and `local-lvm` |
| **`DataDrive`** — RAID1 over the two largest unconfigured drives | The data VD. The firstboot hook (`install.data_volume`) makes it VG `big-vg` with thin pool `big-lv` and registers the lvmthin storage **`DataDrive`** (images, rootdir) — the contract's `vm_storage=DataDrive` |

Rules the layout step enforces: the picked drives must be equal-sized and the
pick unambiguous (a third drive of the same size refuses); volumes that exist
by name are kept after a RAID-type check, never re-created; nothing is ever
deleted; JBOD drives are reported, not converted; a drive whose capacity the
XCC has not reported yet is never sized as 0 — the pick refuses until it is.
Adoption by role weighs every unclaimed hand-made volume at once (the listing order never decides
which mirror is `boot`) and refuses equal-sized candidates. The XCC reports RAID
inventory only while the host is powered on, so the step powers it on with a
one-time boot into UEFI Setup and waits for the adapter to enumerate. When
`boot` is not the adapter's first volume (hand-made units) the install job
**refuses** — the ID_PATH pin assumes target 0 and would wipe the data
volume — and `Apply Storage Layout` warns that it will. `install.data_volume` keys: `vg`, `thinpool`,
`pve_storage`, `select: unused-largest`, `min_size_gib`; a volume group of
that name found on disk (reinstall) is reused, so VM disks survive.

### JBOD platforms: ZFS mirrors (`install.data_pool`)

Boxes that present raw disks (onboard SATA/NVMe, an HBA, or an adapter in
JBOD mode) use the installer's own mirroring instead: `filesystem: zfs`,
`zfs.raid: raid1` and a `disk_filter` selecting exactly the boot pair (e.g. a
capacity token in the model string, `ID_MODEL: "*480*"`) build `rpool`; the
firstboot hook's `install.data_pool` (`name`, `pve_storage`, `raid: mirror`,
`select: unused-largest`, `count`, `min_size_gib`) builds the data mirror over
the largest unused signature-free pair and registers it as a zfspool storage,
importing a same-named pool on reinstall. The hook runs **after** the
credentials phone-home, so a storage problem can never cost the node its
token. The host-verification job's "§4 data-pool preflight" / "§4 data-volume
preflight" evaluate the same rules from the host side: they PASS when a
pool / volume group of the profile's name already sits on the non-boot disks
(firstboot imports / reuses it), and otherwise FAIL unless the largest
remaining non-removable disk(s) are unused and signature-free — no
partitions or holders, no filesystem / `zfs_member` / `LVM2_member` signature,
no partition table — so a layout firstboot would skip is caught before the
install.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Installer sits at answer fetch | Answer service log (`docker compose logs answer-service`): `REFUSED` lines say exactly why (unknown serial, wrong state, missing DefaultGW, no profile). **No `POST /answer` line at all** = the machine never reached the service (wrong media/URL, network, or the service host asleep/down) — nothing to fix in Nautobot |
| Install job refuses: `answer service at … has no install profile '…'` / `does not support profile feature(s) …` | The preflight caught the stale-image case before booting: rebuild the answer service from the current main (`docker compose --profile answer-service up -d --build answer-service`) and re-run. A `did not answer GET /info` *warning* instead means the worker cannot reach the service; the install proceeds — check the node's own reachability if the fetch then fails |
| Installer: `Fetching answer file via HTTP failed: http error: 403 Forbidden: {"detail":"no install profile for DeviceType '...'"}` | The answer service image predates the DeviceType's profile (profiles bake in at build; the jobs update independently via Git sync). Rebuild it from the current main — `docker compose --profile answer-service up -d --build answer-service` — verify with `docker exec answer-service ls /app/profiles`, then re-run the install job: the storage step adopts the volumes it already made and the media is re-mounted |
| Installer: `filter did not match any device` / `... any devices` | The answer was issued, but its NIC filter (`ID_NET_NAME_MAC` from the pinned mgmt MAC) or the profile's disk filter matched nothing on this box. From the installer shell: `proxmox-auto-install-assistant device-info -t disk` / `-t network`, then `device-match disk KEY='glob'` until it lists exactly the intended disk(s); fix the profile (or the pinned MAC) and rebuild the answer service |
| Need a shell on the installer | Every mode runs a root shell on **tty3** (`Ctrl+Alt+F3`; tty2 = installer stderr). A failed automated install drops to a debug shell on tty1 (our answers set `reboot-on-error = false`). To pause *before* anything runs, add `proxmox-debug` to the kernel line (press `e` in GRUB on the automated entry, or use the `debug` iPXE entry). Logs: `/tmp/fetch_answer.log`, `/tmp/auto_installer.log`, `/tmp/install-low-level.log` |
| `Storage layout refused: ... JBOD` / `only N free` | The RAID adapter's drives are not `Unconfigured good` (JBOD, hot spare, or already in a volume of the wrong shape). Convert JBOD drives once in the XCC storage page or UEFI; a volume the step cannot adopt (wrong RAID level or drive set) must be deleted by hand — the step never deletes |
| `Storage layout refused: ... drive(s) are bad or disabled and never used: [...]` (after `only N free` or `free drives differ in size`) | A drive the plan needs is `Unconfigured bad` (failed, or carries a foreign config) or its Redfish `Status.State` is not `Enabled`; the step never builds a volume on it. Replace the drive, or clear/import the foreign config in the XCC storage page or UEFI so it reads `Unconfigured good`, then re-run |
| `Storage layout refused: volume '…': free drive(s) [...] report no capacity (adapter still enumerating?) — capacity unknown, refusing to pick drives; re-run in a minute` (or `free or adoptable drive(s)`) | The XCC listed the drives but has not reported their `CapacityBytes` yet — typical right after the step powered the host on. Sizing them as 0 would make large drives the "smallest" pair and mirror them as `boot`, so nothing was created. Wait a minute and re-run (the host is now on, so the step reads the inventory directly). If it persists, check the drives on the XCC storage page: a drive that never reports a size must be reseated or replaced |
| `DataDrive` storage missing on a hand-built unit | Its data VD already carries an LVM signature: firstboot creates nothing on a signed disk and registers storage only when **both** names match the profile — VG `big-vg` **and** thin pool `big-lv` (`big-vg/big-lv`). A differently named VG is not touched (`vgrename` it, then see the next row if its pool is not `data`), or wipe the VD (`wipefs -a`, data loss) before installing |
| Firstboot log `data volume big-vg: volume group present but thin pool big-vg/big-lv missing — PVE storage DataDrive NOT registered; ...` | The reused VG `big-vg` has no thin pool `big-lv`: a hand-built unit whose pool has another name, or a reinstall after a fresh unit's `lvcreate` failed (the empty VG is then reused on every install). Firstboot never creates or renames anything on a reused VG. On the node: `lvs big-vg`; rename an existing pool (`lvrename big-vg <pool> big-lv`) or create one (`lvcreate -l 98%FREE --thinpool big-lv big-vg`), then `pvesm add lvmthin DataDrive --vgname big-vg --thinpool big-lv --content images,rootdir`. Or wipe the VG (data loss) and reinstall |
| Install job: `Storage layout refused: the boot volume is not the adapter's first virtual drive` (Apply Storage Layout: `boot volume … is not the adapter's first VD` + `Install Proxmox Node will REFUSE this unit`) | The volumes were created by hand in the other order; the profile's `ID_PATH *-scsi-0:2:0:0` pin would select the data VD and the installer would wipe it. Back up anything on the data VD, delete both volumes by hand (XCC storage page or UEFI — the step never deletes), and re-run: the step re-creates `boot` first |
| `Storage layout refused: volume 'boot': existing volume … would be adopted, but other drives share its smallest size … — ambiguous role, refusing` | Hand-made mirrors over equal-sized drives: nothing says which one is `boot`. Rename the intended boot VD to `boot` (matched by name first) or delete and let the step create them |
| `BMC identity check refused: the BMC at … belongs to serial '…', but <node>'s serial is '…'` / `reports no system serial` / `could not read the system serial` | The Device's `xcc` IP leads to another machine (or the Device's serial is wrong): fix the record — the discovery job reports the BMC's serial. A BMC that reports no serial, or cannot be read, is refused too; nothing was written to it |
| Install job: `… installs static (primary_ip4 …) but the interface carrying it has no MAC address` / log `REFUSED: … installs static … has no MAC — pin the mgmt interface MAC` (installer: `409 static install needs the mgmt interface MAC pinned`) | Static installs need the mgmt port's MAC on the Nautobot interface that carries `primary_ip4` — the service never guesses a NIC (the installer's NIC list carries names, not link state). Record the MAC (installer shell: `proxmox-auto-install-assistant device-info -t network`), or drop `primary_ip4` for a DHCP install |
| Log `REFUSED: … pinned mgmt MAC … is not among the NICs the installer reported (…)` (installer: `409 pinned mgmt MAC is not present on this machine`) | The MAC on the mgmt interface is a typo or belongs to another unit/card; the log lists every MAC this machine reported — correct the interface's MAC and re-run |
| Log `REFUSED: serial '…' matches N Devices` (installer: `409 serial matches more than one Device`) | Two Devices carry the same serial (a copied record?). Serials must be unique — fix the duplicate |
| Install job: `Device name '…' is not a valid hostname label` / log `REFUSED: Device name '…' (serial …) is not a valid hostname label` (installer: `409 device name is not a valid hostname label`) | The Device name becomes the node's hostname (`<name>.<DOMAIN>` in the answer's `fqdn`), so it must be one DNS label: letters, digits and hyphens, 1-63 chars, no leading/trailing hyphen, not all digits (no spaces, dots or underscores). Rename the Device and re-run — nothing was booted or rendered |
| Install job / Apply Storage Layout: `<node> has role '…', not 'NFV' — refusing to boot an installer` (`… refusing to touch its RAID adapter`) | The job was given a Device without the `NFV` role — usually an API submission (the form's dropdown only lists NFV-role Devices, but the REST API accepts any Device pk). The role is checked first, so nothing was read from or done to the BMC. If the Device really is an install target, assign it the `NFV` role and re-run; otherwise fix the submitted pk (a production host with a stray `awaiting_install` state would otherwise have been reset into the installer) |
| Installer: `500 profile install.… must be …` at answer fetch | The DeviceType profile's `data_pool` / `data_volume` / `filter_match` is invalid; it is now checked before the installer runs (it used to fail at firstboot). Fix the profile and rebuild the answer service |
| `DataDrive` storage missing after an SE455 V3 install | `journalctl -u proxmox-first-boot` on the node: the data-volume step logs why it refused (no unused signature-free disk at the largest size, LVM error, or a reused volume group without the thin pool `big-lv` — see the `thin pool big-vg/big-lv missing` row). A reused volume group from a previous install is expected and logged |
| Installer fails with `duplicate interface name mapping` or `interface name ... is invalid` | The pinning mapping rendered from Nautobot clashed (two interfaces with the same name, or a name the installer's `pve-iface` rule rejects). The answer service transliterates names to the Linux rule and skips what still clashes with a log line before rendering; if the installer still complains, check the `ANSWERED ... names=` log line against the Device's interfaces |
| A port came up as `nic<N>` although its Nautobot interface records the MAC | Answer-service log: `pin name … is already used` (two Nautobot names transliterate to the same Linux name — first wins), `squats the installer's default nic<N> namespace`, or `is not a valid Linux/pve-iface name` (shorter than 2 chars after transliteration). Rename the interface in Nautobot |
| Host Verification: `FAIL: §4 data-pool preflight — data pool '…': the largest remaining disk(s) are not unused and signature-free — /dev/… carries …` (or `§4 data-volume preflight — data volume '…': …`) | The intended data disk(s) carry partitions/holders, a filesystem / `zfs_member` / `LVM2_member` signature or a partition table — firstboot would skip the data step and the node would come up without its `DataDrive` storage. The job lists what each disk carries (and logs every disk's `signature=` / `partition-table=` in the inventory). A pool or VG of the **profile's** name is fine (imported / reused, the check PASSes with `already exists on …`): a differently named one is not — `zpool export` + `zpool import <old> datastore`, or `vgrename <old> big-vg` (then see the thin-pool row above). Otherwise wipe the disk (`wipefs -a`, plus `sgdisk --zap-all` for a partition table — data loss) only if it is truly spare. An `LVM2_member` disk whose VG is not shown may be an inactive VG on a non-root login: `pvs` on the node names it |
| Host Verification: `FAIL: §5 DMI serial vs SoT — could not read DMI product_serial` | Almost always a non-root login: `/sys/class/dmi/id/product_serial` is readable by root only, and the job never invokes sudo, so a sudo-capable user still fails here. Point the `host_ssh_username` / `host_ssh_password` Secrets at the node's **root** login (Proxmox VE permits root SSH by default). If it fails as root, the firmware exposes no serial — fix that in the BMC/UEFI before installing, since the installer POSTs this value to the answer service |
| `datastore` pool missing after a JBOD (ZFS) install | `journalctl -u proxmox-first-boot` on the node: the data-pool step logs why it refused (fewer/more than `count` equal-sized unused disks, or leftover signatures — `wipefs -a` the intended data disks by hand only if they are truly spare, then `zpool create` + `pvesm add zfspool` per the profile) |
| `500 root password hash not provisioned` in the log | `secrets/root_password_hash` missing/empty — composer's `./setup.sh` generates it when the answer-service profile is enabled (re-run it), or create manually: `openssl passwd -6 > secrets/root_password_hash` |
| Install job result: `installer delivered; state machine incomplete within the N-min watch window (webhook=…, credentials=…)` | The job stopped *watching*; the install itself carries on. `webhook=pending`: check the answer-service log for the `ANSWERED` line and the node's console — a vmedia install that is merely slow will still flip `bm_installed` on its own; re-check the Device's `provisioning_state` later. If the unit routinely needs more than the window, set `delivery.watch_timeout_seconds` in its profile. `credentials=pending` only: see "No credentials after first boot" below |
| Install job result: `... credentials=unverified (pre-existing SecretsGroup '<name>-proxmox' not rewritten by this install's phone-home — it may still hold the previous install's token)` | A reinstall: the Device's `secrets_group` CF already named the group when the job started (the job logs `... already names SecretsGroup ... (reinstall)`), and within the watch window the phone-home did not rewrite both of its Secrets (username + secret `last_updated` at/after the job start). Until it does, the Secrets may point at the previous install's token, which died with the old OS — deploys against the node would get 401. Check the answer-service log for `OVERWRITING stored credentials` followed by `CREDENTIALS STORED` for the node (a `REFUSED credentials` line means the phone-home source did not match the mgmt IP), and the node's `journalctl -u proxmox-first-boot`; see "No credentials after first boot". If the log does show `CREDENTIALS STORED` after the job start, the store succeeded and the result is conservative — e.g. clock skew between the Nautobot worker and web containers |
| Job log *warning*: `Installer media left mounted on … — the webhook did not land within the N-min watch window` / `Could not eject installer media from …` | The vmedia ISO is still inserted on that `EXT{N}` slot (left on purpose when the webhook had not landed — the installer may still be reading it). Once the node is installed, eject it from the XCC UI or with a discovery-job write-test run, so stale mounts do not fill the EXT slots |
| Install job refuses: `profile delivery.watch_timeout_seconds must be an integer between 300 and … for …` | The DeviceType profile's watch-window override is not an integer in range (6000 s cap for `redfish-vmedia`, 3600 s for `pve-nested` — the caps keep the job inside its time limit). Fix or drop the key; nothing was booted |
| Install finished but state didn't flip | `docker compose logs answer-service` — webhook arrives before reboot/power-off; payload archived in `/data/install-<serial>.json` |
| No credentials after first boot | Node's journal: `journalctl -u proxmox-first-boot`; the phone-home retries for ~10 min, and its one-time key stays valid until success — but a consumed key needs a fresh install (by design) |
| Phone-home 403 `source does not match` | The node reached the service from an IP other than its SoT primary_ip4 (NAT?) — fix the record or set `VERIFY_PHONE_HOME_SOURCE=false` |
| Webhook never arrived but node installed fine | Observed once on the PXE path (real NUC). The credentials phone-home also advances the state (firstboot = proof of install), so the loop self-heals; the log says `state advanced ... via credentials phone-home`. Exact webhook loss cause `[lab-verify]` |
| Nested VM reinstalls in a loop | The nested profile must keep `reboot_mode: power-off` so the job can detach the ISO |
| Install job (nested) refuses: `VM <vmid> on <carrier> is named <node> but is not this job's install VM (no 'l0-lab' tag, and the Device's vmid custom field is …) — refusing to destroy it` | A reinstall destroys a same-named VM on the carrier only when it is provably the job's own install VM: tagged `l0-lab` (the job sets `nfv;l0-lab` on every install VM) or at the vmid recorded in the Device's `vmid` CF. This VM is neither — typically a VNF VM (tagged `nfv;sot-driven`) whose Device shares the node's name. Nothing was stopped or destroyed. Rename the NFV Device (or the VM), or use another carrier. If it really is this node's old install VM (tag removed by hand), set the Device's `vmid` CF to its vmid, or re-add the `l0-lab` tag, and re-run |
| Install job (nested) refuses: `VMID <vmid> on <carrier> belongs to '<other>', not <node> — refusing to touch it` | The Device's `vmid` CF points at a VM with another name on the carrier. Nothing was touched. Clear or correct the CF (empty = the job picks a free vmid) |
| Deploy / Ingest Image / nested install refuses: `REFUSED: image <file> has no checksum on its SoftwareImageFile — the node-side pull would be unverified. Register it via 'Register Image from Published Set' (or set image_file_checksum + hashing_algorithm on the record)` | The image record was created by hand without `image_file_checksum`. Proxmox's `download-url` only verifies when given a checksum, and a pulled file is then reused by filename for every later deploy, so the jobs no longer pull unverified bytes. Register the image with `Register Image from Published Set` (it reads the `.sha256` sidecar), or fill `image_file_checksum` + `hashing_algorithm` on the `SoftwareImageFile` from the published sidecar — never from the downloaded file itself. Nothing was pulled. If an unverified copy was pulled before this check existed, delete it from the node's import/ISO storage so the next run pulls a verified one |
| Job log *warning*: `Proxmox task UPID:… on <node> succeeded with N warning(s) - see the task log on the node` | Informational, not a failure: PVE finished the task (VM create/start/stop/destroy, image download, ISO upload) with exit code 0 but logged `WARN:` lines, so its status is `WARNINGS: N`. The job carries on (deploy no longer rolls back a VM that started fine). Read the WARN lines in the node's **Tasks** panel (or `pvenode task log <UPID>`) and fix the cause if it matters — e.g. MTU or EFI-cert notices on `qmstart`. A task whose status is anything other than `OK`/`WARNINGS` still fails the job |
| Job log *warning*: `Proxmox task UPID:… on <node>: status poll failed (k/5 consecutive), retrying in Ns: …` | The job could not read the task's status for a moment (connection refused/reset, timeout, HTTP 5xx such as pveproxy's `596`, or a non-JSON reply) — typically pveproxy restarting or a network blip. The task itself keeps running on the node; the job retries with a growing delay (capped at 30 s) and a single good poll resets the count. Nothing to do unless it repeats — then check `systemctl status pveproxy` and the worker→node path |
| Job fails: `Lost contact with task UPID:… on <node> after 5 consecutive failed status polls - the task may still be running on the node (check its Tasks panel): …` | Five status polls in a row failed, so the job gave up *observing* the task — this is **not** a task failure. Open the node's **Tasks** panel (or `pvenode task status <UPID>`) to see how it ended. A deploy rolls back best-effort, but PVE refuses to destroy a VM still holding `lock: create`: if the VM finished creating after the job gave up, the re-run is refused by the name-collision check — destroy that VM by hand (it is not in the SoT; the device is still Planned), then re-run. Fix the node reachability first (pveproxy, worker→node routing) |
| Job log *warning* after a failed deploy: `Not rolling back VM <vmid> on <node>: it is named '<other>', not '<device>' — another deploy likely took the same vmid (/cluster/nextid reserves nothing). Left untouched; re-run this deploy` | Two deploys to the same node were handed the same vmid (PVE's `nextid` is a suggestion, not a reservation), the other one created its VM first, and this run's create failed (typically `config file already exists`). The rollback now destroys only a VM carrying this device's name, so the other deploy's VM is left alone (an unnamed VM is left alone too). Nothing of this run's VM exists on the node and the device is still Planned: just re-run the deploy, which asks for a fresh vmid. If `<other>` is not a VM you expect, reconcile it before re-running |
| Job log: `… -> transport error: ConnectionError: …` / `… -> 5xx: …` / `… response is not JSON` (e.g. in `Could not roll back VM …`, `Bootstrap-ISO sweep failed …`) | The Proxmox API was unreachable or answered garbage for that call. These now count as Proxmox errors, so the best-effort cleanups (deploy rollback, decommission ISO sweep) log a warning and carry on — decommission still writes the SoT back after a destroy. Do the named manual cleanup once the node answers again |
| Deploy job log *warning*: `Readiness UNVERIFIED — the token may not query the guest agent: guest-agent probe on VM … refused (403): the API token lacks VM.GuestAgent.Audit …` (result: `Deployed … — readiness unverified`) | The VM is deployed and the SoT already says Active; only the readiness probe (`agent/network-get-interfaces`) was refused. Since PVE 8.2 it needs `VM.GuestAgent.Audit`, which nodes installed or hand-built before 2026-10-02 lack in their `NFVAutomation` role. On the node, run the `pveum role modify NFVAutomation --privs "…"` line from [getting-started §4](getting-started.md#4-proxmox-service-account-per-node) (the role is granted to both user and token, so one modify covers both). Then check the guest by hand (console / `qm agent <vmid> network-get-interfaces`); later deploys report the IP again. A `401` instead means the token itself is invalid — re-check the SecretsGroup |
| Installer: `409 install NIC: …` / log `REFUSED: primary_ip4 … is not assigned to any interface of …` / `… is assigned to several interfaces of … (…) — ambiguous install NIC; keep it on one` (install job: `Install NIC: …`) | The install NIC is derived from `primary_ip4`'s interface (decision #55). Assign the IP to exactly one interface of the Device — the management bridge (`vmbr0`) or the mgmt port. Nothing was answered or booted |
| `REFUSED: bridge … on … has no member interfaces — set the Bridge field of its port(s) to …` / `LAG … on … has no member interfaces — set the LAG field of its port(s) to …` | The bridge (or bond) carrying `primary_ip4` has no members in Nautobot: set the port's (or bond's) **Bridge** field to the bridge, and each physical port's **LAG** field to the bond |
| `REFUSED: LAG … on … has several members (…) and none is flagged primary_member — flag the port that carries the install` (or `bridge … has several members …`) / `…: primary_member is set on several members (…) — flag exactly one` | Several members and no unique install port: set the interface custom field `primary_member` on exactly one — the port that is cabled for the install (the management bond's primary). Two flags in one bond or bridge are as ambiguous as none |
| `REFUSED: install NIC derivation for … reached … (type …) via … — only bridge -> LAG -> port nesting is supported` | The chain from `primary_ip4` must end on a physical port: bridge → bond → port, bridge → port, bond → port or the port itself. A bridge inside a bridge, or a bond inside a bond, is refused |
| Log `REFUSED: … installs static (…) but its install NIC … (via …) has no MAC — record the port's MAC on its Nautobot interface` (installer: `409 static install needs the install port's MAC (derived through the management bridge/LAG) recorded in Nautobot (contract §4)`) | The derivation reached a port without `mac_address`. Record the port's MAC (installer shell: `proxmox-auto-install-assistant device-info -t network`) |
| Log `REFUSED: …: host_baseline.… (config context)` (installer: `409 config context: …`) — e.g. `host_baseline.serial_console.speed must be one of [9600, 19200, 38400, 57600, 115200] (got …)`, `…parity must be no, odd or even`, `host_baseline.serial_console has unknown key(s) […]`, `host_baseline.zfs_arc_max_bytes must be an integer >= 67108864 (64 MiB; got …)`, `host_baseline.remove_subscription_nag must be true or false (got …)`, `host_baseline.packages: '…' is not a valid Debian package name`, `host_baseline.packages must be a list of Debian package names`, `host_baseline.serial_console must be a mapping like {speed: 115200}`, `host_baseline.serial_console.word must be 5-8 (got …)`, `host_baseline.serial_console.stop must be 1 or 2 (got …)`, `config context host_baseline must be a mapping` | A firstboot input in the Device's rendered config context is malformed; it is refused at answer time, before the installer runs. Fix the config context (the `nfv-host-baseline` ConfigContextSchema catches most of these when you edit it) and re-run the install. *Missing* inputs are not refused — firstboot logs and skips them |
| `500 profile install.serial_console must be a mapping like {unit: 0}` / ``… accepts only `unit` (got […]) — speed and framing come from the config context host_baseline.serial_console`` / `….unit must be an integer 0-7 (ttyS<unit>)` | The DeviceType profile's serial-port declaration is invalid; it is checked at answer time. Fix the profile and rebuild the answer service |
| Install job warning: `answer service at … does not render the host_baseline firstboot input(s) […] set in this Device's config context — the node would install without them …` | The answer-service image predates decision #55: it answers but silently skips packages / serial console / ARC / nag hook. Rebuild it from the current main (`docker compose --profile answer-service up -d --build answer-service`). The Host Baseline job still ensures the packages |
| Firstboot log `serial console: the DeviceType profile declares ttyS0 but the SoT has no host_baseline.serial_console (speed) — serial console NOT configured` / `serial console: the SoT sets host_baseline.serial_console but the DeviceType profile declares no serial port (install.serial_console) — skipped` | Informational, never a refusal (decision #55): both halves are needed — the port from the profile, the speed from the config context. Set the missing half; a reinstall picks it up (the Host Baseline job does not configure the console) |
| Firstboot log `packages: apt-get install … FAILED — the Host Baseline job retries it` / `serial console: update-grub FAILED` / `serial console: proxmox-boot-tool refresh FAILED` / `could not enable serial-getty@…` / `zfs arc: update-initramfs FAILED — the limit applies only once the initramfs is rebuilt` / `… step incomplete — continuing` | Firstboot's host-baseline steps never stop the script (they run after the credentials phone-home). Check `journalctl -u proxmox-first-boot` for the command's error; packages come back with the Host Baseline job, the others need the command re-run by hand (or a reinstall) |
| Journal `nfv-remove-subscription-nag: pattern not found in /usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js (proxmox-widget-toolkit …) - the UI keeps the subscription dialog; this toolkit version needs a new pattern` (or `patching … FAILED`) | The toolkit's subscription check changed shape in this release; apt is unaffected (the hook always exits 0). Update the pattern in the firstboot template (`firstboot.sh.j2`, `nfv_subscription_nag`) and redeploy the script, or leave the dialog |
| Host Baseline refused: `… has role '…', not 'NFV' — refusing to baseline it; …` / `Dry run is off but Confirm is not ticked — refusing to change the node` / `… provisioning_state is '…' — the Host Baseline applies to bm_installed / baseline_done nodes (a dry run to any installed state: …); set the state in the SoT first` / `… has no serial — the identity check compares it with the node's DMI serial` / `… has no primary_ip4 — the job connects to it and renders it onto the management bridge` | Gate refusals — nothing was read from or written to the node. A hand-built node needs `provisioning_state=bm_installed` set by hand; in-service nodes (`fabric_done` and later) accept dry runs only |
| Host Baseline refused: `config context has no 'host_baseline' block — …` / ``host_baseline.<section> is missing — set it (contract §4c) or set `<section>: {enabled: false}` to skip that step on purpose`` / `host_baseline.service_accounts is missing — …` / `host_baseline.… is missing` / `… must be …` / `… is not …` / `… contains a control character (newline, tab, ...) — not allowed` / `host_baseline.snmp grants no access — …` | Every required config-context fact is named by its path; the job lists all of them at once. Fill them in the right context (fleet / site / device — Nautobot merges them) per contract §4c; attach the `nfv-host-baseline` schema to catch shape errors when editing |
| Host Baseline refused: `host_baseline.service_accounts[n].user '…' is not managed by the Host Baseline (root, and the firstboot deploy account …)` / `….name '…' is reserved (<node>-proxmox holds the deploy token)` / `….user '…' is listed twice` / `….name '…' is listed twice — the SecretsGroup <node>-… would collide` / `….name '…' is not a SecretsGroup suffix …` / `….privsep must be true or false …` / `host_baseline.ad.realm '…' is a built-in PVE realm` | The service-account list names an identity the job must not own (root, `svc-nfv@pve`), or two entries collide; `name` sets the SecretsGroup suffix when the user part is not usable. `privsep` is required — the recommended accounts use `false` (contract §4c) |
| Host Baseline refused: `Secret '…' (…) does not exist — re-run Bootstrap NFV Data Model …` / `Secret '…' (…) has no readable value (…) — supply it (composer: ./add-secret.sh …)` / `Secret '…' (…) is empty` | Create the record (bootstrap does it for the conventional names and every v3 user a config context names — re-run it after adding users) and supply the value. The message never contains a secret value |
| Host Baseline refused: `Secret '…' (SNMP community) must be 1-64 printable ASCII characters without spaces, quotes, '#' or backslashes (snmpd.conf token)` / `Secret '…' (SNMPv3 … passphrase) must be 8-128 printable ASCII characters without double quotes or backslashes (net-snmp createUser)` / `Secret '…' (AD bind password) contains a line break or NUL — PVE reads only its first line` | The value would break the file it is written into (net-snmp syntax, PVE's one-line credential file). Choose a value within the rule and rotate it in the Secret |
| Host Baseline refused: `the Device has no Location name for SNMP sysLocation` / `Location name '…' contains a control character — cannot be sysLocation` | sysLocation is the Device's Location **name** (decision #54/#55) — fix the Location record |
| Host Baseline refused: `the worker cannot write /opt/nautobot/secrets/nodes — service-account tokens are stored there as text-file Secrets …` | The Celery worker needs its read-write `secrets/nodes` mount (nautobot-composer#66): pull composer, run `./setup.sh` (it makes the directory group-writable), recreate the worker. Elsewhere point `NFV_NODE_SECRETS_DIR` at a directory both Nautobot containers see and the worker can write. Dry runs skip this check |
| Host Baseline refused: `… network model: primary_ip4 … is on … (type …), not on a bridge — model the management bridge (vmbr0, type bridge) …` / `… primary_ip4 … must sit on exactly one interface of the device (found N)` / `… no DefaultGW-role IP in primary_ip4's parent prefix (contract §3)` / `… DefaultGW … is outside primary_ip4's network …` / `… several DefaultGW-role IPs in primary_ip4's prefix (…) — contract §3 allows exactly one` | The management path must be the bridge carrying `primary_ip4`, with exactly one DefaultGW-role IP in that IP's prefix. A legacy `mgmt` (virtual) interface carrying the IP must be remodelled: `vmbr0` (bridge) with the IP, the bond or port as its member |
| Host Baseline refused: `LAG '…' must be named bond<N> …` / `bridge '…' must be named vmbr<N> …` / `LAG … has no members …` / `LAG … has no valid lag_mode custom field …` / `LAG … lag_xmit_hash '…' is not one of …` / `LAG … is … but has no lag_xmit_hash — set the transmit hash policy in the SoT (it must match the switch side)` / `LAG … sets lag_xmit_hash '…' but mode … does not use it — clear the field or fix the mode` / `LAG … needs host_baseline.network.bond_miimon in the config context` / `LAG … is 802.3ad but host_baseline.network.lacp_rate is not set …` / `LAG …: primary_member is set on several members (…) — flag exactly one` / `bridge …: primary_member is set on several ports (…) — flag exactly one` / `LAG … is active-backup with N members but none is flagged primary_member — …` | The bond model is incomplete or contradictory. PVE recognises bonds and bridges by name (`bondN` / `vmbrN`); the mode, hash policy and LACP rate must be stated, not defaulted — they have to match the switch side |
| Host Baseline refused: `… mtu … differs from its LAG … mtu … (the kernel forces the bond's MTU onto members)` / `bridge … mtu … is above its port …'s mtu …` / `… mtu … is outside 576-9216` | Set MTUs consistently along the path (members = bond ≥ bridge); MTU 9000 on the data path is the fleet target (#54) |
| Host Baseline refused: `member … of … has no MAC address — the baseline matches ports to the node's NICs by MAC, never by name` / `… (type …) is a member of … but is not a physical port` / `… is both a LAG member and a bridge port` / `bridge … has another bridge (…) as a port` | Members are physical ports with their MACs recorded (contract §4c); a port is in one bond or one bridge, not both |
| Host Baseline refused: `bridge … is mode tagged but carries no tagged VLANs` / `bridge … has mode access — use tagged-all (VLAN-aware) or no mode (plain bridge)` / `bridge … carries address(es) … besides primary_ip4 — …` / `… carries address(es) … but is not part of the bond/bridge topology — the baseline would drop it` | The rendered file would contradict the SoT: give the tagged bridge its VLANs or use `tagged-all`; keep only `primary_ip4` on the topology; remove (or model) addresses on interfaces outside it (`xcc` is exempt) |
| Host Baseline: `could not open an SSH session to … as the host_ssh_username login (…) — check primary_ip4, root SSH (PermitRootLogin) and the host_ssh_username/host_ssh_password Secrets` / `the applier did not finish (rc=…): the applier must run as root (host_ssh_username must be root)` / `the applier did not finish (rc=…): …` / `the SSH session to the node ended mid-step (…)` | The job connects to `primary_ip4` as **root** (as the host-verification job): PVE permits root SSH by default; the Secrets must hold the node's root login. A session that ends mid-step (network blip, node reboot) fails that step; every step is idempotent — re-run |
| Host Baseline refused: `the node at the management IP calls itself '…', not '…' — wrong primary_ip4 or wrong Device; refusing to touch it` / `the node's DMI serial is '…', but …'s serial is '…' — refusing to touch that machine` / `could not read the node's DMI serial (… needs the ROOT login …)` / `could not read the node's hostname` / `the host_ssh_username login is not root — …` | Identity check, before any write: `primary_ip4` leads to another machine, or the Device's name/serial is wrong. Fix the record; nothing was changed on the node |
| Host Baseline refused: `PVE role '…' (named in host_baseline) does not exist on the node — …` / `could not list the node's PVE roles (pveum role list) — is this a Proxmox VE node?` | A service account or the AD admin group names a role the node lacks: use a built-in (`PVEAuditor`, `Administrator`, …) or create the role first |
| Host Baseline refused: `realm … exists on the node with type '…' but the SoT describes an AD realm — …` / `realm-sync job … exists for realm '…' but the SoT names realm … — a sync job's realm is fixed; delete it by hand (pvesh delete /cluster/jobs/realm-sync/…) or rename host_baseline.ad.sync_job.name` | The node carries a conflicting realm or sync job (hand-built). Remove or rename it by hand — the job never deletes realms or jobs |
| Host Baseline refused: `… network: member … (…) is not among the node's physical NICs (…) — wrong MAC in Nautobot, or the card is missing` / `… network: MAC … of … appears on several node NICs (…)` / `… network: … and … both resolve to node NIC … (duplicate MAC in Nautobot)` | Ports are matched by MAC (the permanent one when enslaved); the refusal lists every physical NIC the node reported as `name=mac`. Fix the MAC on the Nautobot interface or check the hardware. Nothing was written |
| Host Baseline step failures: `apt-get install … rc=…: …` / `systemctl enable --now lldpd rc=…` / `systemctl stop snmpd rc=… (SNMPv3 users are created with snmpd stopped): …` / `could not append createUser to …` / `systemctl restart snmpd rc=…: … — journalctl -u snmpd` / `snmpd is not active after the restart — journalctl -u snmpd` / `could not write /etc/snmp/snmpd.conf` / `could not write /etc/pve/priv/realm/….pw` / `token remove rc=…: …` / `token add rc=…: … (a re-run rotates a token left without stored Secrets)` / `token add printed no value — …` / `the payload carries no bind password` / `rc=… from: pveum …` (result `… failed on the node — see the log; later steps (the network above all) did not run`) | A command failed on the node; the step's log line names it, and later steps did not run (the network never runs after a failure). Fix the cause on the node (repositories for apt, `journalctl -u snmpd`, the PVE error text) and re-run — every step is idempotent |
| Host Baseline warnings: `apt-get update rc=… (trying the install anyway): …` / `systemctl enable snmpd failed` / `initial realm sync failed (rc=…: …) — AD unreachable or bind credentials wrong; re-run once AD answers` / `sync --dry-run failed (rc=…: …) — AD unreachable or the stored bind password is stale` / `group … does not exist on the node (realm sync failed, or the group filter excludes it) — … on … NOT granted; re-run once the group has synced` | Not failures: the job finished, but AD did not answer (or the admin group is filtered out), so the admin-group ACL is still missing. Check reachability of the AD servers from the node and the bind DN/password, then re-run |
| Host Baseline warning: `Could not verify the stored … token against https://…:8006 from the worker — keeping it …` / log `token … stored in SecretsGroup '…' (not verified (worker cannot reach :8006))` | The worker cannot reach the node's API; the token is kept / stored but unverified. Deploy jobs need worker → node `:8006` anyway — fix the path. A token the node rejects with 401 is rotated on the next run |
| Host Baseline failures: `the node returned no usable value for … — re-run (a token whose Secrets are missing is rotated)` / `the new token … was stored in SecretsGroup '…' but the node rejects it (401) — re-run` | The token was created but not stored (or not accepted): re-run — a token without its Secrets is rotated, never left half-stored |
| Host Baseline warnings: `pending PVE GUI network changes exist in /etc/network/interfaces.new — a non-dry run discards them` / `discarding pending PVE GUI network changes staged in …` | Someone staged network edits in the PVE GUI; the SoT render replaces them. Put the intended change into Nautobot instead |
| Host Baseline network failures: `ifupdown2 rejected the rendered file (rc=…): … — nothing applied` / `could not stage …` / `could not back up … — nothing applied` / `could not arm the rollback timer (rc=…: …) — nothing applied` / `could not move … into place — nothing applied, timer cancelled` / `ifreload -a failed (rc=…; journalctl -u nfv-baseline-net-apply-…) — restored …, reloaded, timer cancelled` | The apply was refused or undone on the node; the previous `/etc/network/interfaces` is in place (backup `interfaces.nfv-baseline.<timestamp>`). Read the ifupdown2 / journal error; usually a SoT value the kernel rejects (MTU above the NIC's maximum, a bond option) |
| Host Baseline: `the SSH session ended during the apply (…) — expected when the management path moves; reconnecting on …` (warning) then `network apply lost management reachability: no SSH on … within …s, so the rollback timer (nfv-baseline-net-rollback-…) restores the previous /etc/network/interfaces — …` | After the apply the node did not answer on `primary_ip4` in time: the timer restores the previous file and reloads. The message says whether the node answered again afterwards; if it did not, use the console (XCC / serial). Usual causes: the management bond's members or primary port wrong in the SoT, a switch port not configured for the new bond, the VLAN on the wrong bridge. Raise `rollback_seconds` only if the reconnect is merely slow |
| Host Baseline: `the rollback already ran (…) — the previous configuration is back` / `the rollback fired while confirming — …` / `could not stop ….timer — it will restore the previous file` (result `the rollback timer fired before the job could cancel it — …`) | The job reconnected too late (or the timer could not be stopped): the previous configuration is back. Re-run; raise `host_baseline.network.rollback_seconds` if the reconnect is slow |
| Host Baseline: `the apply did not take effect (the session ended before the file was moved into place?) — …` | The session dropped before the apply happened; the node keeps its previous file. Re-run |
| Host Baseline bond-state **errors**: `…: /proc/net/bonding/… is missing — the bond is not up` / `…: running mode '…', SoT says '…'` / `…: running members […], SoT says […]` (result `the network was applied and management reconnected, but the running bonds do not match the SoT …` or `the running bonds do not match the SoT although the file does …`) | The file is applied but the kernel's bond differs — typically a mode change ifupdown2 did not apply in place. Check `cat /proc/net/bonding/<bond>`; `ifdown <bond>; ifup <bond>` (console or a non-management bond) or a reboot applies it. Management stayed reachable, so nothing was rolled back |
| Host Baseline bond-state **warnings**: `…: LACP has no partner (partner MAC …) — the switch ports are not running LACP for this bundle yet` / `…: only […] aggregate with partner … (active aggregator …, … port(s)) — check the switch-side channel` / `…: member … link is …` / `…: active member is …, the SoT primary … is not carrying traffic (its link down?)` / `…: transmit hash policy …, SoT says …` / `…: miimon … ms, SoT says …` / `…: LACP rate …, SoT says …` | Switch-side or cabling state, reported but never failed (decision #54 — the switch configuration is a separate work item). The node is configured as the SoT says; flip the switch ports (e.g. `channel-group … mode active`, matching hash/LACP rate) and re-run the job (a dry run is enough) to see the warnings clear |
