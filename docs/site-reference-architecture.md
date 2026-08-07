# Site Reference Architecture (Café Model)

Captured from internal design documentation (August 2026). This is the as-built standard
the automation must reproduce — the design job's naming/IPAM/VLAN templates and the
network profiles derive from this document. Where the legacy ESXi standard and the
Proxmox target differ, the difference is called out explicitly.

## Site model overview

A regional/café office runs a pair of Lenovo SE350 NFV servers hosting:

- **Dual PA firewalls (HA pair)** — all L3/SVIs for the site live here; ECMP enabled to
  use both DIAs; dual IPsec Service Connectors to Prisma Access Cloud (active-active
  across both DIAs) for AWS resource access and remote-management ingress.
- **Dual Ubuntu servers** — network management functions / testing (the jump hosts).

Supporting services live in AWS, not on site: corporate DHCP/DNS, Cisco ISE (auth for
the mgmt VLAN), Cisco CSSM smart-license satellite, and the OpenGear Lighthouse server
for remote OOB. Site-local DHCP for mgmt/IoT/users runs on the PA firewalls with Google
DNS. APs run from AWS-hosted controllers and can operate autonomously if the controllers
are unreachable — **the café model deploys no local WLC**; the 9800-CL VNF applies to
other site types.

Users are wireless-first (single CCF Guest SSID, known PSK); switch LAN ports are a last
resort (VLAN 220).

## VLAN / IPAM standard

Switches are **L2-only**; every VLAN's default gateway is a PA firewall interface, each
in its own security zone:

| VLAN | Name (typical) | Purpose | PA gateway |
|------|----------------|---------|------------|
| 200 | Prisma-Mgmt | Network mgmt devices (fw/hypervisor/aps/ups) | e1/1 |
| 210 | Prisma-IoT | IoT (printers/video/HVAC/BMS) | e1/2 |
| 220 | Prisma-Users | Users (PCs, phones, tablets — wireless-first) | e1/3 |
| 904 | DIA2 (usually Comcast) | Secondary DIA handoff | PA DIA interface |
| 905 | DIA1 (usually CCF/Lumen) | Primary DIA handoff | PA DIA interface |
| 907 | Palo-HA2 | PA HA link between the two VM-Series across hosts | — |

Notes for the design job:

- Per-site naming drift exists in the legacy estate (e.g. a site's VLAN 904 named
  `DIA2-Verizon`, 905 named `DIA1-Comcast` while the written standard says primary =
  CCF/Lumen on 905). The design job should emit the standardized names and the audit job
  should flag drift, not inherit it.
- VLAN 907 (PA HA) rides the switch fabric between the two hosts — it must be in the
  trunk/`bridge_vids` set even though no user traffic uses it.

## Connectivity

- Two 1 Gb DIA circuits per site from different providers (one CCF/Zayo circuit where
  possible, plus Comcast/Lumen); balanced for performance; 2×1G has proven sufficient.
- Enterprise SMF fiber preferred; MMF supported in special circumstances.
- DIAs land on the main switch stack as L2 VLANs (904/905) and are trunked through to
  the PA VMs — the DIA VLANs are just two more members of the trunk to the NFV servers.

## Switching

- Cisco Catalyst 9300-48UXM (Network Essentials) stacks, `swi<site>` naming, minimum
  2 members (data + power stack cabling), up to 8.
- Server naming: `nfv<site>1` (LEFT) / `nfv<site>2` (RIGHT); UPS `ups<site>1`.
- SE350 physical connection map (each server cross-connects to both stack members):
  - **p1** — XCC (XClarity) management port
  - **p3/p4** — 1 GbE copper (I350), used by ESXi today (labelled "esx")
  - **f1/f2** — 10 GbE SFP+ fiber (X722), MMF
  - Exact port roles under Proxmox (mgmt vs VM dataplane vs unused) are a Phase 0
    verification item — see the decision log.
- **Port-channel modes today:** `channel-group mode on` (static EtherChannel) on
  server-facing ports, `mode active` (LACP) between switch stacks. Mode-on exists
  because the ESXi *standard* vSwitch supports only static teaming (IP-hash); LACP
  would have required a Distributed vSwitch.

### Proxmox change: static → LACP (coordinated flip)

Linux bonding supports LACP natively, and Proxmox's documented recommendation is
`bond-mode 802.3ad` when the switch supports it. The plan adopts 802.3ad with
`mode active` on the switch — matching what the stacks already do between themselves —
because static mode-on forwards onto anything with link (miimon can't detect a
mis-patched cable or wrong-channel port), while LACP verifies channel membership per
link before bundling.

**The flip is unforgiving in both directions and must land in the same maintenance
window as the server conversion:**

