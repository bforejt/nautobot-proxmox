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
| Image checksum | The version's `SoftwareImageFile` carries **`image_file_checksum`** (+ `hashing_algorithm`, default `sha256`). Deploy, Ingest Image and the nested install path **refuse** a record without one — the node-side `download-url` pull is only verified when a checksum is passed, and a pulled file is reused by filename afterwards. `Register Image from Published Set` (and the answer service's media forge) always set it |
| Sizing CFs | `vcpus`, `memory_mb`, `disk_gb` all set on the VNF device (PA-VM: `disk_gb=60` — the image's own virtual size; a too-small value deploys at image size with a warning, never shrinks) |
| Hypervisor linkage | A **Hosted On** relationship from the hypervisor to the VNF |
| Hypervisor record | `primary_ip4` set (API endpoint); CFs `vm_bridge`, `vm_storage`, `import_storage` set (optional `mgmt_bridge` for two-bridge hosts; for PA deploys `import_storage` must also allow **ISO** content — the bootstrap CD lives there) |
| Credentials | Hypervisor CF `secrets_group` names a SecretsGroup, or global Secrets `proxmox_token_id`/`proxmox_token_secret` exist |
| Console password | Secret `jumphost_console_password` (cloud-init platforms only) |
| PA admin password | Secret `pa_admin_password` has a value (pa-bootstrap platforms — ships as a phash in bootstrap.xml) |
| Static-IP guests | Their prefix contains exactly one IP with role **DefaultGW** (DHCP guests don't need it) |
| PA static mgmt | The mgmt prefix additionally contains at least one IP with role **DNS** (lowest address = dns-primary, next = dns-secondary), and `primary_ip4`, when set, must equal the `mgmt` interface's IP |
| Host Baseline (hypervisors, §4c) | `provisioning_state` **`bm_installed`** or **`baseline_done`** (dry run: any installed state); config-context key **`host_baseline`** with `root_email`, `snmp`, `ad`, `service_accounts`, `network` (a section may say `enabled: false`, never be absent); `primary_ip4` on a **bridge** interface named `vmbr<N>`, bonds are **LAG** interfaces named `bond<N>` with CF `lag_mode` (and `lag_xmit_hash` for `802.3ad`/`balance-xor`), every physical member with its `mac_address`, one member flagged `primary_member` on a multi-port active-backup bond; Secrets `ad_bind_password`, the community Secret the context names, `snmpv3_<user>_auth` / `_priv`, and the root login in `host_ssh_username` / `host_ssh_password` |

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
| Host lifecycle stage | CF `provisioning_state` (hypervisors) — states, writers and readers in the §4c state table | Settled (bootstrapped) |

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
| Node name (Proxmox) | `device.name` — for a bare-metal install it becomes the hostname (`<name>.<DOMAIN>`), so it must be a valid hostname label: letters, digits and hyphens, 1-63 chars, no leading/trailing hyphen, not all digits. The install job and the answer service refuse any other name | Settled |
| API endpoint | `device.primary_ip4` | **Settled (2026-08-08)** |
| API credentials | Per-hypervisor **SecretsGroup** named by the device's `secrets_group` CF (Generic/Username = token id, Generic/Secret = token UUID) — each standalone node has its own token; **falls back** to the global `proxmox_token_id`/`proxmox_token_secret` Secret pair when the CF is empty (single-host quickstart) | **Settled (2026-08-08)** |
| BMC/XCC address | **Settled (2026-08-08)**: a dedicated interface named `xcc` on the physical device (SE350, SE455 V3) with its IP assigned — native, visible, cable-truthful. Never a host NIC: it is excluded from NIC-name pinning. The BMC at that IP must report the Device's `serial` (Redfish system SerialNumber, trimmed, case-insensitive): the install and storage-layout jobs refuse before any BMC write otherwise, and when the BMC reports no serial (decision #52) | Layout process creates it |
| Install identity (bare-metal loop) | Device role **`NFV`** — re-checked server-side by Install Proxmox Node and Apply Storage Layout before any BMC action (the form filter is UI-only) and by the answer service's allowlist; `device.serial` = the DMI system serial the installer POSTs (the answer service's allowlist key) — **unique**: a serial matching several Devices is refused; `device_type.model` selects the install profile `bmc/profiles/<slug>.yaml`; CF `provisioning_state=awaiting_install` gates the answer; `primary_ip4` (static) needs a DefaultGW-role IP in its own parent prefix (the IP's namespace) and the install port's `mac_address` — **required** for static installs (the answer service never guesses the mgmt NIC) and it must be a MAC the installer actually reports (decision #52). The install port is **derived through the model** (decision #55): the interface carrying `primary_ip4`; a bridge resolves to its single port or the port flagged `primary_member`, a LAG to its single member or the flagged one — the install job and the answer service refuse an ambiguous model (several members without one flag, two flags, the IP on several interfaces) | **Settled (2026-08-09)**; derivation 2026-10-02 (#55) — [baremetal-install.md](baremetal-install.md) |
| Host NIC names (physical nodes) | Profiles with `install.interface_name_pinning` (SE455 V3) pin every physical NIC's Linux name by MAC at install: a Device interface that records its `mac_address` gets its **Nautobot name** as the Linux name (`mgmt` → `mgmt`), unmapped ports get the installer's `nic<N>`; a MAC recorded on a bond (LAG) or bridge interface is never used for pinning (decision #55). Nautobot names outside the Linux rule are transliterated deterministically: lowercase; each run of characters outside `[a-z0-9_]` → `_`; leading/trailing `_` stripped; `p_` prefixed unless it starts with a letter; truncated to 15 (`OCP-1` → `ocp_1`, `1GbE-4` → `p_1gbe_4`). Results must still be 2–15 chars, not `nic<N>`, unique (first wins) — otherwise the port keeps `nic<N>` | **Settled (2026-09-16, decision #51; transliteration #52)** |
| Storage layout (physical nodes) | Not in Nautobot: the DeviceType profile's `storage` (out-of-band RAID volumes, decision #50) and `install` (filesystem, disk filter, data pool/volume) sections — hardware policy as data (#14). Nautobot records only the consequence: `vm_storage`/`import_storage` below | **Settled (2026-09-16)** |
| VM bridge + storage targets | Hypervisor-device CFs `vm_bridge`, `vm_storage`, `import_storage` — set by the layout engine per node (SE350 standard: `vmbr1`/`local-lvm`/`local`; SE455 V3 installed by the loop: `vmbr1`/`DataDrive`/`local`, `DataDrive` being the firstboot-created LVM-thin storage (VG `big-vg`, thin pool `big-lv`) on the data volume); deploy refuses if unset. For PA deploys `import_storage` also holds the per-device bootstrap ISO, so it must allow **ISO** content | **Settled (2026-08-08)** — desired state, stored once, on the object it describes |
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
  `jumphost_console_password` (a record the bootstrap creates — text-file at
  `/opt/nautobot/secrets/<name>` by default; the provider and path prefix are
  job inputs, [getting-started.md §1](getting-started.md#1-connect-the-jobs)).
  The deploy job reads it and
  passes it as Proxmox `cipassword`; Proxmox hashes it before storing (verified:
  `$5$` SHA-256), so plaintext only transits the TLS API call, never at rest,
  never in job logs/inputs. Deploy **refuses** (ContractViolation) if a
  cloud-init platform has no `console_user` CF or no console password Secret —
  no un-loginable desktop. Both resolve in preflight, before the image pull or
  VM create, so the refusal leaves nothing on the node to roll back.
- **Verified mechanism**: `ciuser`+`cipassword` makes Proxmox emit
  `user: <u>` + `password:` + `users: [default]`, so cloud-init builds the
  baked default_user (groups preserved) and set_passwords unlocks it
  (overriding the seed's `lock_passwd`), `expire: False` (no forced change).
- **Rotation** (future): update the Secret → converge job re-pushes
  `cipassword` (applies next boot; pairs with the twin-safe reboot guardrail).

## 4c. Host baseline (decisions #54/#55)

What the `Host Baseline (SoT-driven)` job and the install's firstboot read
to turn an installed node into a fleet node — design and placement in
[host-baseline.md](host-baseline.md), runbook in
[baremetal-install.md](baremetal-install.md#host-baseline-after-verification).
Principle (Brian, 2026-10-02): **the SoT is the repository of facts.**
Per-node facts sit on the Device and its interfaces, fleet/site settings in
config contexts, credentials in Secrets, hardware policy in the DeviceType
profile; the code carries mechanics only, and a missing or ambiguous fact is
a named refusal before anything touches the node.

### The interface model (native fields first)

| Need | Source | Notes |
|---|---|---|
| Physical ports | Device interfaces with a physical `type` and **`mac_address`** | Matched to the node's NICs **by MAC** (the permanent address when enslaved), never by name; the Linux name is whatever the node calls that MAC (pinned at install, #51) |
| Bonds | Interfaces of type **`lag`** named **`bond<N>`**; members via each port's native **LAG** field (`Interface.lag`) | PVE types bonds by name. Mode/hash in the custom fields below; MTU on the record (`mtu`); a member's MTU, when set, must equal its bond's |
| Bridges | Interfaces of type **`bridge`** named **`vmbr<N>`**; ports via the native **Bridge** field (`Interface.bridge`) — a bond or a physical port | `mode: tagged-all` → VLAN-aware (`bridge-vids 2-4094`); `mode: tagged` → VLAN-aware with the interface's tagged VLANs; no mode → plain bridge; `access` is refused |
| Management address | `device.primary_ip4`, assigned to the **management bridge** (e.g. `vmbr0`) | Rendered as the bridge's `address`; the gateway is the DefaultGW-role IP in the IP's own parent prefix (exactly one, §3). No other address may sit on the topology, and an interface outside it carrying an address is refused (except `xcc`) |
| Install NIC | Derived: `primary_ip4`'s bridge → its port (or the port flagged `primary_member`) → if a bond, its member flagged `primary_member` (or its only member) | Feeds the answer's NIC filter and the nested VM's MAC; the same port stays the bond's primary afterwards, so the cable that carried the install keeps carrying management |

Custom fields on **dcim.interface** (bootstrap-created, grouping NFV):

| Key | Type | On | Values / rule |
|---|---|---|---|
| `lag_mode` | select | LAG interfaces (**required** on each) | `balance-rr`, `active-backup`, `balance-xor`, `broadcast`, `802.3ad`, `balance-tlb`, `balance-alb` — the bootstrap creates exactly the code's choices (it never deletes one you added). Fleet: data bond `802.3ad` (+ MTU 9000, #54), management bond `active-backup` |
| `lag_xmit_hash` | select | LAG interfaces | `layer2`, `layer2+3`, `layer3+4`, `encap2+3`, `encap3+4`, `vlan+srcmac`. **Required** for `802.3ad` and `balance-xor` (must match the switch side), refused on modes that do not use it |
| `primary_member` | boolean | member ports (of a bond or a bridge) | Exactly one per multi-member `active-backup` bond (→ `bond-primary`); on any bond or multi-port bridge it names the install port. Two flags in one group are refused |

Fleet-uniform bond settings come from the config context (below):
`network.bond_miimon` (required when a bond exists) and `network.lacp_rate`
(required when an `802.3ad` bond exists).

### Config context `host_baseline`

One top-level key, partial contexts merged by Nautobot (fleet → site →
device). The bootstrap keeps a ConfigContextSchema **`nfv-host-baseline`**
equal to the code's shape (types, enums, patterns — no `required`, because
contexts are partial); attach it to the contexts that carry the key. The
job checks required-ness on the merged result.

| Key | Required | Consumer | Meaning |
|---|---|---|---|
| `packages` | no | firstboot + job | Extra Debian packages; **`lldpd` and `snmpd` are always installed** (the baseline's own dependencies) |
| `serial_console.speed` (`word`, `parity`, `stop`) | no | firstboot | Line settings for the port the DeviceType profile declares (`install.serial_console.unit`). Speed ∈ 9600/19200/38400/57600/115200; 8N1 unless set. Profile port + this block → GRUB + kernel console + `serial-getty`; either side missing → logged and skipped, never a refusal |
| `zfs_arc_max_bytes` | no | firstboot | `options zfs zfs_arc_max=` (≥ 64 MiB) + initramfs rebuild; absent = ZFS default |
| `remove_subscription_nag` | no (default false) | firstboot | Installs the project's subscription-nag hook (script + apt post-invoke) |
| `root_email` | **yes** | job (AD step) | `root@pam` e-mail |
| `snmp` | **yes** (or `{enabled: false}`) | job | `contact` (**required**, sysContact); `community_secret` (Secret name of the v2c read-only community; absent = no community); `community_source` (CIDR the community is limited to); `community_view` / per-user `view` (`systemonly`, the only view the rendered file defines; absent = full read-only); `v3_users` (names, or `{name, auth_protocol: SHA/SHA-224/256/384/512, priv_protocol: AES/AES-192/AES-256, auth_secret, priv_secret}` — default SHA/AES and Secrets `snmpv3_<name>_auth` / `_priv`). At least a community or one v3 user. **sysLocation is the Device's Location name** (not the path: a reorganised hierarchy never churns monitoring tags) |
| `ad` | **yes** (or `{enabled: false}`) | job | `realm`, `domain`, `servers` (1–2), `mode` (`ldap` / `ldaps` / `ldap+starttls`), `base_dn` (the directory's base DN, e.g. `DC=example,DC=net` — never server names), `bind_dn` (the bind user's DN), `sync_job.name`, `sync_job.schedule` (systemd calendar event), `admin_group` (the **PVE group id**: synced AD groups are named `<group>-<realm>`), `admin_role` — all **required**. Optional: `port` (PVE default per mode), `verify`, `bind_password_secret` (default `ad_bind_password`), `user_filter`, `group_filter`, `sync_attributes` (e.g. `email=mail`), `sync_defaults_options`, `case_sensitive`, `comment`, `admin_path` (default `/`), `default_realm` (default **true** — AD is the default login realm, #54), `sync_job.scope` (default `both`), `sync_job.enable_new`, `sync_job.remove_vanished`. Options the SoT leaves out are not managed |
| `service_accounts` | **yes** (`[]` = none) | job | List of `{user, token, role, path, privsep, name}`: `user` in the `pam` or `pve` realm (never `root@pam` or the firstboot `svc-nfv@pve`), `token` id, `role` (must exist on the node), `path` (default `/`), `privsep` **required** boolean, `name` = SecretsGroup suffix (default: the user part, e.g. `datadog`; `proxmox` is reserved). The job owns these identities' ACLs: entries not in the SoT are removed |
| `network` | **yes** (or `{enabled: false}`) | job | `bond_miimon` (required with any bond), `lacp_rate` (`slow`/`fast`, required with an `802.3ad` bond), `rollback_seconds` (60–900, default 180 — the window in which the job must reconnect after the apply) |

Recommended service accounts (decision #54): `datadog@pam` → `PVEAuditor` on
`/`; `pdm@pve` → `Administrator` on `/` with `privsep: false` (what Proxmox
Datacenter Manager's own enrollment produces; revisit when PDM documents a
minimum).

### Secrets

| Secret (record name) | Created by | Used for |
|---|---|---|
| `host_ssh_username` / `host_ssh_password` | bootstrap (existing) | The job's root SSH login (the same pair the host-verification job uses) |
| `ad_bind_password` (or `ad.bind_password_secret`) | bootstrap | Written on the node to PVE's realm credential file `/etc/pve/priv/realm/<realm>.pw` — where `pveum --password` would store it — never on an argv |
| `snmp_community` (the name `snmp.community_secret` gives) | bootstrap (the conventional name, plus any name a context references) | `rocommunity`/`rocommunity6` in `/etc/snmp/snmpd.conf` (mode 0600). 1–64 printable characters, no spaces/quotes/`#`/backslash |
| `snmpv3_<user>_auth` / `snmpv3_<user>_priv` (or the names a v3 user gives) | bootstrap, for every v3 user named in a config context at bootstrap time | `createUser` in snmpd's persistent file with snmpd stopped; 8–128 printable characters, no double quote or backslash |

All are records the bootstrap creates under the provider and path prefix its
inputs choose — by default text-file at `/opt/nautobot/secrets/<name>`, the
composer layout (values via `./add-secret.sh` there). Values resolve with the Device as
`obj`, so a record's path may be templated per site (e.g.
`/opt/nautobot/secrets/{{ obj.location.name }}-snmp-community`).

**Per-node token SecretsGroups** — written by the job, named exactly like
the firstboot deploy token's `<node>-proxmox`: **`<node>-<name>`** (e.g.
`pve-se455-01-datadog`, `pve-se455-01-pdm`) with Generic/**Username** = the
token id (`datadog@pam!datadog`) and Generic/**Secret** = the value, over
text-file Secrets `<slug>-<name>-token-username` / `-secret` in
`/opt/nautobot/secrets/nodes/` (the Celery worker's read-write mount —
nautobot-composer#66). A token that exists on the node
while its Secrets are missing (or rejected by the node with 401) is
**rotated**.

### provisioning_state

| State | Set by | Read by |
|---|---|---|
| `awaiting_install` | the operator (intent) | answer service allowlist; Install Proxmox Node |
| `bm_installed` | answer service (post-install webhook, or the credentials phone-home) | Host Baseline (eligible); the install job's watch |
| `baseline_done` | **Host Baseline** after a complete, non-dry run (from `bm_installed`; a re-run keeps it) | Host Baseline (re-runnable, drift checks) |
| `fabric_done`, `vms_deployed`, `handed_off` | planned layers (plan-of-attack §3) | Host Baseline accepts them for a **dry run** only |

### Example config context (fictional values)

A fleet context (weight 1000, all NFV-role devices) plus a per-site one
would split this; shown merged. Values are example-only:

```yaml
host_baseline:
  packages: [snmp]
  serial_console: {speed: 115200}
  zfs_arc_max_bytes: 17179869184        # 16 GiB
  remove_subscription_nag: true
  root_email: noc@example.net
  snmp:
    contact: "Example NOC <noc@example.net>"
    community_secret: snmp_community
    community_source: 192.0.2.0/24
    v3_users:
      - {name: datadog, auth_protocol: SHA, priv_protocol: AES}
  ad:
    realm: EXAMPLE-AD
    domain: example.net
    servers: [192.0.2.10, 192.0.2.11]
    mode: ldap
    port: 389
    base_dn: DC=example,DC=net
    bind_dn: CN=svc-pve,OU=Service Accounts,DC=example,DC=net
    user_filter: (memberOf=CN=PVE-Admins,OU=Groups,DC=example,DC=net)
    group_filter: (cn=PVE-Admins)
    sync_attributes: email=mail
    sync_defaults_options: remove-vanished=acl;entry;properties
    case_sensitive: false
    comment: Example AD realm
    default_realm: true
    sync_job: {name: pve-admins-sync, schedule: "*-*-* 06:00:00", scope: both, enable_new: true}
    admin_group: PVE-Admins-EXAMPLE-AD
    admin_role: Administrator
  service_accounts:
    - {user: datadog@pam, token: datadog, role: PVEAuditor, path: /, privsep: false}
    - {user: pdm@pve, token: pdm, role: Administrator, path: /, privsep: false}
  network:
    bond_miimon: 100
    lacp_rate: fast
    rollback_seconds: 180
```

Where each setting of the tester's post-deploy script now lives (its
values are that site's facts: they go into that site's Nautobot, never into
code or this public repo):

| Script variable | SoT home |
|---|---|
| `AD_REALM`, `AD_DOMAIN`, `AD_SERVER1`/`AD_SERVER2`, `AD_BASE_DN`, `AD_BIND_DN`, `AD_USER_FILTER`, `AD_GROUP_FILTER` | `ad.realm`, `ad.domain`, `ad.servers`, `ad.base_dn`, `ad.bind_dn`, `ad.user_filter`, `ad.group_filter` (+ `mode: ldap`, `port: 389`, `case_sensitive: false`, `comment`, `sync_defaults_options: remove-vanished=acl;entry;properties` as the script passed them) |
| `AD_BIND_PASSWORD` | Secret `ad_bind_password` |
| `AD_SYNC_NAME`, `AD_SYNC_SCHEDULE` (`--scope both --enable-new 1`) | `ad.sync_job.name`, `.schedule`, `.scope: both`, `.enable_new: true` |
| `PVE_ADMIN_GROUP` (+ the hard-coded `Administrator`) | `ad.admin_group` (the synced `<group>-<realm>` id), `ad.admin_role` |
| `ROOT_EMAIL` | `root_email` |
| `SNMP_LOCATION` | the Device's **Location name** |
| `SNMP_CONTACT` | `snmp.contact` |
| `SNMP_COMMUNITY` | Secret named by `snmp.community_secret` (conventionally `snmp_community`) |
| `SNMPV3_USERS` (`name:authpass:privpass`, SHA/AES, `-V systemonly`) | `snmp.v3_users: [{name, view: systemonly}]` + Secrets `snmpv3_<name>_auth` / `_priv` |
| `SERIAL_UNIT`, `SERIAL_SPEED` | profile `install.serial_console.unit`; `serial_console.speed` |
| `ZFS_ARC_MAX_BYTES` | `zfs_arc_max_bytes` |
| `DATADOG_ROLE`, `PDM_ROLE` and the `datadog-token` / `pdm-token` ids | `service_accounts` (roles per decision #54: PVEAuditor / Administrator) |
| `MGMT_NICS`, `TRUNK_NICS`, `MGMT_CIDR`, `MGMT_GW` | the interface model above: LAG members by MAC, `primary_ip4` on the management bridge, the DefaultGW-role IP |
| `DATA_DISK`, `DATA_VG`, `DATA_POOL`, `DATA_STORAGE_ID` | the DeviceType profile (`install.data_volume`, decision #54 names) — firstboot, already built |

### Example interface model (an SE455 V3)

| Interface | Type | LAG / Bridge field | MAC | Other |
|---|---|---|---|---|
| `mgmt0` | 1000base-t | LAG `bond1` | port MAC | `primary_member: true` |
| `mgmt1` | 1000base-t | LAG `bond1` | port MAC | |
| `data0`, `data1` | 10gbase-x-sfpp | LAG `bond0` | port MACs | `mtu 9000` |
| `bond1` | lag | Bridge `vmbr0` | — | `lag_mode: active-backup` |
| `bond0` | lag | Bridge `vmbr1` | — | `lag_mode: 802.3ad`, `lag_xmit_hash: layer3+4`, `mtu 9000` |
| `vmbr0` | bridge | — | — | `primary_ip4` assigned here |
| `vmbr1` | bridge | — | — | `mode: tagged-all`, `mtu 9000` |
| `xcc` | 1000base-t | — | BMC MAC | the BMC IP (never rendered, never pinned) |

Rendered on the node (Linux names from the MAC match): `bond1`
(active-backup, `bond-primary mgmt0`, miimon 100) under `vmbr0`
(`address`/`gateway`), `bond0` (802.3ad, layer3+4, lacp-rate fast, MTU 9000)
under the VLAN-aware `vmbr1` (`bridge-vids 2-4094`, MTU 9000) — the
tester's layout, with every value from the SoT.

## 5. Normalization guardrails (the standing rule, operationalized)

- Job inputs are **object references** (Device, SoftwareVersion), never
  free-form strings, wherever the object exists in Nautobot.
- Prefer **native fields** (Status, Role, primary_ip, interface VLANs) over
  custom fields; custom fields only where no native slot exists (`vmid`).
- Standards (sizing, port maps, storage/bridge names) are **stamped onto the
  objects they describe** by the layout process — fully materialized per-device
  and per-platform records, no runtime file or config-context lookups. The
  "define once" DRY lives in the layout engine's templates. **One scoped
  exception (decisions #54/#55):** fleet and site settings of the host
  baseline (AD realm, SNMP, service accounts, serial line settings, ...) live
  in config contexts under the single key `host_baseline` (§4c) — they are
  genuinely shared by every node of a site or the fleet and have no native
  slot; the per-node facts (bonds, bridges, MACs, addresses) stay on the
  Device's own interfaces.
- Anything a consumer reads that is not in this document is a bug in this
  document.
