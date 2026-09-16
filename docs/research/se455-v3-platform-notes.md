# Lenovo ThinkEdge SE455 V3 Platform Notes

> Reference document from the platform research pass (2026-09-15). Claims were
> verified against Lenovo's product guide (LP1724, PDF text extract), the XCC2
> REST API reference (pubs.lenovo.com/xcc2-restapi), the XCC2 product guide
> (LP1800), Lenovo's AMD UEFI tuning guide (LP2210) and the pve-installer
> sources current at that time. Hands-on facts still owed by the lab are
> tagged `[lab-verify]`.

## Summary

The SE455 V3 (machine type 7DBY, AMD EPYC 8004 "Siena", 2U short-depth) is
the current-generation successor to the SE350 and the second physical target
of the bare-metal install loop. It is an **XCC2** platform: virtual media is
still driven by PATCHing an `EXT{N}` VirtualMedia member — the same mechanics
the SE350 client already uses — but XCC2 also accepts HTTPS/NFS/CIFS image
URLs, documents the collection under `/redfish/v1/Systems/1/VirtualMedia`, and
gates remote media behind the **XCC2 Platinum** license (fleet-confirmed
present). Storage is the big difference: our units carry four front 2.5"
SATA SSDs — a 480 GB pair and a 1.92 TB pair — behind a ThinkSystem RAID
540-8i / 940-8i, and the two RAID1 mirror sets the admin used to build in
UEFI are now **created out-of-band by the install job through the XCC2's
Redfish storage API** (decision #50): boot = the smaller pair, ext4 + LVM-thin
as on the SE350; data = the larger pair, LVM-thin at firstboot. The box has **no onboard Ethernet** (OCP 3.0 adapter only) and **no
built-in serial port** (optional COM bracket; SOL otherwise). The project now
carries a DeviceType, an install profile, discovery/verification support and
a BIOS-policy skeleton for it; what remains is the on-unit verification pass.

## Key Findings

- **Identity**: `ThinkEdge SE455 V3`, machine type 7DBY, XCC2 (ASPEED AST2600),
  dedicated 1GbE RJ-45 XCC port on the front, AMD EPYC 8004 (8–64 cores,
  6 DDR5 channels). DMI `product_serial` is the Lenovo serial exactly as the
  XCC reports `Systems/1.SerialNumber` — the answer-service lookup key.
- **XCC2 virtual media**: Lenovo documents `PATCH
  /redfish/v1/Systems/1/VirtualMedia/EXT{N}` with `{Image, Inserted,
  WriteProtected}` (plus `UserName`/`Password`/`VerifyCertificate` for
  authenticated/HTTPS sources); "only EXT{N} media via HTTP, HTTPS, NFS or
  CIFS"; `RDOC{N}`/`Remote{N}` cannot be inserted. Member IDs are
  `EXT1..EXT4`, `RDOC1..2`, `Remote1..4` — identical to XCC1, so the dual-mode
  client's EXT-first branch is the path that runs on XCC2 too.
- **License**: remote media (and remote KVM, SSH serial redirection, boot
  capture, System Guard) requires **XCC2 Platinum** — optional on SE455 V3
  base models (upgrade 7S0X000KWW / feature SBCV, CTO feature BRPJ). The
  fleet always carries it (decision log #49). XCC2 exposes
  `/redfish/v1/LicenseService/Licenses/XCC2_Platinum` with `Status.State`;
  the discovery job reads it. Power control and boot-source override are
  Standard features, so a license-free fallback exists: boot-once `Pxe` via
  Redfish + power on, against the lab netboot server.
- **Boot storage**: the product guide's M.2 options ("ThinkSystem M.2 RAID
  B540i-2i SATA/NVMe Adapter", Broadcom SAS3808N; "M.2 SATA/x4 NVMe 2-Bay
  Adapter", pass-through) are not what our units use. The front 2.5" bays
  hang off either the onboard SATA controller (JBOD only, no RAID) or one of
  exactly two adapters: **RAID 540-8i** (SAS3808, cacheless, RAID 0/1/10) or
  **RAID 940-8i 4GB Flash** (SAS3916); a 440-8i HBA is the third option. The
  admin's UEFI mirror sets prove a 540 or 940 is fitted.
- **Out-of-band RAID configuration**: both adapters are managed by the XCC —
  web UI, OneCLI `raid` over `--bmc`, and Redfish `POST
  /redfish/v1/Systems/1/Storage/{RAID_Slot<n>}/Volumes` with `Name` (≤ 15
  chars), `RAIDType` and `Links.Drives`; drives expose `CapacityBytes`,
  `Links.Volumes` and `Oem.Lenovo.DriveStatus` (`Unconfigured good`,
  `Online`, `JBOD`, ...); volumes expose `Id`, `Name`, `RAIDType`,
  `CapacityBytes`, `Links.Drives`, `Oem.Lenovo.Bootable`. Lenovo publishes
  the opposite for the 5350/9350 families (HT513901: no out-of-band tools at
  all) — not offered on the SE455 V3 bays. The XCC shows RAID inventory only
  while the host is powered on ("If the system is powered off, power it on
  in order to view the RAID information"), so an automated step must power
  the host on first — a one-time boot into UEFI Setup parks it safely.
- **Boot-pair rule**: "boot is always the smaller pair" is now a Redfish
  selection, not a udev guess: the layout step sorts the adapter's
  unconfigured drives by `CapacityBytes`, takes the two smallest for `boot`
  (created first → the adapter's first VD) and the two largest for
  `datastore`, refusing on unequal or ambiguous sizes. In Linux the boot VD is
  then `ID_PATH pci-...-scsi-0:2:0:0` (megaraid_sas exposes VDs as SCSI
  targets on channel 2, target = VD number), which the profile pins. The
  installer's identity POST carries no disk data
  (`proxmox-auto-installer/src/sysinfo.rs`), so the answer service itself
  cannot pick disks — the pin plus the layout step's ordering guarantee do.
- **Data volume**: created at firstboot as LVM-thin (`datastore/data`) on the
  largest unused, signature-free whole disk — the data VD — and registered
  as the lvmthin storage `datastore` (images, rootdir). A volume group of
  that name found on disk (reinstall) is reused, not rebuilt. The node's
  `vm_storage` CF becomes `datastore`; `import_storage` stays `local`.
  (Boxes without an adapter keep the ZFS alternative: `install.data_pool`.)
- **Networking**: no onboard LOM. OCP 3.0 SFF slot (PCIe 5.0 x16) takes
  Broadcom 5719/57416/57412/57414/57504/57508 or Intel I350/X710/E810 adapters;
  one OCP port can be NC-SI-shared with the XCC. The NIC filter in the answer
  file is MAC-based, so the adapter family does not matter for the install —
  pin the mgmt port's MAC on the Device interface that carries `primary_ip4`.
  Firmware-LLDP quirks differ per family (the SE350's X722 `disable-fw-lldp`
  check is i40e-only and reports SKIP here); Intel E810 (`ice`) has
  `fw-lldp-agent`, Broadcom has none `[lab-verify]`.
- **PXE**: UEFI PXE comes from the OCP adapter's option ROM; the port's UEFI
  PXE must be enabled in Setup. The lab iPXE chain (snponly.efi) needs
  **Secure Boot off**; the product guide states Secure Boot, once enabled,
  cannot be disabled (factory feature BPKR = disabled, BPKQ = enabled) —
  check the state before experimenting. Virtual-media ISO boots work either
  way (PVE's signed shim).
- **Serial console**: no built-in RS-232. Optional "ThinkSystem COM Port
  Upgrade Kit v2" (4Z17A80446, PCIe slot 5 only). Otherwise the console is
  SOL through the XCC2 (IPMI SOL is Standard; SSH serial redirection needs
  Platinum). The OpenGear plan therefore needs the COM bracket or a
  console-server-to-XCC-SOL arrangement.
- **BIOS policy**: every attribute in `bmc/se350_bios.yaml` is Intel-specific.
  AMD ThinkSystem V3 names (LP2210: `OperatingModes_ChooseOperatingMode` =
  MaximumEfficiency/MaximumPerformance/CustomMode,
  `Processors_GlobalC_stateControl`, `Processors_CorePerformanceBoost`,
  `Processors_DeterminismSlider`, `Processors_CPPC`, `Processors_SMTMode`,
  `Processors_DFC_States`, `Memory_NUMANodesperSocket`, `PowerProfileSelection`)
  are the candidates in `bmc/se455v3_bios.yaml` — LP2210 covers the
  SR635/655/665 V3, **not** the SE455 V3, so the skeleton stays
  `verified: false` until the discovery dump of a real unit. The SE455 V3
  does not support power capping.
- **Lockdown**: System Lockdown Mode is optional; units ordered "ThinkShield
  Key Vault Portal managed" (BYBR) ship locked and need activation (mobile
  app on the front USB, or XCC Internet activation); the DCSC default (BYBQ,
  XCC-managed) boots at first power-on. Replacement-unit runbook item.
- **OS support**: Lenovo lists Windows/RHEL/SLES/ESXi only; Proxmox is not
  OSIG-certified for the SE455 V3 (same posture as the SE350). Kernel support
  for EPYC 8004, the onboard SATA (ahci) and the OCP families (bnxt_en, igb,
  i40e, ice) is mature in PVE 9.

## Confirmed from a hand-built unit (tester dump, 2026-09-16)

`proxmox-auto-install-assistant device-info disk` on an SE455 V3 installed
the old way (mirror sets made in UEFI) settled the assumptions above:

- **Adapter**: `ID_MODEL=RAID_540-8i`, `ID_VENDOR=Lenovo` on both virtual
  drives — the RAID 540-8i, XCC-manageable. The model string is identical for
  every VD, so it can never discriminate them.
- **VD ↔ SCSI target**: boot VD = `sda`, `ID_PATH=pci-0000:02:00.0-scsi-0:2:0:0`
  (GPT, the installed OS); data VD = `sdb`, `...-scsi-0:2:1:0`. megaraid_sas
  exposes VDs on channel 2 with target = VD number, exactly what the profile
  pins (`*-scsi-0:2:0:0`), and the admin-made order was boot first.
- **Identity properties**: `ID_WWN` is the same on both VDs — it is the
  adapter's NAA prefix; the per-VD identifiers are `ID_SERIAL`,
  `ID_SERIAL_SHORT` and `ID_WWN_WITH_EXTENSION`.
  A per-node pin, if ever needed, must use `ID_SERIAL`, never `ID_WWN`.
- **Data VD carried LVM** (`ID_FS_TYPE=LVM2_member`) on the hand-built unit —
  the same shape the firstboot `data_volume` step produces. On such units
  the step leaves the signature alone; it registers storage only when the
  volume group is named `datastore` (rename or recreate to converge).
- **NICs**: four Broadcom BCM5719 1GbE ports (`tg3`, `pci-0000:01:00.0-3`),
  an Intel E810-XXV-2 OCP 3.0 25GbE pair
  (`ice`, `pci-0000:41:00.0/1`), and — notably — the **XCC's USB
  Ethernet-over-USB port** (`cdc_ether`, `XClarity Controller`, with a
  locally administered MAC), which the installer lists like any NIC. Consequences: pin the mgmt MAC on the Device (the answer service's
  unpinned fallback now skips link-down and locally administered NICs, but a
  pin is the only deterministic choice); the E810 has a firmware LLDP agent
  (`ice` priv-flag `fw-lldp-agent`), the 800-series counterpart of the SE350's
  X722 issue — the host-verification job now reports it.
- **Interface naming**: the hand-built system runs PVE 9's pinned names
  (`nic0…nic6` via `50-pmx-nicN.link`, the installer's own pinning). Decided
  2026-09-16 (#51): the profile enables pinning too, and the answer service
  adds a MAC→name mapping from the Device's interfaces, so the management
  port comes up as `mgmt` (or whatever the SoT calls it) and unmapped ports
  as `nic<N>`. The installer numbers `nic<N>` in `ip address show` order
  over every physical link — the XCC's USB NIC included — which is why the
  SoT mapping, not the index, is what should carry meaning.

## What the project adds for it (2026-09-15)

| Piece | Where | Note |
|---|---|---|
| DeviceType `ThinkEdge SE455 V3` (Lenovo, 2U) | `jobs/design/bootstrap_schema.py` | Model string = profile key |
| Install profile | `bmc/profiles/thinkedge-se455-v3.yaml` | `storage` section (boot/datastore RAID1 by capacity), ext4 boot pinned by `ID_PATH *-scsi-0:2:0:0`, `data_volume` (LVM-thin) for the data VD, `redfish-vmedia`, http+https ISO URLs |
| Out-of-band RAID layout | `jobs/lib/storage_layout.py`, `jobs/baremetal/apply_storage_layout.py`, install job step | spec parsing, capacity-based drive planning (refuses on ambiguity), keep-by-name, power-on into UEFI Setup, Redfish volume create + wait; dry-run job; unit tests in `tests/test_storage_layout.py` |
| Answer file `filter-match` | `answer.toml.j2`, `app.py` | profile `install.filter_match` (any/all) |
| Firstboot data storage step | `firstboot.sh.j2`, `app.py` | `install.data_volume` (LVM-thin on the data VD, VG reused on reinstall) or `install.data_pool` (ZFS mirror for JBOD boxes); both run after the credentials phone-home and refuse on ambiguity |
| Redfish client | `jobs/lib/redfish_discovery.py` | VirtualMedia link fallback to the ComputerSystem; `licenses()` (Platinum); `storage_drives()`; RAID surface `storage_controllers()` / `controller_drives()` / `controller_volumes()` / `create_volume()`; generic `set_boot_once()` |
| Discovery job | `jobs/baremetal/discover_platform.py` | logs licenses + drives + suggested boot filter; AMD BIOS fragments; `licenses.json`/`drives.json` attachments |
| Host verification job | `jobs/baremetal/verify_host.py` | installer-exact matcher incl. `filter_match`, expected disk count per filesystem/raid, data-pool preflight, any-bus ID_PATH parsing |
| Install job | `jobs/baremetal/install_node.py` | ISO-URL scheme guard from `delivery.iso_url_schemes` (XCC1 default http-only) |
| BIOS skeleton | `bmc/se455v3_bios.yaml` | unverified AMD attribute candidates |

## Storage layout (decision log #50)

```
RAID 540-8i / 940-8i (XCC2 Redfish, created by the install job before the installer boots)
  VD 0 "boot"      RAID1  480G  ← the two smallest unconfigured drives, created first
  VD 1 "datastore" RAID1  1.92T ← the two largest
Linux (megaraid_sas):
  /dev/sda  ID_PATH pci-…-scsi-0:2:0:0  → installer: ext4 + LVM-thin (local, local-lvm)   [profile disk_filter]
  /dev/sdb  ID_PATH pci-…-scsi-0:2:1:0  → firstboot: VG datastore / thin pool data → lvmthin storage "datastore" = vm_storage
```

Why hardware RAID and not ZFS here: the adapter is present anyway, the team's
convention is mirror sets on the adapter, and keeping the boot volume a single
disk keeps decision #27 (ext4 + LVM-thin) and the deploy jobs' storage model
unchanged. The Redfish step is what removes the human from the loop. ZFS
mirrors over raw disks (`install.data_pool`) remain the mechanism for boxes
without an adapter.

## Getting a shell on the installer (`[lab-verify]` tool for the disk filter)

From `unconfigured.sh` in the PVE 9.2 ISO:

- **Every installer mode starts a root shell on tty3** — `Ctrl+Alt+F3` on the
  console (XCC2 remote console: send it from the keyboard/macro menu). tty2
  carries the installer's stderr.
- **A failed automated install drops into a debug shell on tty1** when the
  answer file has `reboot-on-error = false` (ours does): "Auto-installation
  failed … Installation aborted - unable to continue (type exit or CTRL-D to
  reboot)".
- **Debug mode before anything runs**: add `proxmox-debug` to the kernel line
  (the prepared media's `debug`/`debugtui` boot entries carry it; for the
  automated entry press `e` in GRUB — or edit the iPXE `auto` stanza — and
  append `proxmox-debug` next to `proxmox-start-auto-installer`). The
  installer pauses at a shell ("type 'exit' or press CTRL + D to continue").
- **Serial**: the `serial`/`serialdebug` entries put the console on
  `ttyS0,115200` — usable over XCC2 SOL (`ipmitool -I lanplus -H <xcc> -U …
  sol activate`, a Standard feature) once UEFI console redirection is on.

Useful commands once in the shell:

```bash
cat /tmp/fetch_answer.log /tmp/auto_installer.log      # why the fetch/parse failed
proxmox-auto-install-assistant device-info -t disk     # every disk's udev properties (ID_MODEL, ID_PATH, ID_SERIAL)
proxmox-auto-install-assistant device-match disk ID_MODEL='*480*'   # must print exactly the boot pair
proxmox-auto-install-assistant device-info -t network  # NIC udev properties (ID_NET_NAME_MAC)
proxmox-fetch-answer http > /run/automatic-installer-answers   # re-fetch by hand; then exit to continue
```

## Open items `[lab-verify]`

- The Redfish volume create on the real 540/940: response code, how long
  the new volume takes to appear, and the `Id`/`Name` the adapter assigns
  (the layout step keys on `Name`).
- The boot pin is confirmed on the hand-built unit (VD 0 = target 0 = sda);
  still to confirm: that a volume the step creates first on a fresh adapter
  lands on target 0 as well (it should — targets are assigned in creation
  order on a clean adapter).
- Units whose volumes were made by hand: the step adopts them by role
  (RAID1 over the two smallest → `boot`, over the two largest →
  `datastore`) — confirm against a real hand-built unit's Redfish view that
  the drive links and RAIDType read as expected.
- The firstboot LVM-thin step on the data VD, and the reinstall path reusing
  the volume group.
- The VirtualMedia link on the Manager resource: present or not on XCC2
  (client falls back to the ComputerSystem either way).
- BIOS attribute dump → fill `bmc/se455v3_bios.yaml`.
- Secure Boot factory state; OCP port PXE enablement; SOL console path.
- Which OCP adapter the units carry, and its firmware-LLDP behaviour vs the
  LACP trunk.

## Sources

- https://lenovopress.lenovo.com/lp1724-thinkedge-se455-v3-server (and the LP1724 PDF text)
- https://lenovopress.lenovo.com/lp0769-thinksystem-m2-adapters
- https://lenovopress.lenovo.com/lp1800-lenovo-xclarity-controller-2-xcc2
- https://pubs.lenovo.com/xcc2-restapi/insert_eject_virtual_media_patch
- https://pubs.lenovo.com/xcc2-restapi/virtual_media_properties_get
- https://pubs.lenovo.com/xcc2-restapi/collection_virtual_media_get
- https://pubs.lenovo.com/xcc2-restapi/license_properties_get
- https://pubs.lenovo.com/xcc2-restapi/update_next_onetime_bootconfig_patch
- https://pubs.lenovo.com/xcc2-restapi/resource_volume_create_volume_post
- https://pubs.lenovo.com/xcc2-restapi/vol_managed_by_storage_controller_get
- https://pubs.lenovo.com/xcc2-restapi/drives_managed_by_storage_controller_get
- https://pubs.lenovo.com/xcc2/raid_setup
- https://lenovopress.lenovo.com/lp1552-thinksystem-raid-540-545-pcie-gen4-12gb-adapters
- https://support.lenovo.com/us/en/solutions/ht513901-out-of-band-raid-configuration-is-not-possible-for-thinksystem-raid-5350-8i-9350-8i-and-9350-16i-lenovo-thinksystem
- https://pubs.lenovo.com/lxce-onecli/onecli_miscellaneous_raid_command
- https://raw.githubusercontent.com/lenovo/python-redfish-lenovo/master/examples/lenovo_create_raid_volume.py
- https://lenovopress.lenovo.com/lp2210-tuning-uefi-settings-5th-gen-amd-epyc-processor-servers
- https://pve.proxmox.com/wiki/Automated_Installation
- https://git.proxmox.com/?p=pve-installer.git (proxmox-auto-installer/src/{utils,sysinfo}.rs, unconfigured.sh)
