# nautobot-proxmox

Nautobot-driven lifecycle automation for Proxmox VE NFV hypervisors — Lenovo ThinkEdge
SE350 pairs running edge VNF workloads (Palo Alto VM-Series, Cisco Catalyst 8000v,
Catalyst 9800-CL, Cisco SD-WAN edges, Ubuntu jump hosts).

**Status: planning + first tooling.** This repo holds the project documentation set
produced from the initial analysis and research pass (August 2026), plus the first
Nautobot Job: a read-only SE350 platform discovery that answers the Phase 0 checklist's
Redfish questions (see *Jobs* below). Implementation follows the phased plan.

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

## Jobs

| Job | What it does |
|---|---|
| `SE350 Platform Discovery` ([jobs/baremetal/discover_platform.py](jobs/baremetal/discover_platform.py)) | Redfish sweep of one XCC: full BIOS attribute list (+ registry of allowed values when published), VirtualMedia EXT-member check, XCC/UEFI/NIC firmware versions, Secure Boot state — read-only by default, with checklist §1/§3 verdicts logged and JSON dumps attached to the JobResult. Opt-in **write checks**: virtual-media mount/verify/eject test (auto-detects XCC1 PATCH-on-EXT vs XCC2 InsertMedia — the dual-mode client seed), and a clearly-marked DISRUPTIVE dress rehearsal that boot-once's the mounted ISO (lab units only). |

Setup (Nautobot 2.4):

1. Add this repo as a **Git Repository** (Extensibility → Git Repositories), provides:
   **jobs**, then Sync. Note the repo-root [`__init__.py`](__init__.py) is load-bearing:
   Nautobot imports the checkout as a package named after the repo slug, and job
   discovery finds nothing without it (sync warns "No jobs were registered").
2. Create two Secrets named `xcc_username` and `xcc_password` (any provider — e.g.
   Environment Variable on the worker), matching the bare-metal starter's convention.
3. Enable the job (Jobs are disabled on first sync), run it against one lab XCC IP.

The pure-Python client ([jobs/lib/redfish_discovery.py](jobs/lib/redfish_discovery.py))
also runs standalone: `python redfish_discovery.py --bmc-ip <ip> --username <u>
--password <p> --insecure`.

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
- **Reservation parity by policy, pinning as escalation** — KVM has no ESXi-style CPU
  reservation; the no-oversubscription guardrail is the admission control, plus
  `balloon=0`, KSM off, and host services/IRQs confined to housekeeping cores. Per-VM
  CPU affinity is invoked only if the Phase 2 jitter soak demands it (proposed; VM
  tuning table in the site reference architecture).
- **Two-bridge layout mirrors the legacy vSwitch0/LAN-Trunk split** — mgmt `vmbr0` +
  VLAN-aware `vmbr1` on the 10G LACP bond, jumbo MTU on the data path (proposed).

See the [decision log](docs/decision-log.md) for the full list with status.
