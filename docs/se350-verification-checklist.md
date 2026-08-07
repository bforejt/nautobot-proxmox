# SE350 Platform Verification Checklist (Phase 0)

Hands-on checks against real SE350 units before job development hardens assumptions.
Everything here is read-only or lab-scoped unless marked otherwise. Record results per
machine serial (they feed Nautobot device records and the platform-profile YAML).

Replace `XCC_IP`, `XCC_USER:XCC_PASS` as appropriate. All curl examples are plain GETs
and safe against in-service units.

> **Shortcut for §§1–3:** the `SE350 Platform Discovery` Nautobot Job
> ([jobs/baremetal/discover_platform.py](../jobs/baremetal/discover_platform.py))
> collects all of it in one run — BIOS dump (+ allowed-values registry), VirtualMedia
> EXT check with pass/fail verdict, firmware inventory, Secure Boot state — and
> attaches the JSON dumps to the JobResult. Its opt-in **write checks** also cover
> §1's functional test: mount/verify/eject of a test ISO, plus the DISRUPTIVE
> dress-rehearsal option (boot-once from the mounted ISO — lab units only) that is
> the end-to-end proof of the no-USB install mechanism. The curl commands below
> remain as the no-Nautobot fallback.

## 1. XCC virtual media functional check (license question answered)

**Answered 2026-08-06: fleet licenses are XCC Enterprise**, so virtual media is
license-entitled everywhere. What remains is the functional check — confirm the EXT
members actually appear at the fleet's XCC firmware level (and PATCH-insert works),
since firmware minimums for reliable Redfish vmedia exist per XCC1 model.

**Functional check** — list the VirtualMedia collection members:

```bash
curl -sk -u 'XCC_USER:XCC_PASS' https://XCC_IP/redfish/v1/Managers/1/VirtualMedia | python3 -c "import json,sys; [print(m['@odata.id']) for m in json.load(sys.stdin)['Members']]"
```

- `EXT1`–`EXT4` present (a licensed XCC1 shows ~10 members incl. `RDOC*`/`Remote*`) →
  **automation path works.**
- Only `RDOC*`/`Remote*` → firmware too old (license is known-good) → XCC firmware
  update, then re-test.
- `/Managers/1` 404s → list `/redfish/v1/Managers` and substitute the member ID.

Notes: only `EXT{N}` members are Redfish-insertable, via **PATCH** on the member (not
POST InsertMedia) on XCC firmware "19A"+; ISO URLs must be **plain HTTP or credential-less
NFS** — no authenticated HTTPS. Record whether FoD keys are transferable to spare units.

## 2. XCC / UEFI firmware inventory

Record XCC and UEFI build levels per unit (`GET /redfish/v1/UpdateService/FirmwareInventory`
or the XCC UI). No published SE350 minimum for reliable Redfish vmedia exists — treat
"latest available" as the target, pin that bundle in the repo, and stop chasing updates.
Note: Lenovo OneCLI does not support Debian — firmware updates on Proxmox hosts are
out-of-band only (XCC UI / Redfish UpdateService).

## 3. BIOS attribute dump (feeds `bmc/se350_bios.yaml`)

```bash
curl -sk -u 'XCC_USER:XCC_PASS' https://XCC_IP/redfish/v1/Systems/1/Bios | python3 -m json.tool > se350-bios-dump.json
```

**Done 2026-08-07** (first discovery-job run, 172 attributes + registry): all nine
UEFI-side standard settings verified with exact names/enums in `bmc/se350_bios.yaml`
(one correction: `MMConfigBase_3GB` enum prefix). Findings: **Thermal Mode is not a
UEFI attribute on SE350** — confirmed: the legacy tool sets it via SSH to the XCC CLI
(`thermal performance`), in the same session as its `asu set` commands (ASU names map
1:1 to the Redfish attributes). The discovery job's chassis/OEM probe (next run) tells
us whether a Redfish OEM equivalent exists; otherwise ApplyBiosPolicyJob keeps one
small XCC-SSH step for thermal. **The sampled unit shows drift**: it sits at the
`MaximumPerformance` preset with `MONITORMWAIT=Disable` (the preset forces it),
not the standard's CustomMode + MWAIT Enable — either un-standardized or predates
the tool; the audit job exists for exactly this. All proposed-addition settings
(console redirect group, serial sharing, Secure Boot) are already at desired values
on the sampled unit. Remaining from this section: after the policy is applied to a
lab unit, run `cpupower idle-info` on the PVE host — that decides whether the
firstboot kernel C-state args stay or go.

## 4. Boot storage: hardware-RAID volume characterization (design decided)

Answered 2026-08-07: the fleet's RAID controller presents a **single volume** from the
M.2 set — individual disks are not visible at install (matches the ESXi experience).
Design: **ext4 + LVM-thin on that volume**, no ZFS. What remains is characterization
on a live Linux boot (`lsblk -o NAME,MODEL,SERIAL,SIZE`, `lspci -k`):

- Capture the volume's model/serial string exactly as Linux enumerates it — this
  becomes the `answer.toml` disk filter so the installer can never select a data disk.
- Determine what alerting exists for a **degraded mirror** (XCC event? UEFI POST
  message only? nothing?) — scenario 3 needs a dead member to page someone, not hide;
  document the residual risk if visibility is poor.
