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
custom field the other jobs rely on — since decision #55 also the interface
custom fields of the host-baseline bond/bridge model (`lag_mode`,
`lag_xmit_hash`, `primary_member`), the baseline's Secret records, and the
ConfigContextSchema `nfv-host-baseline` (see step 8). (These jobs run on Nautobot
2.4 and 3.x — validated on 2.4.30 and 3.2. Standing up the stack fresh with
nautobot-composer? Composer can do this **whole step** for you:
`./setup.sh --with-nfv-jobs` registers this repo, syncs, enables the jobs,
and runs the bootstrap against a healthy stack.)

The bootstrap's three inputs decide where its Secret records point (decision
#56) — **Secrets provider** (`text-file`, the default, or
`environment-variable`), **text-file path prefix** (default
`/opt/nautobot/secrets`: records point at `<prefix>/<name>`), and
**environment-variable name prefix** (records name `<PREFIX><NAME>`, the
secret name upper-cased with `-` → `_`: `NFV_` + `xcc_password` →
`NFV_XCC_PASSWORD`). One record is the exception to `<prefix>/<name>`: the
forge admin token record `answer-service-admin-token` points at
`<prefix>/answer_service_admin_token`, the composer's `./add-secret.sh` file
name (its variable is `ANSWER_SERVICE_ADMIN_TOKEN`). The defaults reproduce
the composer layout exactly (`./setup.sh --with-nfv-jobs` runs the job with
them), so on composer there is nothing to choose. Not on composer? Pick the
environment-variable provider, or the prefix your deployment mounts the
secrets at, on the FIRST run: the job is create-only, so a record that
already exists — repointed by you or not — is never touched, and a re-run
with other inputs changes nothing already there. The job refuses an unknown
provider, a relative prefix or a malformed variable prefix (both prefixes
are checked whichever provider is chosen) — and, under environment-variable,
a config-context secret name it cannot spell as a variable (one with `.` or
` `): rename it in the context or keep text-file — before it writes
anything. What any deployment must provide beyond the jobs, and how the
composer provides each item: [platform-contract.md](platform-contract.md).

If the bootstrap refuses, nothing was written — fix the input or the config
context it names and re-run:

