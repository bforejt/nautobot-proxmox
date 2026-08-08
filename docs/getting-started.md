# Getting Started — New Lab Environment

One-time setup to run the deploy jobs against **already-built Proxmox hosts
that are configured in Nautobot** (the ESXi-robot pattern: the hypervisor is
installed and networked by hand or your existing process; these jobs deploy and
manage VMs on top). No bare-metal install or host-network automation is
required for this path.

Scope note: steps marked ⚙️ can be automated later (as jobs or install hooks);
they are fine as manual one-time setup today.

## 1. Connect the jobs

⚙️ Extensibility → **Git Repositories** → Add this repo, Provides: **jobs**,
Sync. Then Jobs → run **`Bootstrap NFV Data Model`** once (it is idempotent —
re-run any time; re-running after a repo update adds only what is new). This
creates every role, relationship, DeviceType, platform, and custom field the
other jobs rely on.

## 2. Firmware/image server

Stand up an HTTP(S) server the Proxmox nodes can reach at stable
`/images/<file>` URLs (the nautobot-composer `firmware` profile, or any nginx).
This is where golden images live; `SoftwareImageFile.download_url` points here.
Default base URL = your composer firmware server; each registration can point
elsewhere if preferred.

## 3. Secrets

- **Console password** (cloud-init guests): create Secret
  `jumphost_console_password` (text-file provider or your backend).
- **Proxmox API token(s)** — two ways:
  - **Single host (quickstart)**: create Secrets `proxmox_token_id`
    (value = `user@realm!tokenname`) and `proxmox_token_secret` (the UUID). The
    jobs use these when a hypervisor has no per-host SecretsGroup.
  - **A pair / multiple standalone hosts (recommended)**: each node has its own
    token. Create a **SecretsGroup per node** — add the token id as a
    *Generic / Username* secret and the token UUID as a *Generic / Secret* — and
    put the group's name in the hypervisor Device's **Proxmox SecretsGroup**
    custom field. The deploy job resolves that group; no global Secret needed.

## 4. Proxmox service account (per node)

⚙️ On each Proxmox node, create the automation identity (privilege-separated
token). Role privileges (validated): `VM.Allocate, VM.Clone, VM.Config.*,
VM.PowerMgmt, VM.Audit, VM.Console, Datastore.AllocateSpace,
Datastore.AllocateTemplate, Datastore.Audit, Sys.Audit, Sys.Modify, SDN.Use`.
**Grant the role to BOTH the user and the token** (privsep tokens intersect
user+token ACLs). Put the token where step 3 expects it.

## 5. A golden image

Build/obtain a template qcow2, publish it to the firmware server, and register
it in Nautobot: a **SoftwareVersion** (status **Staged**) + a
**SoftwareImageFile** (filename, SHA256, size, `download_url`). Promote
Staged → **Active** (the human gate) when validated. See
[image-lifecycle.md](image-lifecycle.md). Platform tunables (day-0 builder,
machine type, console user) are seeded by the bootstrap and adjustable per
platform.

## 6. Site intent (your layout process)

Create contract-conformant records — by NtC Design Builder, your own design, or
by hand for a first test. The **NFV-Lab fixture** is a worked example. Per
[sot-data-contract.md](sot-data-contract.md), each site needs:
- a **Hypervisor** Device (role Hypervisor) with `primary_ip4`, the VM
  bridge/storage/import-storage CFs, and its credential reference (step 3);
- **VNF** Devices (status **Planned**) with `software_version` (Active),
  sizing CFs (`vcpus`/`memory_mb`/`disk_gb`), a **Hosted On** relationship to
  the hypervisor, interfaces with pinned MACs + VLANs, and a `DefaultGW`-role
  gateway IP in each prefix.

## 7. Deploy

Jobs → **`Deploy VNF Device (SoT-driven)`**, pick the Planned device. It reads
the contract, deploys, and writes back the VMID + flips the device to Active.
Teardown/redeploy: **`Decommission VNF Device (SoT-driven)`**. Pre-stage images
ahead of a window with **`Ingest Image onto Proxmox Node`**.

---

### Minimum to prove it in a new lab
Steps 1, 3 (single-host quickstart), 5, 6 (one hypervisor + one jump host by
hand), 7. That is the whole loop; everything else scales it up.
