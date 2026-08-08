# SoT Data Contract — What the Deploy Jobs Read

Governing rule (team, 2026-08-08): **always use the SSoT when possible; when not,
keep data as normalized as possible.**

Division of labor: a separate layout process (NtC Design Builder App or
equivalent) creates all site intent **in Nautobot** — IPAM carve, devices,
interfaces, IPs, Hosted On relationships, per-device specifics. The jobs in this
repo never invent that data; they read it and converge infrastructure toward it.
This document is the contract: the exact conventions consumers depend on.
Items marked **PROPOSED** await team confirmation; everything else is settled.

## 1. The roster — which VMs exist where

- The **`Hosted On` relationship** (key `hosted_on`, hypervisor Device → VNF
  Devices) IS the roster. A site's server-2 differences are simply which VNF
  devices the layout relates to which hypervisor. *(Settled — stated by the team.)*
- **Settled (2026-08-08) — deploy trigger via native Status**: a VNF Device in
  status **Planned** is intent-not-yet-deployed; the deploy job builds it and
  flips it to **Active**. Decommissioned/parked states use the existing status
  set. Native field; the Jobs UI/API filter on it.

## 2. Per-VNF data read from the Device record

| Need | Source | Notes |
|---|---|---|
| VM name / guest hostname | `device.name` | Settled |
| Image to deploy | `device.software_version` → its default `SoftwareImageFile` | **Settled (2026-08-08)**: the layout process — whatever creates the VNF Device records (the team's Design Builder design or equivalent) — sets the native `software_version` FK on each device. Deploy **refuses** if unset or if the version's status ≠ Active, keeping the Staged→Active promotion gate authoritative |
| Sizing (vcpus / memory / disk) | The device's own CFs (`vcpus`, `memory_mb`, `disk_gb`) — **REQUIRED on every VNF device, set by the layout engine at creation**. There is no external sizing profile: the SoT record is complete, consumers read one place. Deploy **refuses** if any sizing CF is unset (same discipline as software_version) | **Settled (2026-08-08)** — team direction: fully materialized per-device values; the "define once" DRY lives in the layout engine's templates, not in runtime lookups. Fleet-wide change flow (SoT-first): bulk-update the CFs (Nautobot bulk edit or a small job) → run the converge job to resize actual VMs to the updated intent. Never the reverse |
| Platform behavior (day-0 builder, machine type, serial console, NIC model) | `device.platform` → the platform profile (config context) | Settled pattern from the plan |
| Proxmox VMID | CF `vmid` — **written back** by the deploy job after create | Settled (bootstrapped) |
| Host lifecycle stage | CF `provisioning_state` (hypervisors) | Settled (bootstrapped) |

## 3. Networking — interfaces, VLANs, addresses

- **Interfaces**: the layout creates `dcim.Interface` records on each VNF device
  with `mode`/`untagged_vlan`/`tagged_vlans` set — an access interface carries
  its VLAN, a trunk interface (PA dataplane) carries the tagged set. The deploy
  job renders these directly into Proxmox `netN` strings (`tag=` / `trunks=`).
- **NIC ordering — settled (2026-08-08): we PUSH order from the SoT; nothing is
  learned from the device at deploy time.** The mechanics that make this safe:
  Proxmox `netN` index → PCI slot order → guest enumeration order is fully
  deterministic, and each guest OS assigns its interface *names* to that order
  by fixed, per-platform rules (PA: first NIC = `mgmt`, then `ethernet1/1…` in
  order; IOS-XE: `Gi1, Gi2…`; Ubuntu: predictable names by PCI slot). The
  platform profile encodes that name↔index map once; the layout names the
  device's Interfaces with the *guest's* names; deploy renders `netN` in the
  profile's order. Three reinforcements:
  1. **Pinned MACs** (below) make Linux-class guests order-proof outright —
     PVE's generated cloud-init network config matches by MAC, not name.
  2. The **audit job** is where "learning from the device" lives: it reads the
     running guest's MAC↔interface mapping and diffs it against intent —
     verification, never silent adoption (per the SSoT-first rule, reality is
     checked against the SoT, not promoted into it).
  3. The one sanctioned learn-INTO-SoT flow is explicit **onboarding** of
     pre-existing (converted ESXi) sites, where a one-time backfill job records
     current MACs/ordering into Nautobot before the SoT takes over.
- **MAC addresses — pinned (Settled 2026-08-08). Storage: the
  native `mac_address` field on the Nautobot Interface record** — a
  first-class core column on `dcim.Interface`, visible on the interface form,
  REST-filterable, fully inside the SoT (not a custom field, not external).
  The layout engine writes it once at design time; deploy renders it into the
  Proxmox `netN` line (`virtio=<mac>,bridge=...`); redeploys reuse it; the
  audit job diffs running-guest MACs (via agent) against it. Rationale for
  pinning: destroy-and-recreate redeploys stay invisible to the L2 fabric
  (leases, ARP/CAM, snooping/port-security state, MAC-keyed monitoring all
  survive), and cloud-init's match-by-MAC config plus the audit's MAC↔intent
  diff both require stable intent MACs. FHRP/virtual MACs are unaffected.
- **Primary/mgmt IP**: `device.primary_ip4` — settled Nautobot convention.
- **Gateway — settled (2026-08-08)**: the default gateway IP in each prefix
  carries IPAddress **Role = `DefaultGW`** (team's standardization of the
  existing "Default Gateway" role; named to acknowledge that *other* gateways
  can coexist in a subnet — FHRP addresses keep their `VRRP`/`HSRP`/`VIP`
  roles). Contract: **exactly one `DefaultGW`-role IP per prefix**; consumers
  resolve gateway = the DefaultGW IP within the interface's prefix. The layout
  process applies the role per subnet; renaming legacy "Default Gateway"
  records is a team data-migration task.
- **DNS/NTP and similar site services**: config context. *(Settled pattern.)*

### Platform behavior — Settled (2026-08-08): facts in code, tunables as Platform CFs

Standing rule applied: **desired-state data lives in the SoT and nowhere else,
stored once.** The line it draws here:

- **Immutable platform FACTS** — guest NIC-name↔order (PA: `mgmt`,
  `ethernet1/1…`; IOS-XE: `Gi1…`), cloud-init class, serial-console
  expectations — are *behavior the code interprets*, not desired state. They
  live in the job code and change only with it (every possible "edit" to them
  is a broken deploy).
- **TUNABLES** — genuinely adjustable desired state — are **custom fields on
  the Platform object** (Option A):
  - `day0_builder` (select): which day-0 mechanism the platform binds to. The
    **choice list is maintained by the bootstrap job to exactly match the
    builders the code ships** — the code↔data handshake; selecting a
    nonexistent builder is a UI impossibility.
  - `machine_type` (text): the QEMU machine pin (e.g. `q35`, or a versioned
    pin like `pc-q35-8.1` after lab validation) — the canonical operational
    lever admins adjust without a code release.
- The bootstrap job **seeds values create-only** (a fresh instance works out
  of the box; an admin's adjustment is never overwritten by a re-run).
- Deploy **fails closed**: unset `day0_builder`/unknown values → precise
  refusal, no partial deploy.
- Because these are SoT desired state, they feed the converge trajectory:
  idempotent jobs diff intent vs actual (change classes: hot-apply /
  restart-required / redeploy-only) and JobHook receivers can auto-generate
  drift reports when watched fields change. Apply remains human-triggered.

## 4. The hypervisor record

| Need | Source | Notes |
|---|---|---|
| Node name (Proxmox) | `device.name` | Settled |
| API endpoint | `device.primary_ip4` | **Settled (2026-08-08)** |
| API credentials | Per-hypervisor **SecretsGroup** named by the device's `secrets_group` CF (Generic/Username = token id, Generic/Secret = token UUID) — each standalone node has its own token; **falls back** to the global `proxmox_token_id`/`proxmox_token_secret` Secret pair when the CF is empty (single-host quickstart) | **Settled (2026-08-08)** |
| BMC/XCC address | **Settled (2026-08-08)**: a dedicated interface named `xcc` on the SE350 device with its IP assigned — native, visible, cable-truthful | Layout process creates it |
| VM bridge + storage targets | Hypervisor-device CFs `vm_bridge`, `vm_storage`, `import_storage` — set by the layout engine per node (SE350 standard: `vmbr1`/`local-lvm`/`local`); deploy refuses if unset | **Settled (2026-08-08)** — desired state, stored once, on the object it describes |

## 4b. Console credentials (cloud-init platforms)

Users reach the jump host at the **desktop/console, never SSH** (team,
2026-08-08). So the guest needs a working username+password:

- **Username**: Platform CF `console_user` (seeded `manager`). **Verified**:
  Proxmox `ciuser` overrides only the account NAME — cloud-init still applies
  the template's baked `default_user` groups and sudo to it (tested: a
  `manager` deploy came up in groups `sudo, wireshark, ...` with passwordless
  sudo, no stray `ubuntu`). So the username is a genuine deploy-time SoT value;
  changing it needs no template rebuild.
- **Password**: a single fleet-wide Nautobot **Secret**
  `jumphost_console_password` (text-file provider). The deploy job reads it and
  passes it as Proxmox `cipassword`; Proxmox hashes it before storing (verified:
  `$5$` SHA-256), so plaintext only transits the TLS API call, never at rest,
  never in job logs/inputs. Deploy **refuses** (ContractViolation) if a
  cloud-init platform has no console password Secret — no un-loginable desktop.
- **Verified mechanism**: `ciuser`+`cipassword` makes Proxmox emit
  `user: <u>` + `password:` + `users: [default]`, so cloud-init builds the
  baked default_user (groups preserved) and set_passwords unlocks it
  (overriding the seed's `lock_passwd`), `expire: False` (no forced change).
- **Rotation** (future): update the Secret → converge job re-pushes
  `cipassword` (applies next boot; pairs with the twin-safe reboot guardrail).

## 5. Normalization guardrails (the standing rule, operationalized)

- Job inputs are **object references** (Device, SoftwareVersion), never
  free-form strings, wherever the object exists in Nautobot.
- Prefer **native fields** (Status, Role, primary_ip, interface VLANs) over
  custom fields; custom fields only where no native slot exists (`vmid`).
- Standards (sizing, port maps, storage/bridge names) live in **Git-synced
  config contexts** — one definition, many consumers, still inside the SoT.
- Anything a consumer reads that is not in this document is a bug in this
  document.
