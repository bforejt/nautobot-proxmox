# Host baseline (L1/L2): from the tester's post-deploy script to a SoT-driven job

> Design note, 2026-10-02. Status: conventions **Decided** (decision log #54);
> job, firstboot inputs and SoT model **Implemented** (decision #55,
> 2026-10-02) — verified by unit tests, in-image renders and an applier run
> against fake PVE commands; the on-hardware items are `[lab-verify]` (list
> at the end). Source: the tester's `proxmox-post-deploy.sh`, which captures
> what was done by hand on the test node after an `answer.toml` install, and
> Brian's answers to the open questions it raised. The SoT contract for all of
> this is [sot-data-contract.md §4c](sot-data-contract.md#4c-host-baseline-decisions-5455);
> the runbook is [baremetal-install.md](baremetal-install.md#host-baseline-after-verification).

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
| Packages, serial console, ARC limit, nag hook | Firstboot (profile port + config context `host_baseline`) | One-shot, reboot-bound; the port is hardware policy (profile `install.serial_console`), line settings are site facts (config context). The job re-ensures the packages |
| Data thin pool + storage | Firstboot — already built | Names are profile data: `DataDrive` / `big-vg` / `big-lv` (#54) |
| SNMP, AD realm + sync job + ACL, root email | **Host Baseline job** | Re-runnable when a community or bind password rotates |
| Service accounts + tokens | Host Baseline job | Tokens captured by the job into Secrets, never printed |
| Bond + bridge network | Host Baseline job, last step | Needs the safe-apply dance and the SoT bond model |

## SoT model (implemented — contract §4c)

- **Bonds and bridges are Nautobot interfaces.** `bond0`/`bond1` are LAG
  interfaces with the physical ports as members; `vmbr0`/`vmbr1` are bridge
  interfaces whose port is the bond (Nautobot's native `lag` and `bridge`
  fields). The management IP (`primary_ip4`) sits on `vmbr0`; MTU is the
  native `mtu`, the VLAN-aware data bridge is `mode: tagged-all`. Custom
  fields on interfaces: **`lag_mode`** and **`lag_xmit_hash`** (selects the
  bootstrap seeds with the code's choices) on the LAG, and
  **`primary_member`** (boolean) on the member port. Fleet-uniform bond
  settings — `bond_miimon`, `lacp_rate` — come from the config context
  (#54: LACP + jumbo are the target; the switch side is out of scope).
- **The install NIC is derived, not typed.** The answer service (and the
  install job's precheck, with the same parity-tested code) walks from the
  primary IP's bridge to its bond to the bond's member flagged
  `primary_member` (or the only member), which is the port that must carry
  link during the install and whose MAC feeds the NIC filter and the pinned
  name. This removes the "fresh install lands on nic2" surprise: the install
  port is by construction the management bond's primary, so the baseline's
  move from port to bond keeps the same cable active. Ambiguity is a named
  409 before answering.
- **Fleet and site settings live in config contexts** under the single key
  `host_baseline` (merged fleet → site → device; ConfigContextSchema
  `nfv-host-baseline` from the bootstrap): `ad`, `snmp` (`location` is the
  device's Nautobot **Location name**, #54), `root_email`,
  `zfs_arc_max_bytes`, `serial_console` (line settings; the port is the
  DeviceType's), `service_accounts`, `network`, `packages`,
  `remove_subscription_nag`. A section the job applies must be present or
  explicitly `enabled: false` — never silently skipped.
- **Secrets are Secret records** the bootstrap pre-creates: `ad_bind_password`,
  `snmp_community`, and `snmpv3_<user>_auth` / `_priv` for every v3 user a
  config context names. Tokens the job creates go into per-node SecretsGroups
  named like the existing `<node>-proxmox` (`<node>-datadog`, `<node>-pdm`:
  Generic/Username = token id, Generic/Secret = value), written through the
  answer service's mechanism (text files in the shared node-secrets
  directory — the Celery worker's read-write mount, nautobot-composer#66).

## The job: `Host Baseline (SoT-driven)` (implemented)

[jobs/baremetal/host_baseline.py](../jobs/baremetal/host_baseline.py), pure
logic in [jobs/lib/host_baseline.py](../jobs/lib/host_baseline.py), on-node
applier [jobs/lib/host_baseline_applier.sh](../jobs/lib/host_baseline_applier.sh).
Inputs: the Device, `dry_run` (default on), `confirm`.

1. **Gates** — NFV role first; `provisioning_state` `bm_installed` /
   `baseline_done` (a dry run accepts any installed state); every required
   config-context fact and every Secret it names, the network model, the
   worker's write access to the node-secrets directory. All problems are
   reported at once, each naming its fact; nothing has touched the node.
2. **Identity** — SSH as root (`host_ssh_*` Secrets); hostname and DMI serial
   must be the Device's (the BMC rule: trimmed, case-insensitive). Then a
   read-only observation and the plan: roles the SoT names must exist on the
   node, a realm of another type or a sync job bound to another realm refuse,
   every bond/bridge member must be found **by MAC** among the node's physical
   NICs. Still nothing written.
3. **Packages** — `lldpd`, `snmpd` + `host_baseline.packages`; `lldpd` enabled.
4. **SNMP** — `snmpd.conf` rendered in the tester's layout (0600, masked diff
   in the log), SNMPv3 users created the tester's way (`createUser` in the
   persistent file with snmpd stopped) and re-created when their Secrets
   rotate (a salted fingerprint kept root-only on the node), users dropped
   from the SoT purged, snmpd restarted only on change.
5. **AD + root e-mail** — realm added or modified with only the drifted
   options; the bind password goes to PVE's credential file
   `/etc/pve/priv/realm/<realm>.pw` (where `pveum --password` stores it) via
   bash builtins — **never on an argv**; AD as the default realm (#54);
   realm-sync job created/updated; an initial `pveum realm sync` when the
   realm or password changed or the admin group is not there yet (failure =
   warning); the admin-group ACL once the group exists (else a warning); the
   root e-mail. A dry run probes AD with PVE's own `realm sync --dry-run`.
6. **Service accounts** — user, token (created when missing; **rotated** when
   its Secrets are missing or the node rejects the stored value), ACLs
   converged to the SoT (extra roles removed — the hand-built node's
   Administrator on `datadog@pam` becomes PVEAuditor). The token value
   crosses back on one event, is stored and verified against the node API,
   and is never logged.
7. **Network, last** — `/etc/network/interfaces` rendered from the model,
   diffed; on a change: staged as `interfaces.new`, syntax-checked
   (`ifup -a -s -i`), the old file backed up, a `systemd-run` timer armed that
   restores it and reloads unless cancelled, `ifreload -a` run detached; the
   job reconnects on the management IP with a **new** session, cancels the
   timer, checks the applied file and `/proc/net/bonding/*` (wrong mode or
   members = failure; LACP partner/aggregation, link and active-slave state =
   **warnings only**, #54).
8. **State** — `bm_installed` → `baseline_done`.

A **dry run** observes, renders and diffs every step and writes nothing (the
drift report). **Not in scope yet**: KSM, host-service confinement, the LLDP
firmware-agent oneshot, serial exposure for VMs, switch-side configuration.

## Decisions taken (Brian, 2026-10-02) and open items

| # | Item | Outcome |
|---|---|---|
| 1 | Data storage names | **Decided**: `DataDrive` (storage id and RAID volume name), VG `big-vg`, thin pool `big-lv` — the hand-built units' names, so a reinstall reuses their volume group |
| 2 | Data bond | **Decided target**: LACP (`802.3ad`) with MTU 9000; mode, hash policy and MTU come from the SoT per interface so a site can run something else until its switch side flips. Management bond stays `active-backup` with a primary member |
| 3 | Switch configuration | **Decided (2026-10-02)**: out of scope. After the apply the job reads `/proc/net/bonding/*` and reports LACP partner/aggregation state as a **warning**, never a failure; management reachability is what the rollback timer protects |
| 4 | Token roles | **Decided**: `datadog@pam` → `PVEAuditor`; `pdm@pve` → `Administrator` with privilege separation off, which is what Proxmox Datacenter Manager's own enrollment produces today (its docs do not yet state a minimum); revisit when they do |
| 5 | SNMP location | **Decided**: from the device's Nautobot Location |
| 6 | Serial console on the SE455 V3 | **Decided (2026-10-02)**: assume the fleet hardware has RS-232. The DeviceType profile declares the port (`install.serial_console: {unit: 0}` in the SE455 V3 profile), the config context the line settings; with both, firstboot configures GRUB + kernel console and `serial-getty`; without the config-context block it logs and skips — never refuses the install |
| 7 | Nag-removal hook | **Implemented**: the project's own idempotent hook in firstboot — `/usr/local/sbin/nfv-remove-subscription-nag` + `/etc/apt/apt.conf.d/86nfv-remove-subscription-nag` (DPkg::Post-Invoke) patching `proxmoxlib.js` the standard PVE 8/9 way, gated by `host_baseline.remove_subscription_nag`; never fails apt (the hook swallows any error and the script always exits 0) and logs when the pattern is not found |
| 8 | Default login realm | **Decided**: AD |

**Serial console facts for item 6** (background; decided above). The SE455 V3 has no built-in RS-232
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

## Implementation notes and deviations (decision #55)

Where the implementation departs from the design text above, and why:

| Design said | Implemented | Why |
|---|---|---|
| Per-LAG custom fields `lag_mode`, `lag_xmit_hash`, `lag_primary` | `lag_mode` and `lag_xmit_hash` on the LAG as designed; the primary flag is **`primary_member`, a boolean on the member port** | One flag then serves both uses — the bond's `bond-primary` and the install-NIC walk through a multi-port bridge — and the ambiguity rules ("exactly one flagged member") are checkable per group |
| `miimon` / `lacp_rate` per bond | Fleet-uniform values in the config context (`network.bond_miimon`, `network.lacp_rate`) | The three custom fields are the only per-interface facts the decisions name; both values are fleet policy (and `lacp_rate` must match the switch side) — required when a bond / an 802.3ad bond exists |
| "secrets pass through the session environment" | Secrets ride the payload that `bash -s` reads from the SSH session's **stdin**, as locals of one function; they reach files only through bash builtins | sshd's `AcceptEnv` admits only `LANG`/`LC_*` by default, so SSH environment requests would not arrive; stdin keeps them off every argv and off the disk |
| AD bind password via `pveum --password` | Written to `/etc/pve/priv/realm/<realm>.pw`, PVE's own storage path for that option (`pveum realm add` without `--password` deletes the file, so it is written after the add) | `pveum` takes the password only on its argv; writing the credential file keeps it off every process list. Limitation: it depends on that PVE storage path — a `[lab-verify]` item |
| SNMP location from the Location | The Location's **name** (not its path) | Matches the hand-built node's value shape; a reorganised hierarchy above the site never churns every node's monitoring tags |
| Firstboot serial console via GRUB (+ `proxmox-boot-tool refresh` "when present") | A GRUB drop-in `/etc/default/grub.d/nfv-serial-console.cfg` (plus `/etc/kernel/cmdline` on systemd-boot layouts); `proxmox-boot-tool refresh` when the tool **manages ESPs** (`/etc/kernel/proxmox-boot-uuids`), else `update-grub` | On the fleet's ext4/LVM installs the tool is installed but unconfigured and its refresh skips GRUB entirely. The tester's `8250.nr_uarts=8` is kept; `console=` goes on `GRUB_CMDLINE_LINUX` so recovery entries use the serial line too |
| The tester's site values as examples | The contract shows fictional values plus a script-variable → SoT-key map | Both repositories are public: the tester's AD servers, DNs and contacts belong in their Nautobot, not here |

`[lab-verify]` — proven by unit tests, in-image renders and the applier
driven through bash with fake PVE commands, not yet on a node:

1. Firstboot's new steps on a real install: packages from the no-subscription
   repos, the GRUB drop-in + `update-grub` (and `serial-getty@ttyS0`, console
   output after the next reboot) on the SE455 V3 with the COM bracket, the ARC
   file + `update-initramfs`, and the nag patch on PVE 9's
   `proxmox-widget-toolkit` (the pattern was checked against the toolkit's
   current source only).
2. The derived install NIC on a real bond/bridge model, including the
   pinned names the installer applies to the members.
3. The job end-to-end on a throwaway node, dry run first: root SSH + stdin
   applier, AD realm with the bind password via PVE's credential file, the
   sync job and initial sync, the admin-group ACL, token capture into
   `<node>-datadog` / `<node>-pdm` through the composer mount, the SNMPv3
   `createUser` flow on a real snmpd, and the network apply — `ifup -s`
   syntax check, rollback timer, reconnect, `/proc/net/bonding` parsing on
   real bonds, and a mode change on an existing bond (e.g. the tester's
   `balance-xor` data bond becoming `802.3ad`: whether `ifreload -a` applies
   it in place or the bond must be recreated).
4. The bootstrap's new records (interface CFs, Secret records,
   ConfigContextSchema) on Nautobot 2.4.30 and 3.x.

The serial-path gate of the plan ("no management-affecting apply until the
OpenGear serial path to that node is proven") stays an operator rule: the
rollback timer covers a lost management path, the console covers a node that
no longer boots.
