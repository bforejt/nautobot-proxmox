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

What you should observe: the job creates a VM on the NUC with the Device's
SMBIOS serial and boots the prepared ISO → answer service logs `ANSWERED` →
unattended install (~10 min) → webhook flips `provisioning_state=bm_installed`
→ VM powers off (nested profile) → job detaches the ISO and boots from disk →
firstboot creates `svc-nfv@pve!deploy` with role NFVAutomation (granted to
BOTH user and token) and phones the token home → answer service writes the
text-file Secrets, creates SecretsGroup `<name>-proxmox`, and sets the
Device's `secrets_group` CF. **The node is now deployable by the existing VM
jobs with zero manual credential steps.**

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
LVM-thin `datastore` at firstboot. See "Disk layout" below and
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
   data.)

Device records for the target unit:

- Device: DeviceType **ThinkSystem SE350**, role **NFV**, the unit's
  **DMI serial** (host-verification job reports it; the rehearsal unit's is
  `J101YCEB`), `provisioning_state=awaiting_install`, `software_version` =
  the Active prepared ISO, CFs `vm_bridge`/`vm_storage`/`import_storage` for
  its post-install life.
- Interface named **`xcc`** with the BMC IP assigned (contract §4) — the
  delivery adapter reads it. Secrets `xcc_username`/`xcc_password` as before.

Run **`Install Proxmox Node (SoT-driven)`** with Confirm ticked. Expected:
mount via PATCH-EXT → one-shot CD → ForceRestart → `ANSWERED` in the service
log → unattended install (**the ISO streams through the BMC NIC for the whole
install — allow 20–40 min**, slower than PXE/nested) → webhook flips
`bm_installed` → reboot to disk → firstboot creates the service account and
phones the token home → SecretsGroup set → the job ejects the spent installer
media. The node is then deployable by the VM jobs.

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

1. **Discovery first, before any Device edits**: run `SE350 Platform Discovery`
   (it is generic — any Lenovo XCC) against the XCC2 IP with the host powered
   on. Read from its log: the **serial** (goes in the Device), **XCC2
   Platinum ENABLED**, **EXT members present**, and the **drive inventory** —
   four drives on the `RAID_Slot<n>` controller, the 480 GB pair and the
   1.92 TB pair, ideally all `Unconfigured good`. Drives shown as `JBOD` must
   be converted to Unconfigured Good once (XCC storage page or UEFI) — the
   layout step never converts drives itself. Drives already `Online` in
   admin-made volumes are fine: the step **adopts** a RAID1 over the two
   smallest drives as `boot` and one over the two largest as `datastore`
   whatever the adapter calls them (Lenovo defaults are `VD_0`/`VD_1`);
   other shapes (a RAID10 over all four, a lone RAID1 over the big pair
   with the small pair also in use) make it refuse.
2. **Device record**: DeviceType **ThinkEdge SE455 V3** (bootstrap-created),
   role NFV, the XCC-reported serial, `provisioning_state=awaiting_install`,
   `software_version` = the Active prepared ISO, CFs `vm_bridge`,
   **`vm_storage=datastore`** (the firstboot-created LVM-thin storage),
   `import_storage=local`; interface `xcc` with the BMC IP; `mgmt` interface
   with `primary_ip4` and the OCP mgmt port's **MAC pinned** (no onboard NIC
   on this box — the NIC filter is the only thing naming the port, and with
   interface name pinning that MAC also makes the Linux name `mgmt`; record
   the MACs of the other ports on their Nautobot interfaces if you want SoT
   names for them too, otherwise they come up as `nic<N>`).
3. **Storage layout dry run**: `Apply Storage Layout (SoT-driven)` with the
   default dry run prints the plan (`create boot RAID1` over the two 480 GB
   drives, `create datastore RAID1` over the 1.92 TB pair) without touching
   the adapter. Untick dry run + Confirm to create them now, or let the
   install job do it as its first step — same code, same rules.
4. **Confirm the boot pin the first time**: the profile selects the boot VD
   as the adapter's first virtual drive (`ID_PATH: "*-scsi-0:2:0:0"`). Boot
   the prepared media once, switch to the tty3 root shell (`Ctrl+Alt+F3`) and
   run `proxmox-auto-install-assistant device-match disk
   ID_PATH='*-scsi-0:2:0:0'` — it must list exactly the ~480 GB volume. If the
   layout step warned that `boot` is not the first volume (hand-made units),
   fix the order (delete and re-create by hand) or pin by `ID_SERIAL` from
   that shell's `device-info -t disk` output.
5. Run **`Install Proxmox Node (SoT-driven)`** with Confirm. Expected: RAID
   layout ensured (host powered on into UEFI Setup if it was off) → EXT mount
   (XCC2 also takes https URLs) → one-shot CD → `ANSWERED` (source static, fs
   ext4) → install onto the boot VD → webhook → reboot → firstboot: service
   account, credentials phone-home, then **LVM-thin `datastore/data` on the
   data VD** + `pvesm add lvmthin datastore` (an existing volume group is
   reused on reinstall). Check `journalctl -u proxmox-first-boot` for the
   `data volume datastore/data created` / `PVE storage datastore registered`
   lines, then `pvesm status`.

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
  actually reported are mapped; names must be 2–15 chars, letter first,
  alnum/underscore, and may not be `nic<N>` (skipped with a log line
  otherwise). The `xcc` interface is never a host NIC. The webhook log line
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
    - {name: datastore, raid: RAID1, select: largest,  count: 2}
