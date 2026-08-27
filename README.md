# nautobot-proxmox

**Nautobot-driven lifecycle automation for Proxmox VE NFV hypervisors.** Given
site intent in Nautobot (devices, IPAM, relationships), the jobs in this repo
deploy, verify, and decommission VNF virtual machines on standalone Proxmox
hosts — golden-image based, checksum-verified end to end, with every input read
from Nautobot and every outcome written back to it. It replaces a legacy
interactive "robot" that built ESXi-based NFV edge servers, keeping the same
operating pattern: hosts are built and networked first; these jobs own
everything from the VM layer up.

## What works today

- ✅ **Ubuntu jump-host track, end to end and proven live**: golden template
  build (Ubuntu 24.04 cloud image + declarative seed) → publish to the firmware
  server → version registration with a human Staged→Active promotion gate →
  SoT-driven deploy (one input: the Device) → console credentials from Secrets
  → SoT-true decommission. A v1→v2 template rollout has been exercised for real.
- ✅ **The data model and contract**: bootstrapped idempotently by a job; every
  value the deploy reads is documented in one place.
- ✅ **Bare-metal install loop (L0), every mechanism field-proven**: a blank
  server boots a prepared installer, identifies itself to the SoT-backed
  answer service, installs unattended, and registers its own API credentials
  — proven live via nested VM, PXE on a physical NUC, and the SE350's XCC
  virtual-media checks. Installer media is prepared by the **media forge**
  from a Nautobot job. See [docs/baremetal-install.md](docs/baremetal-install.md).
- ✅ **SE350/XCC platform discovery + host verification** (read-only Redfish
  sweep, opt-in vmedia write checks, SSH host checks) for the bare-metal track.
- 🚧 **Not yet built**: Palo Alto / Cisco VNF day-0 builders (the deploy engine
  is pluggable and ready for them), host network/tuning automation,
  audit/converge jobs. See
  [docs/deployment-onboarding.md](docs/deployment-onboarding.md) for the honest
  gap register.

## Who this is for

Network/automation engineers who run **Nautobot as their source of truth** and
manage **Proxmox hosts serving as NFV edge compute** (virtual firewalls,
routers, WLCs, jump hosts — not general datacenter virtualization). You should
be comfortable with Nautobot's data model (Devices, Interfaces, IPAM, custom
fields, Secrets, Git-synced jobs) and basic Proxmox administration. The target
environment is pairs of *standalone* hosts with no shared storage, where
redundancy comes from running VM pairs across two independent machines —
deliberately **never clustered** (a two-node Proxmox cluster without shared
storage blocks VM autostart when one node dies).

## How it works — five ideas

1. **Nautobot is the single source of truth.** Site intent (which VMs exist,
   where, with what sizes, addresses, MACs, software) is created *in Nautobot*
   by your layout process — Network to Code (NtC) Design Builder, your own
   design job, or by hand. The jobs here never invent data; they read it and converge
   infrastructure to it. The exact records they read are specified in
   [docs/sot-data-contract.md](docs/sot-data-contract.md).
2. **Device status drives lifecycle.** A VNF Device in status **Planned** is
   intent-not-yet-deployed. The deploy job builds it, records the Proxmox VMID
   on the device, and flips it to **Active**. Decommissioning reverses both.
   The record never lies about reality.
3. **Golden images are versioned artifacts with a promotion gate.** Templates
   are built from vendor bases + a git-reviewed seed, published to a firmware
   server with checksums, and registered as Nautobot SoftwareVersions in
   **Staged** status. A human promotes Staged → **Active**; deploys refuse
   anything else. Rollout and rollback are status flips, not file copies. See
   [docs/image-lifecycle.md](docs/image-lifecycle.md).
4. **Jobs fail closed.** Any missing contract datum (no sizing, no image, no
   gateway, no credentials) is a precise refusal *before* anything is touched —
   never a guess or a half-deploy.
