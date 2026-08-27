# Deployment & Onboarding — Standing Up a New Environment

Honest status (2026-08-27): the **data model, deploy engine, image lifecycle,
and bare-metal install loop are portable and field-proven** — the jobs read
everything from the SoT and carry no environment-specific logic, and composer's
setup flags (`--with-nfv-jobs`, `--nfv-secrets`, `--enable-forge`) automate most
environment setup. Still short of turnkey: the layout/intent engine is the
adopter's responsibility, the host network/tuning layers are unbuilt, and the
first *real* SE350 install (plus LACP/jumbo/serial validation on a real pair)
hasn't run. This document is both the onboarding runbook for what works today
and the honest gap register for what doesn't.

## What is portable and proven

- **The jobs** (Git-synced): the nine jobs in the README's Jobs table. No
  hardcoded environment logic — targets and values come from Nautobot.
- **The data model**: created idempotently by `BootstrapNfvSchema` in any
  Nautobot 2.4 or 3.x instance (validated on 2.4.30 and 3.2). Run once,
  re-run safely.
- **The image lifecycle**: vendor base → sealed template → firmware server →
  `SoftwareVersion`/`SoftwareImageFile` → checksum-verified node pull. Every
  step reproducible; every artifact self-describing (manifest + seed alongside).
- **The SoT data contract** ([sot-data-contract.md](sot-data-contract.md)): the
  fixed interface between the adopter's layout process and these jobs.

## Onboarding sequence (what works today)

### Phase A — Environment setup (once per company)
1. **Connect this repo** as a Nautobot Git Repository (provides: jobs), sync,
   enable the jobs, run **`BootstrapNfvSchema`** → creates roles, relationship,
   DeviceTypes, platforms, statuses, custom fields, Secret records, forge
   integration records. (Composer: `./setup.sh --with-nfv-jobs` performs this
   whole step against a healthy stack.)
2. **Stand up a firmware server** reachable by the Proxmox nodes over HTTP(S)
   at stable `/images/<file>` URLs (the composer `firmware` profile, or any
   equivalent nginx). `[gap: not automated]`
3. **Supply Secret VALUES** — the records themselves are pre-created by
   `BootstrapNfvSchema` (text-file provider, `/opt/nautobot/secrets/<name>`).
   On a composer stack: `./add-secret.sh <name>` per credential, or
   `./setup.sh --nfv-secrets` for all of them in one pass; elsewhere, write the
   files the records' paths name, or repoint records at your backend.
4. **Create the Proxmox service account** per node (custom role `NFVAutomation`
   + privilege-separated token, granted to BOTH user and token), token value
   into the Secret. `[gap: manual today; firstboot-automated in the plan]`

### Phase B — Golden image (once per image version)
5. **Build the template** with
   [vnf-profiles/ubuntu/build-template.sh](../vnf-profiles/ubuntu/build-template.sh)
   (vendor cloud image + build seed → sealed qcow2 + version set), copy the set
   to the firmware server, register the `SoftwareVersion` (Staged) +
   `SoftwareImageFile`, then **promote Staged → Active** in the lab and
   validate one deploy (human gate; rollback = flip back).
   `[gap: build is a shipped script, not yet a job; publish-copy is manual —
   registration is job-driven via Register Image from Published Set, and
   installer media has the media forge job]` Vendor-sealed appliance images
   (PA-VM) skip the build entirely:
   [vnf-profiles/paloalto/register-vendor-image.sh](../vnf-profiles/paloalto/register-vendor-image.sh)
   verifies, emits the version set, and prints the registration recipe
   (see image-lifecycle.md's register-only track).

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
| **PA-VM deploy path BUILT, lab validation pending** | The full track shipped 2026-08-27: register-only image process (script live-tested against the real 11.2.8 image; the registration job's fetch/verify path proven in a live HTTP harness — first run against a real stack pending), `pa-bootstrap` day-0 builder (init-cfg + bootstrap.xml phash + optional authcodes → per-device ISO uploaded to the node), pattern NIC ordering, `mgmt_bridge`, smbios-pinned serial, tcp-mgmt readiness, decommission ISO sweep + delicense warning. Not yet run against a live PA-VM (#45 bar); needs a composer image rebuild (pycdlib) and the `Datastore.Allocate` role update | First validation: static-mgmt standalone, unlicensed, lab node — decision #46's checklist |
| **Image registration** *(closed — job-driven)* | `Register Image from Published Set` reads a published version set (either track) and creates the Staged records — checksum/size come from the set, never hand-typed. Manual entry from the scripts' printed recipes remains the fallback | Remaining manual: publish-copy to the firmware server; the build itself (see the build-template row) |
| **Layout engine has no reference implementation** | Each adopter must author their Design Builder design from the contract; the getting-started worked example demonstrates the shape by hand | Adopter responsibility; a reference design would help |
| **Real-pair validation incomplete** | SE350 discovery, vmedia checks, disk identity, and host verification are green on a real unit — but LACP bonds, VLAN trunks, jumbo, serial/OpenGear, and the first real SE350 install remain unproven | The SE350 lab checklist + scheduling the first install |
| **Setup mostly automated, not fully** | Composer's `setup.sh` flags cover stack, jobs, bootstrap, secrets values, and the forge; remaining manual: per-node service accounts on pre-built hosts, image publish-copy, site intent | Firstboot covers new installs; a build-template job; the layout engine |

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