- `Secrets provider '…' is not supported — choose text-file or environment-variable`
  / `text-file path prefix '…' must be an absolute path (start with /)` /
  `text-file path prefix '…' must not contain '..' — Nautobot's text-file
  provider refuses such a path` / `text-file path prefix '…' contains a
  control character (newline, tab, ...) — not allowed` /
  `environment-variable name prefix '…' must match ^[A-Z_][A-Z0-9_]*$
  (upper-case letters, digits and '_', not starting with a digit) or be empty`
  — a job input (the UI form, or the API run's `data`) is unusable; both
  prefixes are checked whichever provider is chosen.
- `Secret '…' cannot be an environment-variable record: '…' is not a valid
  variable name (only letters, digits, '_' and '-' map, and the variable must
  not start with a digit — a name prefix such as NFV_ fixes that case) —
  rename it where the config context references it, or use the text-file
  provider` — a `host_baseline` config context names a secret with `.` or
  ` ` (its schema allows them) or with a leading digit: rename it in the
  context, set a name prefix for the leading digit, or keep text-file.
- `Secret record name '…' must be non-empty and contain no '/', '..' or
  control character` — a config-context secret name Nautobot's text-file
  provider could not take as a path; rename it in the context.
- `Secret records '…' and '…' both resolve from the same variable '…' —
  rename one where the config context references it, or use the text-file
  provider` (text-file: `… both resolve from the same file '…' — rename one
  …`) — two names spell one variable (`a-b` and `a_b`, or a case
  difference) or point at one file (a context record named
  `answer_service_admin_token` beside the forge token); rename one in the
  context.

## 2. Firmware/image server

Stand up an HTTP(S) server the Proxmox nodes can reach at stable
`/images/<file>` URLs (the nautobot-composer `firmware` profile, or any nginx).
This is where golden images live. Each `SoftwareImageFile` records its own
full `download_url`, so the "default" is just a convention: point registrations
at this server unless a specific image lives elsewhere.

## 3. Secrets

**The Secret records are created by the bootstrap job** (under the provider
and prefix chosen in step 1 — by default text-file at
`/opt/nautobot/secrets/<name>`, the composer layout; records only, never
values; an existing record you've repointed at another provider is left
alone). You supply the VALUES — on a
nautobot-composer stack, one `./add-secret.sh <name>` per credential
(`./setup.sh --nfv-secrets` prompts through all of them in one pass):

- `jumphost_console_password` — console login for deployed cloud-init guests.
- `xcc_username` / `xcc_password` — Lenovo XCC login for the physical nodes
  (SE350 / SE455 V3): platform discovery, out-of-band storage layout, and
  vmedia install delivery.
- `host_ssh_username` / `host_ssh_password` — the **root** login the
  `SE350 Host Verification (SSH)` job uses against a Linux-booted unit. A
  sudo-capable non-root user is not enough: the job never invokes sudo, and
  its DMI serial read (`/sys/class/dmi/id/product_serial`) is root-only.
- `pa_admin_password` — PA-VM admin password (REQUIRED before a PA deploy;
  it ships in bootstrap.xml as a hash so firewalls never come up admin/admin).
- `pa_authcode` — optional BYOL auth code; leave valueless for unlicensed
  lab boots.
- `scm_registration_pin_id` / `scm_registration_pin_value` — only needed for
  devices with `pa_mgmt_mode=scm` (Strata Cloud Manager registration).
- `ad_bind_password`, `snmp_community` and `snmpv3_<user>_auth` /
  `snmpv3_<user>_priv` — only for the **Host Baseline** job (step 8): the AD
  realm's bind password, the SNMP v2c community, the SNMPv3 passphrases.
  The bootstrap creates the per-user records for every v3 user a config
  context names — re-run it after adding users.

Not on composer? The records follow the provider you chose in step 1:
text-file — write each value to the file the record's path names
(`<prefix>/<name>`, readable by the Nautobot web and worker processes);
environment-variable — export `<PREFIX><NAME>` in every Nautobot container
(web AND worker: jobs resolve in the worker, the UI tests in web); or repoint
any record at your own secrets provider — jobs resolve by record name, not
provider. Whatever the provider, three paths must agree: the per-node token
records the answer service and the Host Baseline job write stay text-file
under `<prefix>/nodes`, so the bootstrap's path prefix + `/nodes`, the
answer service's `NAUTOBOT_SECRETS_PATH` and the worker's
`NFV_NODE_SECRETS_DIR` must name the same directory (all default to
`/opt/nautobot/secrets/nodes`; [platform-contract.md](platform-contract.md)
item 3).
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
pveum role add NFVAutomation --privs "VM.Allocate,VM.Clone,VM.Config.Disk,VM.Config.CDROM,VM.Config.CPU,VM.Config.Memory,VM.Config.Network,VM.Config.HWType,VM.Config.Options,VM.Config.Cloudinit,VM.PowerMgmt,VM.Audit,VM.GuestAgent.Audit,VM.Console,Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use"
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
now closed), and `VM.GuestAgent.Audit` joined it 2026-10-02 (the deploy job's
guest-agent readiness probe, `agent/network-get-interfaces`, is gated on it
since PVE 8.2; without it the probe is refused with 403 and the deploy ends
"Readiness UNVERIFIED"). The answer service's firstboot role default carries
both, so freshly L0-installed nodes get them automatically — nodes installed
before those updates, and hand-built nodes, re-run:

```bash
pveum role modify NFVAutomation --privs "VM.Allocate,VM.Clone,VM.Config.Disk,VM.Config.CDROM,VM.Config.CPU,VM.Config.Memory,VM.Config.Network,VM.Config.HWType,VM.Config.Options,VM.Config.Cloudinit,VM.PowerMgmt,VM.Audit,VM.GuestAgent.Audit,VM.Console,Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,Sys.Audit,Sys.Modify,SDN.Use"
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
| Device type | `ThinkSystem SE350` or `ThinkEdge SE455 V3` (both bootstrap-created; any type works for a lab box) |
| Interface | `mgmt` (type Virtual) with the node's management IP assigned, set as the device's **primary IPv4** — this is the API endpoint |
| CF `vm_bridge` | `vmbr0` (SE350 standard: `vmbr1`) |
| CF `vm_storage` | `local-lvm` (an SE455 V3 installed by the bare-metal loop: `DataDrive`, the firstboot-created LVM-thin storage) |
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

## 8. Host baseline (optional — nodes you want fleet-standard)

For Proxmox nodes that should carry the fleet baseline — SNMP, the AD realm
with its sync job and admin ACL, service-account tokens (Datadog, PDM), and
the bond/bridge network — model them per
[sot-data-contract.md §4c](sot-data-contract.md#4c-host-baseline-decisions-5455)
(config context `host_baseline`, bond and bridge interfaces, the Secrets
above), set `provisioning_state=bm_installed` on a node built outside the
install loop, and run **`Host Baseline (SoT-driven)`** — dry run first. The
job needs the root SSH login (`host_ssh_*`) and, on composer, the worker's
read-write `secrets/nodes` mount (nautobot-composer#66). Runbook:
[baremetal-install.md](baremetal-install.md#host-baseline-after-verification).

---

### Minimum to prove it in a new lab
Steps 1, 3 (single-host quickstart), 5, 6 (one hypervisor + one jump host by
hand), 7. That is the whole loop; everything else scales it up.