5. **Least privilege throughout.** Proxmox access uses a privilege-separated
   API token with a documented custom role; images are checksum-verified at
   every hop; passwords live in Nautobot Secrets and are hashed at rest on the
   hypervisor.

## Requirements

| Component | Version / notes |
|---|---|
| **Nautobot** | 2.4.x and 3.x (built on 2.4.30; validated on 3.2). Needs core `SoftwareVersion`/`SoftwareImageFile` (2.2+). |
| **Proxmox VE** | 9.x (validated on 9.2). Requires the `import` storage content type and API-token auth. Hosts are **standalone** (no cluster). |
| **Firmware/image server** | Any HTTP(S) server the Proxmox nodes can reach at stable `/images/<file>` URLs. The [nautobot-composer](https://github.com/bforejt/nautobot-composer) project's `firmware` profile provides one (nginx + Filebrowser). |
| **Git host** | Anywhere Nautobot can sync this repo from (GitHub today; any git remote works). |
| **Network paths** | Nautobot worker → Proxmox API (`:8006`); Proxmox nodes → firmware server; Nautobot → git host. Nothing else. |
| **Hypervisor hardware** | Any x86 Proxmox host for the VM track (developed against a small NUC). SE350-specific material (BIOS policy, Redfish/XCC, wiring) applies to the edge-hardware track only. |
| **Guest images** | Ubuntu 24.04 cloud image (fetched at template build). Vendor VNF images (PAN-OS, IOS-XE) are entitlement-gated downloads you supply. |

## Getting started

The full one-time checklist is **[docs/getting-started.md](docs/getting-started.md)**.
The minimum loop to prove it in a new lab:

1. Add this repo as a Nautobot **Git Repository** (provides: *jobs*), sync, and
   run **`Bootstrap NFV Data Model`** once. ⚠️ The repo-root
   [`__init__.py`](__init__.py) is load-bearing — Nautobot imports the checkout
   as a package; without it, sync reports "No jobs were registered".
2. Create the Secrets: a Proxmox API token pair (or a per-host SecretsGroup)
   and the jump-host console password.
3. Build a golden image with
   [vnf-profiles/ubuntu/build-template.sh](vnf-profiles/ubuntu/build-template.sh),
   publish it to your firmware server, and register it (SoftwareVersion
   **Staged** → promote to **Active**).
4. Create one hypervisor Device + one VNF Device (status **Planned**) per the
   [contract](docs/sot-data-contract.md) — the worked example in
   getting-started shows every required field.
5. Run **`Deploy VNF Device (SoT-driven)`**. Log in at the VM console with the
   configured user and Secret-stored password.

## Jobs

| Job | What it does |
|---|---|
| `Bootstrap NFV Data Model` ([jobs/design/bootstrap_schema.py](jobs/design/bootstrap_schema.py)) | Idempotently creates the data-model prerequisites (Hosted On relationship, roles, virtual DeviceTypes, platforms, custom fields, platform tunables, the Staged/Retired image statuses). Run once per environment; safe to re-run after every update. |
| `Deploy VNF Device (SoT-driven)` ([jobs/proxmox/deploy_device.py](jobs/proxmox/deploy_device.py)) | Deploys one Planned VNF Device reading everything from Nautobot per the contract — hypervisor via Hosted On, Active-gated image, sizing, pinned-MAC NICs, console credentials. Writes back VMID + flips to Active. |
| `Decommission VNF Device (SoT-driven)` ([jobs/proxmox/decommission_device.py](jobs/proxmox/decommission_device.py)) | SoT-true teardown: verifies VMID+name match, destroys the VM, writes back Active→Planned. Deploy + decommission = the redeploy primitive. |
| `Ingest Image onto Proxmox Node` ([jobs/proxmox/ingest_image.py](jobs/proxmox/ingest_image.py)) | Idempotent, checksum-verified pre-stage of an image onto a hypervisor — warm nodes ahead of maintenance windows. |
| `SE350 Platform Discovery` ([jobs/baremetal/discover_platform.py](jobs/baremetal/discover_platform.py)) | Read-only Redfish sweep of a Lenovo XCC (BIOS attributes, virtual-media capability, firmware); opt-in write checks incl. a lab-only boot dress rehearsal. Edge-hardware track. |
| `Install Proxmox Node (SoT-driven)` ([jobs/baremetal/install_node.py](jobs/baremetal/install_node.py)) | One-input bare-metal install: boots the prepared installer (nested VM or XCC virtual media per the DeviceType profile) and follows the state machine to an installed, self-credentialed node. |
| `Prepare Installer Media (Media Forge)` ([jobs/baremetal/prepare_media.py](jobs/baremetal/prepare_media.py)) | Asks the answer service to prepare, publish, and register (Staged) installer media bound to its own URL/cert identity — decision #44. |
| `SE350 Host Verification (SSH)` ([jobs/baremetal/verify_host.py](jobs/baremetal/verify_host.py)) | Read-only SSH pass over a Linux-booted SE350: disk-filter validation with the installer's own matching, DMI serial vs SoT, X722 LLDP flag, Secure Boot, BIOS-effect readbacks. |

## Documentation map

**Operate** (start here):

| Doc | What it answers |
|---|---|
| [getting-started.md](docs/getting-started.md) | One-time setup for a new environment, step by step, with a worked example |
| [sot-data-contract.md](docs/sot-data-contract.md) | Exactly which Nautobot records the jobs read and write |
| [baremetal-install.md](docs/baremetal-install.md) | How a blank server becomes a registered Proxmox node — answer service, media forge, nested/PXE/vmedia delivery, runbooks |
| [deployment-onboarding.md](docs/deployment-onboarding.md) | What's portable vs. what's still manual — the gap register |
| [image-lifecycle.md](docs/image-lifecycle.md) | How golden images are built, versioned, promoted, rolled back (notes which steps are scripted vs. jobs) |

**Design record**:

| Doc | What it answers |
|---|---|
| [decision-log.md](docs/decision-log.md) | Every architectural decision, dated, with status and rationale |
| [plan-of-attack.md](docs/plan-of-attack.md) | The full phased plan: assessment, ESXi→Proxmox translation, roadmap |
| [site-reference-architecture.md](docs/site-reference-architecture.md) | The edge-site standard being reproduced (VLANs, wiring, LACP, tuning) |
| [se350-verification-checklist.md](docs/se350-verification-checklist.md) | Hardware validation checklist for the SE350 edge platform |
| [research/](docs/research/) | Deep per-dimension research backing the plan (six documents) |

## Design rules (read before contributing)

- **SSoT-first**: if Nautobot can hold it, Nautobot holds it — jobs take object
  references, not free-form values.
- **Desired state lives in the SoT, once**: fully materialized per-device
  records; no runtime settings from files; standards defined in the layout
  process that stamps them.
- **Facts vs. tunables**: immutable guest-OS behavior lives in code
  ([jobs/lib/platform_facts.py](jobs/lib/platform_facts.py)); operationally
  adjustable values live on Nautobot objects.
- **Never touch infrastructure without updating the record** — and write-backs
  happen in the same job that made the change.

## Repo layout

```
jobs/            Nautobot jobs (Git-synced) + pure-Python libs (lib/)
bmc/             BIOS/firmware policy as data (SE350 edge track)
vnf-profiles/    Golden-image build seeds + build script per guest platform
docs/            The documentation set above
tests/           Loader harness (validates job discovery pre-push)
```

## Contributing & testing

Changes go through a branch + pull request; after merge, re-sync the Git
Repository in Nautobot and re-run `Bootstrap NFV Data Model` (idempotent — it
adds only what's new). Before pushing, run the loader harness — it drives this
repo through Nautobot's *real* job-loading code without needing a Nautobot
install, and catches the classic silent failure (a directory missing
`__init__.py` loads zero jobs):

```bash
python3 tests/loader_harness.py
```

## License

[Apache-2.0](LICENSE).
