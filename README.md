# nautobot-proxmox

Nautobot-driven lifecycle automation for Proxmox VE NFV hypervisors — Lenovo ThinkEdge
SE350 pairs running edge VNF workloads (Palo Alto VM-Series, Cisco Catalyst 8000v,
Catalyst 9800-CL, Cisco SD-WAN edges, Ubuntu jump hosts).

**Status: planning.** No job code yet — this repo currently holds the project
documentation set produced from the initial analysis and research pass (August 2026).
Implementation follows the phased plan below.

## What this project is

We operate pairs of standalone edge hypervisors (no shared storage; redundancy comes from
redundant VM pairs across the two hosts) and today build them with an ESXi + Python-robot
process. This project replicates and improves that process on Proxmox VE 9.x, driven from
Nautobot as the source of truth: a design job turns three operator inputs (site code,
subnets, server 1/2) into complete Nautobot intent, and layered deploy jobs converge real
hardware toward that intent — bare-metal install, host baseline/tuning, LACP + VLAN-aware
bridge networking, golden-image VM deploys with per-VNF day-0 bootstrap, autoboot
ordering, and serial/OpenGear out-of-band access.

A companion bare-metal starter (Nautobot Job that triggers unattended Proxmox installs
via XCC Redfish virtual media, targeting SE455 V3) is being developed in parallel and
merges into this repo per the plan's proposed layout.

## Documentation set

Start here:

| Document | What it is |
|---|---|
| [docs/plan-of-attack.md](docs/plan-of-attack.md) | **The main document.** Assessment & feedback, ESXi→Proxmox translation table, architecture spine, phased plan, proposed repo layout, and the consolidated `[lab-verify]` / `[decision-needed]` list (§6). |
| [docs/decision-log.md](docs/decision-log.md) | Running log of decisions made and still open. |
| [docs/site-reference-architecture.md](docs/site-reference-architecture.md) | The café-model site standard the automation reproduces: VLAN plan, switching/wiring, DIA model, the static→LACP port-channel flip, ESXi host-tweak translation, sizing policy. |
| [docs/se350-verification-checklist.md](docs/se350-verification-checklist.md) | Actionable Phase 0 lab checklist for the SE350 platform (Redfish vmedia functional check, BIOS dump, M.2/AHCI, wiring/EtherChannel capture). |

Reference research (dense, per-dimension findings backing the plan):

| Document | Dimension |
|---|---|
| [docs/research/esxi-to-proxmox-host-networking.md](docs/research/esxi-to-proxmox-host-networking.md) | Host networking & tuning: bonds, VLAN-aware bridges, trunk-to-VM, power/C-states, NIC offloads, serial console, autoboot |
| [docs/research/proxmox-automation-surface.md](docs/research/proxmox-automation-surface.md) | Proxmox API coverage, image import without shared storage, auto-installer, cluster-vs-standalone, storage layout |
| [docs/research/vnf-guest-requirements.md](docs/research/vnf-guest-requirements.md) | Per-guest requirements: PA-VM, C8000v (autonomous + SD-WAN), 9800-CL, Ubuntu; the common day-0 abstraction |
| [docs/research/nautobot-modeling-and-jobs.md](docs/research/nautobot-modeling-and-jobs.md) | Data modeling (one-host Clusters, VNFs as VMs), job architecture, app ecosystem, 2.4-LTM vs 3.x |
| [docs/research/se350-platform-notes.md](docs/research/se350-platform-notes.md) | SE350 hardware, XCC1 Redfish mechanics & licensing, BIOS attributes, Security Pack/ThinkShield, EOL |
| [docs/research/nfv-lifecycle-process.md](docs/research/nfv-lifecycle-process.md) | Process design: intent-vs-imperative split, layering, orchestration placement, cutover strategy |

## Key decisions so far

- **Standalone Proxmox nodes, never clustered** — pairing modeled in Nautobot only
  (proposed; see plan §1).
- **One LACP bond + one VLAN-aware bridge** per host; trunks to self-tagging VNFs via
  untagged/`trunks=` vNICs (proposed).
- **Build on Nautobot 2.4 LTM** (current environment); 3.x upgrade is its own later
  effort, targeted before Phase 4 (decided).
- **PA-VM management: standalone or SCM, no Panorama** (decided).
- **SE350 fleet is the Security Pack variant** — ThinkShield claim/motion-detection steps
  are mandatory in the ship and RMA runbooks (confirmed).
- **XCC licenses are Enterprise fleet-wide** — the no-USB bare-metal track is unblocked
  (confirmed; functional check at fleet firmware level remains in the
  [checklist](docs/se350-verification-checklist.md)).
- **Server-facing port-channels flip static→LACP at Proxmox conversion**, same
  maintenance window — both mismatch directions black-hole (decided; see the
  [site reference architecture](docs/site-reference-architecture.md)).
- **Image repo = the existing nautobot-composer server** (decided).
- **Per-VM realtime tuning is baseline, not deferred** — legacy VMs run ESXi
  latency-sensitivity=high with reservations; Proxmox parity = disjoint CPU affinity +
  `balloon=0` + KSM off (proposed; VM tuning table in the site reference architecture).
- **Two-bridge layout mirrors the legacy vSwitch0/LAN-Trunk split** — mgmt `vmbr0` +
  VLAN-aware `vmbr1` on the 10G LACP bond, jumbo MTU on the data path (proposed).

See the [decision log](docs/decision-log.md) for the full list with status.
