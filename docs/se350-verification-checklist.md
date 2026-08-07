# SE350 Platform Verification Checklist (Phase 0)

Hands-on checks against real SE350 units before job development hardens assumptions.
Everything here is read-only or lab-scoped unless marked otherwise. Record results per
machine serial (they feed Nautobot device records and the platform-profile YAML).

Replace `XCC_IP`, `XCC_USER:XCC_PASS` as appropriate. All curl examples are plain GETs
and safe against in-service units.

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

Capture exact spellings/enums for: `OperatingModes_ChooseOperatingMode`
(expect `MaximumPerformance`), the C-state controls, and the
`DevicesandIOPorts_*` console-redirection group (COM1 enable, baud, emulation). Caveat
found in research: preset operating modes lock the individual C-state knobs — combining
Maximum Performance with custom C-states requires `CustomMode` with every knob explicit.

## 4. M.2 boot storage: Marvell 88SE9230 exposure

The plan prefers **ZFS RAID1 across two M.2 boot devices**, which requires the Marvell
adapter to present both disks as plain AHCI/JBOD. Verify in UEFI setup / a live Linux
boot (`lsblk`, `lspci -k`) that the disks appear individually. If the adapter only
presents a RAID volume, the boot-storage design in `answer.toml` changes — do **not**
use the Marvell firmware RAID (UEFI-boot-only fake-RAID that Linux can't health-monitor).
Also record data-bay M.2 population (SATA vs NVMe) per unit.

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

On a representative 9300 stack: `show etherchannel load-balance` (default is `src-mac`,
which polarizes NFV traffic onto one member — the runbook standardizes an IP-based hash,
e.g. `src-dst-ip`, paired with Linux `layer2+3`), `show system mtu` (data path targets
jumbo 9000 to match the legacy LAN-Trunk vswitch), and `show run` of the server-facing
port-channels (expect `channel-group mode on` today). Confirm the port-role wiring
against the reference architecture: p1 = XCC, p3/p4 = 1G copper, f1/f2 = 10G fiber,
cross-connected to both stack members — and decide the Proxmox port-role map
(decision log #20: which ports back `vmbr0` mgmt vs the `vmbr1` data bond). After any
test conversion: `show etherchannel summary` must show flags `P` (bundled) and
`/proc/net/bonding/bond0` a non-zero partner MAC.

Also verify on the PVE test host: `qm set <vmid> --affinity <cpuset>` succeeds with the
privilege-separated API token (not just root@pam) — pinning is baseline for
latency-sensitivity parity (plan §6 #31), and the fallback changes the job design.

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
