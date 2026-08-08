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
   `jumphost_console_password`, and XCC creds — via the composer text-file
   provider or your secrets backend.
4. **Create the Proxmox service account** per node (custom role `NFVAutomation`
   + privilege-separated token, granted to BOTH user and token), token value
   into the Secret. `[gap: manual today; firstboot-automated in the plan]`

### Phase B — Golden image (once per image version)
5. **Build the template** (vendor cloud image + build seed → sealed qcow2),
   publish the version set to the firmware server, register the
   `SoftwareVersion` (Staged) + `SoftwareImageFile`, then **promote Staged →
   Active** (human gate). `[gap: build is scripted, not yet a BuildTemplateJob;
   registration currently hardcodes the download_url host]`

### Phase C — Per site (the repeatable operation)
6. **The layout engine creates intent** — the adopter's Design Builder design
   (or equivalent) materializes contract-conformant records: hypervisor +
   VNF Devices, `Hosted On` relationships, interfaces with pinned MACs and
   VLANs, IPAM with a `DefaultGW`-role gateway per prefix, sizing CFs,
   `software_version`, hypervisor target CFs, status **Planned**.
   `[gap: no reference design shipped — the fixture site (NFV-Lab) is the only
   worked example]`
7. **Deploy**: run `DeployVnfDevice` per VNF (a `SiteBuildJob` wrapper is
   planned). It reads the contract, deploys, writes back vmid + Active.
   Teardown/redeploy via `DecommissionVnfDevice`. `[proven]`

## Gap register — what stands between here and turnkey

| Gap | Impact | Where it goes |
|---|---|---|
| **Bare-metal track unbuilt** (Phase 3: XCC → PVE install) | Can't go from a blank SE350 to a running node automatically; node must be hand-installed | Colleague's `xcc_deploy` + the plan's L0 |
| **Host baseline + network jobs unbuilt** (L1/L2: bond/bridge/tuning) | Node networking (LACP, VLAN-aware bridge, MTU) is manual today | firstboot hook + `DeployHostNetworkJob` |
| **Service-account bootstrap manual** | Every node's automation token created by hand | pveum in the firstboot hook (plan §3) |
| **Template build is scripts, not a job** | Rebuilds need an operator running steps, not a button | `BuildTemplateJob` |
| **`download_url` hardcoded at registration** (lab IP) | Registration is environment-specific; should derive from a firmware base URL | Registration helper / `BuildTemplateJob` |
| **Layout engine has no reference implementation** | Each adopter must author their Design Builder design from the contract; only the fixture demonstrates it | Adopter responsibility; a reference design would help |
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
