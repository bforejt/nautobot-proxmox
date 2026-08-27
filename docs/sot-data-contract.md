# SoT Data Contract — What the Deploy Jobs Read

Governing rule (team, 2026-08-08): **always use the SSoT when possible; when not,
keep data as normalized as possible.**

Division of labor: a separate layout process (Network to Code (NtC) Design
Builder App or equivalent) creates all site intent **in Nautobot** — IPAM
carve, devices, interfaces, IPs, Hosted On relationships, per-device specifics.
The jobs in this repo never invent that data; they read it and converge
infrastructure toward it. This document is the contract: the exact conventions
consumers depend on. Dated "Settled" annotations record when each convention
was ratified and why — the conventions themselves are all in force.

## 0. Hard requirements enforced by the code (quick reference)

Exact values the deploy job checks — get one wrong and it refuses with a
precise error (fail-closed), so this table is the fast path when debugging a
refusal. A hand-built worked example using all of them is in
[getting-started.md](getting-started.md).

| Requirement | Exact value(s) today |
|---|---|
| VNF `platform` name | Must have an entry in [jobs/lib/platform_facts.py](../jobs/lib/platform_facts.py) — **`ubuntu-jumphost`** and **`paloalto-panos`** deploy today |
| VNF interface names | Must match the platform's NIC rule — `ubuntu-jumphost`: exactly one interface named **`eth0`**; `paloalto-panos`: **`mgmt`** plus **`ethernet1/1`…`ethernet1/N`** contiguous from 1, and any other interface name is refused (all with pinned MACs) |
| VNF Status to deploy | **Planned** (deploy flips it to **Active**; decommission reverses) |
| `software_version` | Set on the device, and its status must be **Active** (Staged is refused — that's the promotion gate) |
| Sizing CFs | `vcpus`, `memory_mb`, `disk_gb` all set on the VNF device (PA-VM: `disk_gb=60` — the image's own virtual size; a too-small value deploys at image size with a warning, never shrinks) |
| Hypervisor linkage | A **Hosted On** relationship from the hypervisor to the VNF |
| Hypervisor record | `primary_ip4` set (API endpoint); CFs `vm_bridge`, `vm_storage`, `import_storage` set (optional `mgmt_bridge` for two-bridge hosts; for PA deploys `import_storage` must also allow **ISO** content — the bootstrap CD lives there) |
| Credentials | Hypervisor CF `secrets_group` names a SecretsGroup, or global Secrets `proxmox_token_id`/`proxmox_token_secret` exist |
| Console password | Secret `jumphost_console_password` (cloud-init platforms only) |
| PA admin password | Secret `pa_admin_password` has a value (pa-bootstrap platforms — ships as a phash in bootstrap.xml) |
| Static-IP guests | Their prefix contains exactly one IP with role **DefaultGW** (DHCP guests don't need it) |
| PA static mgmt | The mgmt prefix additionally contains at least one IP with role **DNS** (lowest address = dns-primary, next = dns-secondary), and `primary_ip4`, when set, must equal the `mgmt` interface's IP |

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
| Platform behavior (day-0 builder, machine type, serial console, NIC model) | `device.platform` → facts in code ([jobs/lib/platform_facts.py](../jobs/lib/platform_facts.py)) + tunables as Platform CFs | Settled — see "Platform behavior" in §3. The platform *name* must have a facts entry or deploy refuses |
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
  platform facts table ([jobs/lib/platform_facts.py](../jobs/lib/platform_facts.py))
  encodes that name↔index map once — as a fixed `nic_order` list for
  fixed-NIC platforms, or as a **pattern** for variable-count platforms; the
  layout names the device's Interfaces with the *guest's* names; deploy
  renders `netN` in that order. Concretely today: `ubuntu-jumphost` →
  `["eth0"]`, so a jump-host device must have an interface named exactly
  `eth0`; `paloalto-panos` → position 0 = `mgmt`, positions 1..N =
  `ethernet1/1…ethernet1/N` where N is however many dataplane Interfaces the
  device models — **strict/fail-closed**: indices must be contiguous from 1
  (canonical digits, no `ethernet1/01`), and an interface matching neither
  `mgmt` nor the pattern is a refusal, because it would silently never reach
  the VM. Note the PA case leans on this harder than Linux does: PAN-OS maps
  interfaces by PCI-ID with no MAC-match fallback (that's also why the PA
  `machine_type` CF should be pinned to an exact `pc-q35-X.Y` after lab
  validation — a QEMU machine-version bump can reorder PCI). Three
  reinforcements:
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
  For pa-bootstrap platforms the **`mgmt` interface's IP is the addressing
  authority** (it feeds init-cfg); a `primary_ip4` that diverges from it is a
  refusal, not a tiebreak. Dataplane interface IPs modeled in Nautobot are
  *PAN-OS configuration intent* — applied via PAN-OS config management, never
  by the hypervisor (cloud-init `ipconfigN` cannot configure PAN-OS and the
  deploy job pushes no ci values to PA VMs).
- **Bridge selection (settled 2026-08-27)**: every NIC renders onto the
  hypervisor's `vm_bridge` — except position 0 on platforms with a
  **dedicated mgmt NIC** (pattern platforms like PA), which lands on the
  optional hypervisor CF **`mgmt_bridge`** when set. This carries the
  two-bridge SE350 host model (decision #20: `vmbr0` mgmt / `vmbr1` data).
  Fixed-list single-NIC guests (the jump host's `eth0`) always stay on
  `vm_bridge` — that NIC is their access NIC, not a mgmt NIC. Leaving
  `mgmt_bridge` empty preserves single-bridge behavior exactly.
- **Scope note**: PA dataplane modeling is **L3/tagged only for now** —
  vwire/L2 deployments (hypervisor-assigned MACs, promiscuous bridge ports)
  are out of contract until a design needs them.
- **Gateway — settled (2026-08-08)**: the default gateway IP in each prefix
  carries IPAddress **Role = `DefaultGW`** (team's standardization of the
  existing "Default Gateway" role; named to acknowledge that *other* gateways
  can coexist in a subnet — FHRP addresses keep their `VRRP`/`HSRP`/`VIP`
  roles). Contract: **exactly one `DefaultGW`-role IP per prefix**; consumers
  resolve gateway = the DefaultGW IP within the interface's prefix. The layout
  process applies the role per subnet; renaming legacy "Default Gateway"
  records is a team data-migration task.
- **DNS — settled (2026-08-27), same pattern as the gateway**: resolver IPs
  in a prefix carry IPAddress **Role = `DNS`** (bootstrap-created). Consumers
  that need resolvers read the DNS-role IPs inside the interface's prefix —
  **lowest address = primary, next = secondary** (explicit, deterministic
  ordering). First consumer: the PA static
  init-cfg (`dns-primary`/`dns-secondary`) — deploy **refuses** a static PA
  mgmt prefix with no DNS-role IP (the firewall cannot license or fetch
  content without resolution). DHCP-addressed guests still learn resolvers
  from DHCP. NTP remains unconsumed/planned.

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
| VM bridge + storage targets | Hypervisor-device CFs `vm_bridge`, `vm_storage`, `import_storage` — set by the layout engine per node (SE350 standard: `vmbr1`/`local-lvm`/`local`); deploy refuses if unset. For PA deploys `import_storage` also holds the per-device bootstrap ISO, so it must allow **ISO** content | **Settled (2026-08-08)** — desired state, stored once, on the object it describes |
| Mgmt bridge (optional) | Hypervisor CF `mgmt_bridge` — the dedicated mgmt NIC (position 0 of pattern platforms only) lands here when set (two-bridge hosts, SE350 standard: `vmbr0`); empty = everything on `vm_bridge`; fixed-list guests always use `vm_bridge` | **Settled (2026-08-27)** |

## 4a. PA-VM day-0 (pa-bootstrap platforms)

The `pa-bootstrap` builder renders the VM-Series bootstrap package
(init-cfg.txt + a minimal bootstrap.xml + optional authcodes), masters it into
a per-device CD image (`<device>-bootstrap.iso`), uploads it directly to the
target node (never via the firmware HTTP path — it carries credentials), and
attaches it in place of the cloud-init drive. What it reads:

- **Mgmt mode**: device CF `pa_mgmt_mode` — `standalone` (default when empty)
  or `scm` (adds `panorama-server=cloud` + the SCM registration PIN pair from
  Secrets `scm_registration_pin_id`/`scm_registration_pin_value`). Decision
  #2: per-VM attribute, never a code branch.
- **Addressing**: `mgmt` interface IP set → static init-cfg (IPv4 + netmask +
  DefaultGW-role gateway + DNS-role resolvers from the mgmt prefix); no IP →
  `dhcp-client`. Static is the *verifiable* path — **DHCP deploys are
  unverifiable** (no guest agent, no SoT IP to probe) and end with an
  explicit warning. The device name doubles as the PA hostname and the
  bootstrap-ISO filename, so it must be 1-31 chars of letters/digits/`._-`
  (refused otherwise).
- **Admin password**: Secret `pa_admin_password` (REQUIRED) — shipped as an
  md5-crypt **phash** in bootstrap.xml, never plaintext, so the firewall
  never answers on mgmt as admin/admin.
- **Licensing**: Secret `pa_authcode` (OPTIONAL) → `/license/authcodes`;
  absent = unlicensed boot (capacity-limited — fine for lab validation).
  Decommission warns to deactivate licenses before destroy, and the VM UUID
  is pinned to the device's own UUID so redeploys keep the same PA serial.
- **Readiness**: static deploys wait for mgmt TCP 443 ("mgmt reachable", NOT
  chassis-ready) — this requires the **Nautobot worker to route to the
  firewall mgmt network**; on success the bootstrap CD is detached and
  deleted (PA reads it on factory-default first boot only). The wait — and
  the CD cleanup it triggers — runs only when the job's "Wait for readiness"
  input is enabled (the default); on a skipped wait, timeout, or DHCP, the
  ISO stays attached with a logged warning — decommission sweeps it.

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
- Standards (sizing, port maps, storage/bridge names) are **stamped onto the
  objects they describe** by the layout process — fully materialized per-device
  and per-platform records, no runtime file or config-context lookups. The
  "define once" DRY lives in the layout engine's templates.
- Anything a consumer reads that is not in this document is a bug in this
  document.