- Linux `802.3ad` bond into `mode on` ports → LACP never negotiates; the Cisco side
  hashes ~half the southbound flows onto a member the bond won't accept traffic from —
  a *partial, hash-dependent* black hole (the dangerous "it mostly works" symptom).
- Static `balance-xor` bond into `mode active` ports → Cat9k suspends members that send
  no LACPDUs (`%ETC-5-L3DONTBNDL2`) — a *total* black hole by default.

Verification after the flip: `show etherchannel summary` shows the port-channel flags
`P` (bundled), and `/proc/net/bonding/bond0` shows a non-zero partner MAC.

**Hash policy:** avoid MAC-based hashing on both sides — an NFV edge concentrates
traffic behind a couple of router/firewall MACs, so MAC hashing polarizes onto one
member. Note the Cat9300 *default* load-balance is `src-mac`: capture
`show etherchannel load-balance` on-box and set an IP-based policy. Sane pairing:
Linux `layer2+3` (Proxmox-documented, 802.3ad-compliant) ↔ Cisco `src-dst-ip`;
`layer3+4` ↔ `src-dst-mixed-ip-port` if L4 spread is wanted (minor caveat: `layer3+4`
can reorder fragmented flows).

## Legacy ESXi host tweaks → Proxmox translation

The ESXi install robot applied these at build time. Verification against vSphere
documentation showed the first two **assert ESXi defaults** rather than change behavior
— there is no exotic host state to port.

| ESXi robot step | What it actually does | Proxmox equivalent |
|---|---|---|
| Enable TCPIP LRO | vmkernel-stack LRO for *host-terminated* TCP (mgmt/storage) — default-on in ESXi; does not touch the VM dataplane | Nothing to do: Linux GRO is default-on for the mgmt path — keep it there. On VNF dataplane bridge ports, LRO is kernel-auto-disabled when bridged, and GRO/TSO should be explicitly off (coalescing adds latency/jitter into guest routers; standard NFV host guidance) |
| Enable Network Queue Pairing | NetQueue RX/TX queue-thread pairing — also an ESXi default | virtio-net multiqueue: `queues=<guest vCPUs>` on dataplane vNICs (vhost gives each queue pair its own kernel thread); guests activate via `ethtool -L` (PAN-OS/IOS-XE handle their own) |
| Power policy = High Performance | ESXi host power policy | Firmware `OperatingModes_ChooseOperatingMode=MaximumPerformance` (Redfish BIOS) + kernel C-state cap; governor already `performance` on intel_pstate |
| VM autostart | Per-VM autostart + delay | `onboot=1` + `startup=order=N,up=S` |
| SSH shell + serial console shell | ESXi TSM/TSM-SSH services | Break-glass root SSH key (firstboot) + GRUB/getty serial console on ttyS0 → OpenGear |

### VM-level tuning standard (legacy, confirmed Aug 2026)

The team confirmed the ESXi estate applies per-VM realtime tuning, not just host
settings. On ESXi, `Latency Sensitivity = High` grants exclusive pCPU access (each vCPU
owns a physical CPU), bypasses the VMkernel scheduler, and disables vNIC coalescing —
and requires the CPU/memory reservations the robot also sets. **Parity on Proxmox
therefore requires per-VM measures, not only host tuning:**

