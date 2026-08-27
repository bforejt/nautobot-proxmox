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
Sync. Git-synced jobs arrive **disabled** — enable each one under Jobs before
its Run button works. Then run **`Bootstrap NFV Data Model`** once (it is
idempotent — re-run any time; re-running after a repo update adds only what is
new). This creates every role, relationship, DeviceType, platform, status, and
custom field the other jobs rely on. (These jobs run on Nautobot
2.4 and 3.x — validated on 2.4.30 and 3.2. Standing up the stack fresh with
nautobot-composer? Composer can do this **whole step** for you:
`./setup.sh --with-nfv-jobs` registers this repo, syncs, enables the jobs,
and runs the bootstrap against a healthy stack.)

## 2. Firmware/image server

Stand up an HTTP(S) server the Proxmox nodes can reach at stable
`/images/<file>` URLs (the nautobot-composer `firmware` profile, or any nginx).
This is where golden images live. Each `SoftwareImageFile` records its own
full `download_url`, so the "default" is just a convention: point registrations
at this server unless a specific image lives elsewhere.

## 3. Secrets

**The Secret records are created by the bootstrap job** (text-file provider,
standard paths — records only, never values; an existing record you've
repointed at another provider is left alone). You supply the VALUES — on a
nautobot-composer stack, one `./add-secret.sh <name>` per credential
(`./setup.sh --nfv-secrets` prompts through all of them in one pass):

- `jumphost_console_password` — console login for deployed cloud-init guests.
- `xcc_username` / `xcc_password` — SE350 BMC login (discovery + vmedia
  install delivery).
- `host_ssh_username` / `host_ssh_password` — root (or sudo-capable) login
  the `SE350 Host Verification (SSH)` job uses against a Linux-booted unit.
- `pa_admin_password` — PA-VM admin password (REQUIRED before a PA deploy;
  it ships in bootstrap.xml as a hash so firewalls never come up admin/admin).
- `pa_authcode` — optional BYOL auth code; leave valueless for unlicensed
  lab boots.
- `scm_registration_pin_id` / `scm_registration_pin_value` — only needed for
  devices with `pa_mgmt_mode=scm` (Strata Cloud Manager registration).

Not on composer? Write each value to the file the record's path names
(`/opt/nautobot/secrets/<name>`, readable by the Nautobot web and worker
processes), or repoint the record at your own secrets provider — jobs resolve
by record name, not provider.
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

⚙️ On each Proxmox node, create the automation identity — a service user with
a custom **`NFVAutomation`** role and a privilege-separated token. As root on
the node:

```bash
pveum role add NFVAutomation --privs "VM.Allocate,VM.Clone,VM.Config.Disk,VM.Config.CDROM,VM.Config.CPU,VM.Config.Memory,VM.Config.Network,VM.Config.HWType,VM.Config.Options,VM.Config.Cloudinit,VM.PowerMgmt,VM.Audit,VM.Console,Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use"
pveum user add nfv-automation@pve --comment "Nautobot NFV jobs"
pveum user token add nfv-automation@pve nautobot --privsep 1   # SAVE the printed UUID
pveum acl modify / --users nfv-automation@pve --roles NFVAutomation
pveum acl modify / --tokens 'nfv-automation@pve!nautobot' --roles NFVAutomation
```

The last two lines matter: a privilege-separated token's effective rights are
the **intersection** of the user's ACLs and the token's ACLs, so the role must
be granted to BOTH (validated the hard way — role on the user only = 403 on
everything). Put the token id (`nfv-automation@pve!nautobot`) and the UUID
where step 3 expects them.

**Upgrading an existing install**: `Datastore.Allocate` joined the role
2026-08-27 (the PA deploy path deletes its own bootstrap ISO after first
boot — content deletion needs it; the deliberate gap noted in decision #35 is
now closed). The answer service's firstboot role default was updated in the
same change, so freshly L0-installed nodes get it automatically — nodes
installed before that update, and hand-built nodes, re-run:

```bash
pveum role modify NFVAutomation --privs "VM.Allocate,VM.Clone,VM.Config.Disk,VM.Config.CDROM,VM.Config.CPU,VM.Config.Memory,VM.Config.Network,VM.Config.HWType,VM.Config.Options,VM.Config.Cloudinit,VM.PowerMgmt,VM.Audit,VM.Console,Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use"
```

## 5. A golden image

Build the Ubuntu jump-host template with the shipped script — run it from your
workstation against a **build node** (any lab Proxmox host you have root SSH
to; never a field node):

```bash
vnf-profiles/ubuntu/build-template.sh root@<build-node> 24.04-v1
```