```

| Piece | What it is |
|---|---|
| **`boot`** — RAID1 over the two smallest unconfigured drives, created first | The adapter's first VD (SCSI target 0 → `ID_PATH *-scsi-0:2:0:0`, the profile's boot filter). The installer lays ext4 + LVM-thin on it exactly as on the SE350: `local` (iso/import/backup) and `local-lvm` |
| **`datastore`** — RAID1 over the two largest unconfigured drives | The data VD. The firstboot hook (`install.data_volume`) makes it VG `datastore` with thin pool `data` and registers the lvmthin storage **`datastore`** (images, rootdir) — the contract's `vm_storage=datastore` |

Rules the layout step enforces: the picked drives must be equal-sized and the
pick unambiguous (a third drive of the same size refuses); volumes that exist
by name are kept after a RAID-type check, never re-created; nothing is ever
deleted; JBOD drives are reported, not converted. The XCC reports RAID
inventory only while the host is powered on, so the step powers it on with a
one-time boot into UEFI Setup and waits for the adapter to enumerate. It warns
when `boot` is not the adapter's first volume (hand-made units), because the
ID_PATH pin assumes it is. `install.data_volume` keys: `vg`, `thinpool`,
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
preflight" evaluate the same rules from the host side.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Installer sits at answer fetch | Answer service log (`docker compose logs answer-service`): `REFUSED` lines say exactly why (unknown serial, wrong state, missing DefaultGW, no profile). **No `POST /answer` line at all** = the machine never reached the service (wrong media/URL, network, or the service host asleep/down) — nothing to fix in Nautobot |
| Installer: `Fetching answer file via HTTP failed: http error: 403 Forbidden: {"detail":"no install profile for DeviceType '...'"}` | The answer service image predates the DeviceType's profile (profiles bake in at build; the jobs update independently via Git sync). Rebuild it from the current main — `docker compose --profile answer-service up -d --build answer-service` — verify with `docker exec answer-service ls /app/profiles`, then re-run the install job: the storage step adopts the volumes it already made and the media is re-mounted |
| Installer: `filter did not match any device` / `... any devices` | The answer was issued, but its NIC filter (`ID_NET_NAME_MAC` from the pinned mgmt MAC) or the profile's disk filter matched nothing on this box. From the installer shell: `proxmox-auto-install-assistant device-info -t disk` / `-t network`, then `device-match disk KEY='glob'` until it lists exactly the intended disk(s); fix the profile (or the pinned MAC) and rebuild the answer service |
| Need a shell on the installer | Every mode runs a root shell on **tty3** (`Ctrl+Alt+F3`; tty2 = installer stderr). A failed automated install drops to a debug shell on tty1 (our answers set `reboot-on-error = false`). To pause *before* anything runs, add `proxmox-debug` to the kernel line (press `e` in GRUB on the automated entry, or use the `debug` iPXE entry). Logs: `/tmp/fetch_answer.log`, `/tmp/auto_installer.log`, `/tmp/install-low-level.log` |
| `Storage layout refused: ... JBOD` / `only N free` | The RAID adapter's drives are not `Unconfigured good` (JBOD, hot spare, or already in a volume of the wrong shape). Convert JBOD drives once in the XCC storage page or UEFI; a volume the step cannot adopt (wrong RAID level or drive set) must be deleted by hand — the step never deletes |
| `datastore` storage missing on a hand-built unit | Its data VD already carries an LVM signature with a differently named volume group: firstboot creates nothing on a signed disk and registers only a VG named `datastore`. Rename the VG (`vgrename`) or wipe the VD (`wipefs -a`, data loss) before installing |
| `boot volume 'boot' is volume #2 on the adapter` warning | The volumes were created by hand in the other order; the profile's `ID_PATH *-scsi-0:2:0:0` pin would select the data VD. Re-create in the right order, or pin `ID_SERIAL` from the installer shell's `device-info -t disk` |
| `datastore` storage missing after an SE455 V3 install | `journalctl -u proxmox-first-boot` on the node: the data-volume step logs why it refused (no unused signature-free disk at the largest size, or LVM error). A reused volume group from a previous install is expected and logged |
| Installer fails with `duplicate interface name mapping` or `interface name ... is invalid` | The pinning mapping rendered from Nautobot clashed (two interfaces with the same name, or a name the installer's `pve-iface` rule rejects). The answer service skips such names with a log line before rendering; if the installer still complains, check the `ANSWERED ... names=` log line against the Device's interfaces |
| `datastore` pool missing after a JBOD (ZFS) install | `journalctl -u proxmox-first-boot` on the node: the data-pool step logs why it refused (fewer/more than `count` equal-sized unused disks, or leftover signatures — `wipefs -a` the intended data disks by hand only if they are truly spare, then `zpool create` + `pvesm add zfspool` per the profile) |
| `500 root password hash not provisioned` in the log | `secrets/root_password_hash` missing/empty — composer's `./setup.sh` generates it when the answer-service profile is enabled (re-run it), or create manually: `openssl passwd -6 > secrets/root_password_hash` |
| Install finished but state didn't flip | `docker compose logs answer-service` — webhook arrives before reboot/power-off; payload archived in `/data/install-<serial>.json` |
| No credentials after first boot | Node's journal: `journalctl -u proxmox-first-boot`; the phone-home retries for ~10 min, and its one-time key stays valid until success — but a consumed key needs a fresh install (by design) |
| Phone-home 403 `source does not match` | The node reached the service from an IP other than its SoT primary_ip4 (NAT?) — fix the record or set `VERIFY_PHONE_HOME_SOURCE=false` |
| Webhook never arrived but node installed fine | Observed once on the PXE path (real NUC). The credentials phone-home also advances the state (firstboot = proof of install), so the loop self-heals; the log says `state advanced ... via credentials phone-home`. Exact webhook loss cause `[lab-verify]` |
| Nested VM reinstalls in a loop | The nested profile must keep `reboot_mode: power-off` so the job can detach the ISO |
