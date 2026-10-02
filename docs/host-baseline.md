# Host baseline (L1/L2): from the tester's post-deploy script to a SoT-driven job

> Design note, 2026-10-02. Status: conventions **Decided** (decision log #54),
> job design **Proposed**. Source: the tester's `proxmox-post-deploy.sh`,
> which captures what was done by hand on the test node after an
> `answer.toml` install, and Brian's answers to the open questions it raised.

## What the script established

The hand-built fleet node differs from a fresh loop install in eight ways:
lldpd + snmpd installed; serial console on GRUB and a getty; a ZFS ARC
limit; an `snmpd.conf` with a community and SNMPv3 users; the data thin pool
registered as PVE storage `DataDrive`; an Active Directory realm (`EQT-AD`)
with a daily sync job and an admin-group ACL; `datadog` and `pdm` service
accounts with API tokens; and the bond + bridge network (management
active-backup bond under `vmbr0`, data bond under a VLAN-aware `vmbr1`).
The script is idempotent shell and its field knowledge is kept; what moves
is *where the data lives* and *how secrets travel*.

## Placement

| Step | Home | Why |
|---|---|---|
| Packages, serial console, ARC limit | Firstboot (profile-driven) | One-shot, reboot-bound, differs per DeviceType (the SE455 V3 has no serial port without the COM bracket) |
| Data thin pool + storage | Firstboot — already built | Names are profile data: `DataDrive` / `big-vg` / `big-lv` (#54) |
| SNMP, AD realm + sync job + ACL, root email | **Host Baseline job** | Re-runnable when a community or bind password rotates |
| Service accounts + tokens | Host Baseline job | Tokens captured by the job into Secrets, never printed |
| Bond + bridge network | Host Baseline job, last step | Needs the safe-apply dance and the SoT bond model |

## SoT model (contract §3/§4 additions, to be settled with the job)

- **Bonds and bridges are Nautobot interfaces.** `bond0`/`bond1` are LAG
  interfaces with the physical ports as members; `vmbr0`/`vmbr1` are bridge
  interfaces whose port is the bond (Nautobot's native `lag` and `bridge`
  fields). The management IP sits on `vmbr0`. Per-LAG custom fields carry
  `lag_mode` (`802.3ad` for the data bond, `active-backup` for management),
  `lag_xmit_hash` and `lag_primary`; the data path carries `mtu 9000` on the
  interface records (#54: LACP + jumbo are the target; the switch side is a
  separate work item).
- **The install NIC is derived, not typed.** The answer service walks from
  the primary IP's bridge to its bond to the bond's primary member, which is
  the port that must carry link during the install and whose MAC feeds the
  NIC filter and the pinned name. This removes the "fresh install lands on
  nic2" surprise: the install port is by construction a member of the
  management bond, so the baseline's move from port to bond keeps the same
  cable active.
- **Fleet and site settings live in config contexts** (by Location and role,
  per-device overrides): `ad` (realm, domain, servers, base/bind DN, user and
  group filters, sync name/schedule, admin group, `default_realm: true`),
  `snmp` (contact; `location` is taken from the device's Nautobot Location,
  #54; v3 user names), `root_email`, `zfs_arc_max_bytes`, `serial_console`
  (`unit`, `speed`, enabled per DeviceType), and `service_accounts`
  (`[{user, token, role}]` — `datadog@pam` → `PVEAuditor`, `pdm@pve` →
  `Administrator` per the current PDM guidance, #54).
- **Secrets are Secret records** the bootstrap pre-creates: `ad_bind_password`,
  `snmp_community`, and one record per SNMPv3 credential. Tokens the job
  creates go into per-node SecretsGroups named like the existing
  `<node>-proxmox` (`<node>-datadog`, `<node>-pdm`: Generic/Username = token
  id, Generic/Secret = value).

## The job: `Host Baseline (SoT-driven)`

- **Transport**: SSH as root with the existing `host_ssh_*` Secrets — the
  path the plan reserves for root-only work, and the one the verification
  job already uses. The `svc-nfv@pve!deploy` token stays least-privilege.
- **Identity first**: hostname and DMI serial must match the Device (same
  rule as the BMC guard) before anything is written.
- **Render from the SoT**: config context + interfaces → an applier script
  (the tester's script refactored into `ensure_*` functions emitting JSON
  lines) uploaded and run step by step; secrets pass through the session
  environment, never argv; nothing secret is echoed. Each step is idempotent;
  a `dry_run` renders and diffs only (drift detection).
- **AD**: realm created/modified through `pveum` with the bind password from
  the Secret; realm sync job; admin-group ACL; AD set as the default login
  realm (#54); the initial sync is attempted and its failure is a warning.
- **Tokens**: created with `pveum`, captured from the command output inside
  the job, stored into the SecretsGroups above, and never logged. Re-runs
  rotate a token whose Secret is missing (same reasoning as firstboot).
- **Network last, safely**: render `/etc/network/interfaces` from the SoT
  (pinned names, bond modes, MTU), write it staged, diff against the running
  state, apply under a rollback timer (`systemd-run` restores the previous
  file and reloads unless cancelled), reconnect on the expected address,
  cancel the timer, then advance `provisioning_state` to `baseline_done`.
- **Not in scope yet**: KSM, host-service confinement, the LLDP firmware-agent
  oneshot, serial exposure for VMs — the rest of the plan's Phase 3 list.

## Decisions taken (Brian, 2026-10-02) and open items

| # | Item | Outcome |
|---|---|---|
| 1 | Data storage names | **Decided**: `DataDrive` (storage id and RAID volume name), VG `big-vg`, thin pool `big-lv` — the hand-built units' names, so a reinstall reuses their volume group |
| 2 | Data bond | **Decided target**: LACP (`802.3ad`) with MTU 9000; mode and MTU come from the SoT so a site can run static EtherChannel until its switch side flips |
| 3 | Switch configuration | **Open**: whether this deployment owns it — needs its own work item; the job must refuse or stage when the switch side does not match |
| 4 | Token roles | **Decided**: `datadog@pam` → `PVEAuditor`; `pdm@pve` → `Administrator` with privilege separation off, which is what Proxmox Datacenter Manager's own enrollment produces today (its docs do not yet state a minimum); revisit when they do |
| 5 | SNMP location | **Decided**: from the device's Nautobot Location |
| 6 | Serial console on the SE455 V3 | **Open** — see below |
| 7 | Nag-removal hook | The test node carries a `/usr/local/bin/pve-remove-nag.sh` + apt hook that was not in the tarball the tester's assistant received; the project ships its own equivalent in firstboot (no-subscription repo is already set there) |
| 8 | Default login realm | **Decided**: AD |

**Serial console facts for item 6.** The SE455 V3 has no built-in RS-232
port. Options: (a) the "ThinkSystem COM Port Upgrade Kit v2" (4Z17A80446,
PCIe slot 5 only) gives a physical port for the OpenGear telnet-to-port
workflow the site standard relies on; (b) XCC2 serial-over-LAN (IPMI SOL, a
Standard feature; SSH redirection needs Platinum) redirects the UEFI COM
port to the BMC — a different workflow (console server speaks IPMI to the
XCC instead of a cable). Either way the Linux side is `console=ttyS0,115200`
plus a getty, which is why the setting is per-DeviceType profile data and
off unless the port exists. The question for the fleet is which workflow
the sites keep; the discovery job's BIOS dump (`DevicesandIOPorts_COMPort1`,
`ConsoleRedirection`) shows the UEFI side of whichever is chosen.

## Work list

1. Contract: LAG/bridge model, custom fields, config-context schema, Secret
   records (bootstrap). 2. Answer service: derive the install NIC through
   the bond; profile keys for serial console and ARC. 3. Firstboot: packages,
   serial console (GRUB and `proxmox-boot-tool` paths), ARC, nag hook.
   4. The Host Baseline job with its applier, dry-run first, network last,
   on a throwaway node. 5. Docs: runbook, troubleshooting, contract rows.