| ESXi VM setting | Proxmox equivalent |
|---|---|
| Cores-per-socket sized so virtual sockets = physical sockets (SE350: always 1) | `sockets: 1, cores: N` — explicit in every VNF profile |
| CPU min / memory min reservations ("pins up resources") | KVM/Proxmox has **no ESXi-style CPU reservation primitive** (no MHz floor / admission control — `cpuunits` is a relative weight, `cpulimit` a cap). The reservation guarantee is reproduced by policy: the **no-oversubscription guardrail is the admission control** (≤1 vCPU per budgeted physical core means every vCPU has a core's worth of capacity by construction). Memory side: `balloon: 0` (fully committed at start, no overcommit path) + **KSM disabled host-wide** |
| `sched.cpu.latencysensitivity = high` (exclusive pCPU affinity, scheduler bypass, coalescing off) | **Two-tier.** Baseline (no per-VM pinning): admission control as above + host-side confinement — host services restricted to the 2–4 housekeeping cores (`system.slice`/`user.slice` `AllowedCPUs=`), NIC IRQ affinity steered to the same cores, C-states capped, `cpuunits` weighting VNFs above utility VMs. Escalation (on measured jitter only): per-VM `affinity: <cpuset>` with disjoint core sets computed from the same core budget — root@pam-only option, applied via the `HostBaselineJob` root-context path. Rationale: with no oversubscription and ~2×1G of edge throughput, exclusive pinning rarely earns its operational cost; the Phase 2 lab jitter/latency soak is the decision gate |
| All throughput caps off | No `cpulimit`, no vNIC `rate=`, no disk I/O throttles in any profile |
| Guest desktop autolock off | Ubuntu golden-template / cloud-init setting |

Still deliberately deferred (not part of legacy parity): hugepages + memory locking
(root@pam-only via API anyway) and emulator-thread isolation — revisit on measured
jitter.

## Legacy robot function map

Function-by-function mapping of the ESXi deployment robot onto the Proxmox/Nautobot
design. Where a function has no equivalent, the reason is given.

| Robot function / behavior | Proxmox / Nautobot-job equivalent |
|---|---|
| `verify_vswitch` — idempotent vswitch check; on create: MTU 9000+, CDP enabled, allow promiscuous / MAC change / forged transmits | `DeployHostNetworkJob` verify/converge of bridges. **MTU 9000 on bond + data bridge** (VM vNICs use virtio `mtu=1` to inherit the bridge MTU); `lldpd` with CDP mode enabled for neighbor visibility; the three ESXi security-policy relaxations need no equivalent — a Linux bridge doesn't filter promiscuous/forged-MAC by default (keep `firewall=0` on dataplane vNICs) |
| Two vswitches: `vSwitch0` (host mgmt) + `LAN-Trunk` (VM networking) | **Two bridges**: `vmbr0` (mgmt) + `vmbr1` (VLAN-aware "LAN-Trunk" on the 10G LACP bond). Resolves the logical half of the port-role question — remaining is which physical ports carry each |
| `find_and_config_pg` — port groups `LOC_NAME_VlanNum`, tagged packets → specific vNICs | Port groups have no Proxmox object; the mapping lives in Nautobot: VLAN objects named by the same `LOC_NAME_VlanNum` convention, VMInterface↔VLAN assignments, rendered to `net tag=`/`trunks=` at deploy. The audit job replaces "check the port group exists" |
| Load VMDK/VMX, edit in place: map network names, scrub identity (UUID etc.) that must not duplicate | Identity handling **inverts**: fresh qcow2 import + VM config generated from Nautobot intent (`smbios1 uuid=`, pinned MACs, bridge/tag) — nothing to scrub. Golden images must be identity-clean (machine-id reset / cloud-init for Ubuntu; vendor images ship clean) |
| VMDK zeroed-thick format | Thin provisioning (LVM-thin/ZFS/qcow2), sanctioned by the written policy; thin is also what enables native snapshots. Preallocation options exist if ever needed |
| `create_vm` — autostart automatic power-on for all VMs (default delay 15) | `onboot=1` + `startup=order=N,up=15` (15 s as the default `up`, per-profile overrides) |
| vNIC driver vmxnet3 | virtio-net (+ `queues=<vCPUs>` on dataplane NICs) |
| Sanity checks: target server name must match; datastores checked for space and correct targets | `DeployVmJob` pre-flight: Nautobot Device ↔ Proxmox node hostname **and** DMI serial match (refuse on mismatch); storage ID exists, is the intended target, and has capacity — all via API before any write |
| Final alert with 30 s bail-out window + host report | Replaced with something stronger: `DryRunVar` preview (explicit diff of what will happen) + approval gating on field-targeted jobs — an affirmative confirmation instead of a countdown |
| SSH shell enabled | Break-glass root SSH key (firstboot) |
| Console → serial; host firewall opens per-VM remote serial ports (VM consoles via telnet to host TCP ports) | Host: GRUB/getty on ttyS0 → OpenGear. Per-VM: `serial0: socket` + **socat/ser2net systemd units generated from the VM list, exposing each socket on a TCP port** bound to the mgmt VLAN and firewalled to OpenGear/mgmt sources — preserving the existing telnet-to-port workflow; `qm terminal` remains the native path |
| LROEnabled, FeatPairEnable, power high performance | See host-tweak table above (ESXi defaults / multiqueue / BIOS + C-states) |
| Backup tools added for CLI snapshots | Native: `qm snapshot` / `vzdump` are built in (thin storage makes them work); Proxmox Backup Server deferred |

## Compute sizing & oversubscription policy

- Fleet is uniform: SE350, 16-core Xeon D-2183IT (32 threads), 256 GB RAM. (Uniformity
  also neutralizes the PA-VM CPUID licensing concern for RMA swaps.)
- Written policy: *oversubscription strictly forbidden — used CPU/RAM must not exceed
  actuals; storage may be thin-provisioned.*
- Interpretation for the guardrail jobs (per verified NFV practice): count **physical
  cores, not SMT threads** — budget = 16, with 2–4 cores reserved for the
  host/housekeeping (vhost threads, bridge, mgmt), leaving ~12–14 cores of VNF vCPU
  budget; SMT siblings absorb housekeeping but are never counted as VNF capacity. RAM
  budget = 256 GB minus host overhead and the pinned ZFS ARC cap. Thin provisioning
  stays fine (LVM-thin/ZFS/qcow2).