It pulls the vendor cloud image (checksum-verified), boots one unattended
build with the seed
([template-build.user-data.yaml](../vnf-profiles/ubuntu/template-build.user-data.yaml)),
seals, and publishes the version set (qcow2 + sha256 + seed + manifest) on the
node. Copy those files to the firmware server's image root, then register in
Nautobot: a **SoftwareVersion** (status **Staged** — the bootstrap job
provisioned this status for software models) + a **SoftwareImageFile**
(filename, SHA256, size, `download_url`) — the **`Register Image from
Published Set`** job does this from the artifact URL (supply platform +
version for template sets), or enter the values the script prints by hand.
Promote Staged → **Active** in the lab and validate one deploy (the deploy
job refuses non-Active versions — that IS the gate); rollback is flipping the
previous version back to Active, its artifact never left the server. Full lifecycle: [image-lifecycle.md](image-lifecycle.md). Platform
tunables (day-0 builder, machine type, console user) are seeded by the
bootstrap and adjustable per platform.

Vendor-sealed appliance images (PA-VM) skip the build entirely:
[vnf-profiles/paloalto/register-vendor-image.sh](../vnf-profiles/paloalto/register-vendor-image.sh)
verifies the vendor qcow2, publishes the version set, and prints the same
registration recipe (see image-lifecycle.md's register-only track).

## 6. Site intent (your layout process)

Create contract-conformant records — by Network to Code (NtC) Design Builder,
your own design job, or by hand for a first test. Per
[sot-data-contract.md](sot-data-contract.md) (its §0 quick-reference table
lists every value the code enforces), each site needs:
- an **NFV** Device (role `NFV` — the team's server role) with `primary_ip4`, the VM
  bridge/storage/import-storage CFs, and its credential reference (step 3);
- **VNF** Devices (status **Planned**) with `software_version` (Active),
  sizing CFs (`vcpus`/`memory_mb`/`disk_gb`), a **Hosted On** relationship to
  the hypervisor, and interfaces named per the platform's NIC order with
  pinned MACs (+ VLANs where used). Static-IP guests additionally need a
  `DefaultGW`-role gateway IP in their prefix; DHCP guests don't.

### Worked example — one hypervisor + one jump host, by hand

This is the exact shape proven live in the dev lab. Prerequisite: a
**Location** whose type allows devices (both devices need one).

**NFV device** (the already-built Proxmox host):

| Field | Value |
|---|---|
| Name | `pve1` — must equal the Proxmox **node name** exactly |
| Role / Status | `NFV` / `Active` |
| Device type | `ThinkSystem SE350` (bootstrap-created; any type works for a lab box) |
| Interface | `mgmt` (type Virtual) with the node's management IP assigned, set as the device's **primary IPv4** — this is the API endpoint |
| CF `vm_bridge` | `vmbr0` (SE350 standard: `vmbr1`) |
| CF `vm_storage` | `local-lvm` |
| CF `import_storage` | `local` — a storage with the **Import** content type enabled |
| CF `secrets_group` | name of its SecretsGroup, or empty to use the global Secrets (step 3) |

**VNF device** (the jump host to be deployed):

| Field | Value |
|---|---|
| Name | `jump-01` — becomes the VM name and guest hostname |
| Role / Status | `Jump Host` / **`Planned`** |
| Device type | `Ubuntu Jump Host VM` (bootstrap-created) |
| Platform | **`ubuntu-jumphost`** — exact name; deploy resolves guest facts by it |
| Software version | the **Active** SoftwareVersion from step 5 |
| CFs | `vcpus=2`, `memory_mb=4096`, `disk_gb=32` |
| Interface | named exactly **`eth0`** (type Virtual, per the platform's NIC order) with a pinned MAC, e.g. `BC:24:11:AA:00:01`. No IP assigned → guest uses DHCP; assign an IP (in a Namespace'd prefix with a `DefaultGW`-role gateway) and set it primary for static |
| Relationship | **Hosted On** → `pve1` |

## 7. Deploy

Jobs → **`Deploy VNF Device (SoT-driven)`**, pick the Planned device. It reads
the contract, deploys, and writes back the VMID + flips the device to Active.
Teardown/redeploy: **`Decommission VNF Device (SoT-driven)`**. Pre-stage images
ahead of a window with **`Ingest Image onto Proxmox Node`**.

---

### Minimum to prove it in a new lab
Steps 1, 3 (single-host quickstart), 5, 6 (one hypervisor + one jump host by
hand), 7. That is the whole loop; everything else scales it up.