- Record data-bay M.2 population (SATA vs NVMe) per unit and how those enumerate.

## 5. DMI serial ↔ Nautobot serial match (answer-service lookup key)

From a live Linux/installer boot on the unit:

```bash
proxmox-auto-install-assistant system-info
```

(or `dmidecode -s system-serial-number`). Confirm the serial the installer would POST
matches the serial recorded in Nautobot for that device.

## 6. X722 firmware LLDP agent (breaks LLDP visibility on the 10G LAG ports)

From a live Linux boot, per SFP+ port:

```bash
ethtool --show-priv-flags eth_X722_PORT | grep disable-fw-lldp
```

Then set `disable-fw-lldp on` and confirm LLDP neighbors appear in the OS. If the
priv-flag is absent, the X722 NIC firmware is too old (support arrived ~FW 3.10 era) —
add NIC firmware to the pinned bundle in §2.

## 7. Security Pack / ThinkShield state (fleet confirmed Security Pack)

Per unit: record ThinkShield claim state and current **motion-detection / tamper
settings**. Key question: what does our existing ESXi-era ship process do about motion
detection? (Years of successful lab-build-then-ship suggests it's already disabled or
handled — codify that answer into the pre-ship job.) Also confirm the SED auto-lock
behavior and portal reactivation procedure with whoever owns ThinkShield today.

## 8. Serial console path (OpenGear)

- Physical rear RJ45 serial port: verify pinout against OpenGear cabling.
- Standardize **115200 8N1** (the physical port's default is 9600; UEFI COM1 default is
  115200 — align both).
- Keep UEFI *Serial Port Sharing* / *Access Mode* = Disable so the physical port stays
  host-owned (XCC SOL remains a secondary path via SSH-to-XCC).
- After a manual PVE install (§10): confirm GRUB + login getty on `ttyS0` through the
  OpenGear, and `qm terminal <vmid>` reaches a VM's `serial0: socket`.

## 9. Secure Boot

Record current state. Plan default: **disable** Secure Boot for the auto-install pipeline
(PVE's signed-shim path exists but complicates unattended installs), TPM 2.0 left
enabled. Confirm the auto-install ISO boots with Secure Boot disabled.

## 10. Switch-side EtherChannel capture (per site type, feeds the conversion runbook)

Port roles are confirmed (decision log #20): mgmt = p3/p4 copper active/passive on
**access ports** (no channel — unchanged at conversion); data = f1/f2 fiber,
channeled. On a representative 9300 stack capture:

- `show etherchannel load-balance` — default is `src-mac`, which polarizes NFV traffic
  onto one member; the runbook standardizes an IP-based hash (e.g. `src-dst-ip`,
  paired with Linux `layer2+3`).
- `show run` of the f1/f2-facing port-channels (expect `channel-group mode on` today)
  and of the mgmt access ports (confirm access VLAN = mgmt VLAN 200).
- **Jumbo (decided: fabric is always jumbo-capable):** capture `show system mtu` per
  stack (default 1500, target 9198). Net-new sites carry it in the switch template;
  for existing sites confirm whether `system mtu` change applies live or needs a
  reload on the fleet IOS-XE release, then schedule proactive raises so it never has
  to happen reactively. Also record the PA jumbo-frames/HA2-MTU config (HA2 sync on
  VLAN 907 is the likeliest real >1500 consumer).
- On the PVE side, record the Linux interface names for p3/p4 (igb/I350) and f1/f2
  (i40e/X722) by PCI path — feeds the platform profile's NIC pinning map.

After any test conversion: `show etherchannel summary` must show flags `P` (bundled)
and `/proc/net/bonding/bond0` a non-zero partner MAC.

Affinity **fully proven on PVE 9.2.2** (lab NUC, Aug 2026), both directions:
`affinity`/`hugepages` refuse every API token — even root's ("only root can set") —
while `qm set --affinity` over root SSH succeeds, pins every QEMU thread at runtime
(verified in `/proc`), and persists across VM stop/start. Host confinement also
demonstrated: `system.slice AllowedCPUs=` moved pvedaemon onto housekeeping cores
while the VM (qemu.slice) stayed on its own set. socat ships installed (console
exposure needs no extra package); ksmtuned is active by default (firstboot disables).
Remaining here, SE350-specific only: NIC IRQ affinity steering on the X722.

## 11. Proxmox VE 9 manual install + burn-in

Manually install PVE 9.x (pin the current point release) on one unit and burn in:
Xeon D-2100 + kernel 6.14-era stability, i40e (X722) and igb (I350) behavior under
sustained traffic, LACP bond formation against the lab switch, a test VM on a
VLAN-aware bridge trunk. This is the host used to develop Phases 1–2 jobs against.

---

### Results capture

For each unit: serial, machine type (7Z46/7D1X/7D27), CPU SKU (fleet is documented as
uniform 16-core D-2183IT — flag any deviation, since the PA-VM serial derives from UUID
**and CPUID**, making same-SKU spares matter), RAM, XCC/UEFI/X722 firmware, EXT-members
yes/no, M.2 layout, ThinkShield state, Secure Boot state.
