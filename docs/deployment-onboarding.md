# Deployment & Onboarding — Standing Up a New Environment

Honest status (2026-08-08): the **data model, deploy engine, and image
lifecycle are portable and proven** — the jobs read everything from the SoT and
carry no environment-specific logic. But the project is **not yet turnkey**:
environment setup is still manual, several layers are unbuilt, the layout/intent
engine is the adopter's responsibility, and everything has been validated only
against a single x86 NUC — never a real SE350 pair. This document is both the
onboarding runbook for what works today and the honest gap register for what
doesn't.

## What is portable and proven

- **The jobs** (Git-synced): `BootstrapNfvSchema`, `DeployVnfDevice`,
  `DecommissionVnfDevice`, `IngestImage`, `DiscoverSe350Platform`. No hardcoded
  environment logic — targets and values come from Nautobot.
- **The data model**: created idempotently by `BootstrapNfvSchema` in any
  Nautobot 2.4 instance. Run once, re-run safely.
- **The image lifecycle**: vendor base → sealed template → firmware server →
  `SoftwareVersion`/`SoftwareImageFile` → checksum-verified node pull. Every
  step reproducible; every artifact self-describing (manifest + seed alongside).
- **The SoT data contract** ([sot-data-contract.md](sot-data-contract.md)): the
  fixed interface between the adopter's layout process and these jobs.

## Onboarding sequence (what works today)

### Phase A — Environment setup (once per company)
1. **Connect this repo** as a Nautobot Git Repository (provides: jobs), sync,
   run **`BootstrapNfvSchema`** → creates roles, relationship, DeviceTypes,
   platforms, custom fields, platform tunables.
2. **Stand up a firmware server** reachable by the Proxmox nodes over HTTP(S)
   at stable `/images/<file>` URLs (the composer `firmware` profile, or any
   equivalent nginx). `[gap: not automated]`
3. **Create Secrets**: `proxmox_token_id`, `proxmox_token_secret`,
   `jumphost_console_password`, and XCC creds — via Nautobot's text-file
   secrets provider (which the composer stack pre-wires) or your secrets
   backend.
4. **Create the Proxmox service account** per node (custom role `NFVAutomation`
   + privilege-separated token, granted to BOTH user and token), token value
   into the Secret. `[gap: manual today; firstboot-automated in the plan]`

### Phase B — Golden image (once per image version)
5. **Build the template** with
   [vnf-profiles/ubuntu/build-template.sh](../vnf-profiles/ubuntu/build-template.sh)
   (vendor cloud image + build seed → sealed qcow2 + version set), copy the set
   to the firmware server, register the `SoftwareVersion` (Staged) +
   `SoftwareImageFile`, then **promote Staged → Active** (human gate).
   `[gap: build is a shipped script, not yet a job; publish-copy and
   registration are manual]`

### Phase C — Per site (the repeatable operation)
6. **The layout engine creates intent** — the adopter's Design Builder design
   (or equivalent) materializes contract-conformant records: hypervisor +
   VNF Devices, `Hosted On` relationships, interfaces with pinned MACs and
   VLANs, IPAM with a `DefaultGW`-role gateway per prefix, sizing CFs,
   `software_version`, hypervisor target CFs, status **Planned**.
   `[gap: no reference design job shipped — the hand-built worked example in
   getting-started.md documents the target shape field by field]`
7. **Deploy**: run `DeployVnfDevice` per VNF (a `SiteBuildJob` wrapper is
   planned). It reads the contract, deploys, writes back vmid + Active.
   Teardown/redeploy via `DecommissionVnfDevice`. `[proven]`

## Gap register — what stands between here and turnkey

| Gap | Impact | Where it goes |
|---|---|---|
| **Bare-metal track: EVERY MECHANISM FIELD-PROVEN** (Phase 3) | Validated end-to-end three ways: (a) nested VM on the lab NUC (2026-08-09), (b) **a physical Intel NUC over PXE** (2026-08-09) — netboot → serial-discovery refusal → Device created → unattended PVE 9.2 install → firstboot `pveum` bootstrap → token phoned home, and (c) **the SE350 vmedia leg (2026-08-10, nfvlabspt1)** — XCC1 PATCH-EXT write test (`Inserted=true`, verified eject) + dress-rehearsal boot (one-shot CD override + ForceRestart, ISO boot confirmed on console). Host verification (disk filter, DMI serial, X722, Secure Boot) green on the same unit. Remaining: the first *real* SE350 install — a scheduling decision, not an engineering one | Schedule it: Device w/ serial `J101YCEB` + `awaiting_install` + Active prepared-ISO version, vmedia-mount the prepared ISO, watch the state machine |
| **Host baseline + network jobs unbuilt** (L1/L2: bond/bridge/tuning) | Node networking (LACP, VLAN-aware bridge, MTU) is manual today | firstboot hook + `DeployHostNetworkJob` |
| **Service-account bootstrap manual** *(closed for installed-by-us nodes)* | Nodes installed via the L0 loop get `svc-nfv@pve!deploy` + role + SecretsGroup automatically (firstboot → phone-home); hand-built hosts still follow the manual pveum steps | [baremetal-install.md](baremetal-install.md); manual path in getting-started §4 |
| **Template build is a script, not a job** | Rebuilds need an operator with build-node SSH running [build-template.sh](../vnf-profiles/ubuntu/build-template.sh), not a button | A build-template job |
| **Image registration is manual** | Version + image file + `download_url` entered by hand per environment | A registration helper / the build-template job |
| **Layout engine has no reference implementation** | Each adopter must author their Design Builder design from the contract; the getting-started worked example demonstrates the shape by hand | Adopter responsibility; a reference design would help |
| **Validated only on one x86 NUC** | LACP bonds, VLAN trunks, real SE350 firmware/BIOS/vmedia, jumbo, serial/OpenGear — all unproven on real hardware | The SE350 lab checklist |
| **Setup is scattered, not one installer** | No single "new environment" runbook beyond this doc; several manual steps | A setup job/script + this doc maturing |

## Verdict

**Reproducible core, not a turnkey product.** A capable operator can stand up a
new environment today by following Phase A–C and supplying their own layout
design — the jobs will behave identically, because they are pure SoT consumers.
But "drop it into a new company and it works the same way" is not yet true: it
requires manual environment setup, an adopter-built layout engine, and it has
never touched the actual target hardware. The deliberate order (prove the spine
first, on cheap hardware) is sound — the remaining work is the bare-metal/host
layers, turning the manual setup and template build into jobs, de-lab-ifying the
few hardcoded values, and a real SE350 validation pass.
