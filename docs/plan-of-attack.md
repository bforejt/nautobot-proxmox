# Plan of Attack — Nautobot-Driven Proxmox NFV Lifecycle (SE350 Edge Pairs)

Status: proposal, August 2026. Produced from a multi-track research pass (Proxmox VE 9.x
host networking/tuning, per-VNF guest requirements, Nautobot architecture, Proxmox
automation surface, SE350 platform specifics, ops/process design) followed by an
adversarial review. Version-specific claims were verified against current vendor docs;
items still needing hands-on confirmation are tagged `[lab-verify]`, decisions the team
must make are tagged `[decision-needed]` (consolidated in §6).

---

## 1. Assessment & Feedback

### What the framing gets right

- **Replicate-ESXi-first is the correct instinct.** Every element of the ESXi robot has a
  clean Proxmox VE 9.x equivalent, and most of it — bonds, VLAN-aware bridges, VM NIC
  trunking, image import, autoboot ordering, VM serial consoles — is drivable through the
  Proxmox REST API with zero SSH. That is a strictly better position than the ESXi SSH
  robot ever had.
- **VM-level redundancy across independent hypervisors is the right model, with a stronger
  technical justification than familiarity:** a two-node Proxmox corosync cluster with no
  shared storage is *actively harmful*. When one node dies, the survivor loses quorum,
  `pmxcfs` goes read-only, and VMs will not autostart after a power event
  (`cluster not ready - no quorum (500)`). A QDevice workaround would need a third
  always-on box per site. The existing "two standalone servers, redundant VM pairs"
  philosophy is exactly what standalone Proxmox nodes want. **Never cluster these.**
- **Walking slowly is right.** Several tempting features (hugepages, OVS, Proxmox SDN
  zones, Proxmox HA, drift-sync/SSoT) are all correctly deferrable — and some have hard
  blockers anyway (hugepages settings are root@pam-only; API tokens can't set them).
  One nuance surfaced later: the legacy estate runs `sched.cpu.latencysensitivity=high`
  with CPU/memory reservations per VM. KVM has no reservation primitive, so the
  reservation guarantee is reproduced by the no-oversubscription guardrail (admission
  control) + `balloon=0` + host-side confinement (host services and NIC IRQs restricted
  to housekeeping cores); per-VM `affinity` pinning is the documented **escalation
  path**, invoked only if the Phase 2 jitter/latency soak demands it (see
  site-reference-architecture.md, VM tuning table).
- **The colleague's pipeline philosophy is validated:** Nautobot Jobs stay fast and
  re-runnable, long multi-stage work writes status back to Nautobot. Research shows this
  can live *inside* Nautobot (dedicated JobQueue + per-job time limits + job chaining)
  without standing up AWX/Kubernetes in early phases.
- **The bare-metal track is well-aimed** (Redfish virtual media + Proxmox auto-install ISO
  is the right L0 mechanism) — but it needs a real SE350 port, not a config tweak (§1,
  decision 6 below).

### One thing to retire, not port

The "Python robot walks the server through setup via SSH" mental model should be retired.
On Proxmox the SSH surface shrinks to a residue: kernel cmdline (C-states, serial
console), ethtool persistence, package installs. That residue belongs in the
**auto-installer first-boot hook** (one-time, reboot-coupled state), plus one small
idempotent "host baseline" job for day-N tuning changes on already-deployed hosts. The
target architecture: **firstboot hook for one-time host-OS state, REST API for everything
else, one bounded SSH job for tuning drift, SSH otherwise break-glass only.**

The deeper shift: the robot *asks three questions* (site code, subnets, server 1/2) and
then executes. The source-of-truth pattern splits this in two — a **design job** takes
those same three inputs and materializes ALL intent into Nautobot (devices, IPAM carve,
VLANs, VMs, the server-2 skip logic); **deploy jobs** take only a device/VM reference and
converge actual state toward SoT intent, asking nothing. This one decision collapses all
three of your scenarios (lab new-build, field one-off redeploy, RMA rebuild) into a single
code path entered at different layers.

### Significant decisions the framing hasn't called out (decide early — some are one-way)

1. **Cluster vs standalone pair.** Standalone (see above). One-way: joining a node that
   already has VMs into a cluster later is unsupported (`/etc/pve` gets overwritten).
   Model the pairing only in Nautobot. Use Proxmox Datacenter Manager (PDM 1.1.x, stable
   since Dec 2025) later for fleet *visibility* — never as an orchestration dependency.
2. **VLAN-aware bridge, not port-group emulation.** Do not replicate ESXi
   port-group-per-VLAN as bridge-per-VLAN (the colleague's `proxmox-config/` README hints
   at "bridge-per-VLAN mapping" — steer this). The bridge layout mirrors the legacy
   two-vswitch pattern: `vmbr0` (mgmt, = vSwitch0) + `vmbr1` (VLAN-aware "LAN-Trunk"
   on the 10G LACP bond, MTU 9000) — port groups collapse into Nautobot VLAN/VMInterface
   assignments rendered as `tag=`/`trunks=` (§2).
3. **VNF modeling: Device vs VirtualMachine.** Nautobot core cannot pin a VM to a specific
   host Device inside a multi-host cluster, and Golden Config / the `Controller` model
   only work against `dcim.Device`. Recommendation: model each SE350 as its own
   **one-host Cluster** (VM→host becomes implicit), VNFs as `VirtualMachine` records.
   Adopt the dual-record (Device + Relationship) pattern only if Golden Config compliance
   on VNF configs becomes a requirement — decide before populating data at scale, because
   retrofitting is painful.
4. **Nautobot version. Decided: build on the existing 2.4 LTM; upgrade to 3.x as its own
   later effort** (target: before Phase 4, where approval gating becomes load-bearing).
   Facts driving the timing: 2.4 remains LTM until 3.3 ships; 2.4's Django 4.2 passed
   upstream EOL in Apr 2026 (security patches are case-by-case now); 3.0 removed
   `Job.approval_required` in favor of Approval Workflows — migration path is upgrade to
   ≥2.4.15, run `check_job_approval_status`, then re-express gating as Workflows. Pin
   2.4-LTM app trains (GC 2.6.x, SSoT 3.10.x, DLM 3.2.x, design-builder 2.3.x) and keep
   the 3.x-sensitive surface small: approval gating isolated in one place, job libraries
   as pure Python with thin Nautobot wrappers.
5. **Image distribution without shared storage is a solved, pure-API problem** — if
   designed in from day one: the existing **nautobot-composer firmware server** acts as
   the image repo (immutable versioned qcow2/ISO filenames + SHA256 sidecars; HTTPS for
   node `download-url` pulls, plus a plain-HTTP vhost for XCC1 install-ISO mounts) →
   `POST /nodes/{node}/storage/{storage}/download-url` (content=`import`, checksum
   verified) → VM create with `import-from=<volume ID>`.
   Critically, `import-from` with an absolute filesystem path fails for **all** API tokens
   (including root@pam's) — the volume-ID path is not an optimization, it is the only
   token-compatible mechanism.
6. **The SE455 V3 Redfish job will not work on SE350 as-is.** SE350 is first-generation
   XCC: virtual media requires the **XCC Enterprise Feature-on-Demand license** (without
   it the insertable `EXT{N}` members simply don't exist), uses **PATCH on
   `/redfish/v1/Managers/1/VirtualMedia/EXT{N}`** rather than POST `InsertMedia`, and
   accepts **plain HTTP or credential-less NFS only** for ISO URLs. The BMC client needs a
   platform-profile abstraction (per-DeviceType YAML: vmedia method, BIOS attribute map,
   disk filter, license requirements), not an if/else bolted on later — the fleet will
   inevitably be mixed SE350 + successor hardware.
7. **Security Pack confirmed** (the fleet requires ThinkShield activation, which exists
   only on Security Pack units). Consequences are now firm: replacements arrive
   ThinkShield-locked and won't boot until claimed via the Key Vault Portal / Edge app
   (scenario-3 runbook starts with the claim step); transit G-sensor events can trigger
   SED lockup on a lab-built, shipped server (scenario-1 pre-ship procedure must disable
   motion detection or accept portal reactivation on arrival). Both are extensions of the
   team's existing ThinkShield process, not new capabilities. `[lab-verify]` what the
   current ESXi-era ship process does about motion detection — years of successful
   shipments suggest it's already handled or disabled.
8. **Homogeneous pairs during cutover.** A mixed ESXi+Proxmox pair breaks the operational
   model (two consoles, two redeploy paths for one redundant VM pair, mid-incident).
   Realistic rule: **pairs are always homogeneous** — a failure-driven migration converts
   *both* nodes of a pair. Cost that into the cutover policy now.

---

## 2. ESXi → Proxmox Translation Table

| ESXi concept / robot step | Proxmox VE 9.x equivalent | Mechanism |
|---|---|---|
| Interactive robot Q&A (site code, subnets, server 1/2) | Nautobot design job inputs; intent stored in SoT | `SiteNfvDesignJob` |
| Fresh ESXi install (manual/kickstart) | Automated-installer ISO + `answer.toml` | Redfish vmedia mount + one-time CD boot (primary); PXE secondary, official since PVE 9.2 (decision #41); `proxmox-auto-install-assistant` |
| Host power policy = High Performance | Firmware standard confirmed (legacy XCC BIOS tool, carried verbatim in `bmc/se350_bios.yaml`): **Custom Mode** with explicit knobs — C-States/C1E/Energy-Efficient-Turbo disabled, MONITOR/MWAIT enabled, Power/Performance Bias = Platform Controlled → Maximum Performance, Thermal Mode = Performance. Kernel C-state cmdline caps become defense-in-depth (likely redundant with BIOS-level disable — lab-confirm); governor already `performance` with intel_pstate | `bmc/` BIOS policy + firstboot hook |
| vSwitch + LAG (dot1q trunk; today `channel-group mode on` — static, a vSS limitation) | `bond0`: bond-mode `802.3ad`, xmit-hash `layer2+3` (pair with Cisco `src-dst-ip`; 9300 default is `src-mac` — change it), miimon 100. **Switch ports flip `mode on`→`mode active` in the same window** — both mismatch directions black-hole (see site-reference-architecture.md) | `POST /nodes/{n}/network` (staged) + `PUT /nodes/{n}/network` (apply, ifupdown2, live) |
| Port groups (one per VLAN; `LOC_NAME_VlanNum` naming) | ONE VLAN-aware data bridge `vmbr1` ("LAN-Trunk" equivalent; `bridge_vlan_aware=1`, `bridge_vids` = site VLAN list from IPAM — VLAN 1 excluded by default; MTU 9000) + `vmbr0` for mgmt (= vSwitch0). Port-group naming becomes the Nautobot VLAN naming standard; VMInterface↔VLAN assignment replaces the port-group object | Same network API |
| VM vNIC on access port group | `net0: virtio,bridge=vmbr0,tag=<vid>[,queues=<vCPUs>]` | VM config API |
| VLAN 4095 / VGT trunk (PA-VM, C8000v self-tagging) | vNIC with no `tag` (full trunk) or `trunks=<vid;vid;…>` (filtered — preferred, generated from IPAM) | VM config API |
| Port-group "forged transmits / promiscuous" | Not needed — Linux bridge does no MAC anti-spoofing; keep `firewall=0` on VNF dataplane NICs so floating MACs (VRRP, PA HA) are never filtered | VM config |
| NIC tuning (offloads, rings) | `ethtool -K … gro/lro/tso off` on the bridged data path (standard NFV/PA KVM guidance; LRO is kernel-auto-disabled on bridged ports anyway) + X722 `disable-fw-lldp on`; persist via systemd oneshot unit (no API exists for this). Keep GRO on the mgmt path | firstboot hook |
| "Enable TCPIP LRO" robot step | Asserts an ESXi *default* (vmkernel-stack LRO for host-terminated TCP) — no Proxmox action needed; Linux GRO on the mgmt path is the default-on analog | — |
| "Enable Network Queue Pairing" robot step | Also an ESXi default (NetQueue RX/TX thread pairing); functional analog is virtio-net multiqueue `queues=<vCPUs>` on dataplane vNICs | VM config API |
| Golden VMDK deploy | qcow2 in `import`-typed storage; VM create with `import-from=<volume ID>` | `download-url` + VM create APIs |
| Guest customization (Ubuntu) | Native cloud-init (`ide2=<store>:cloudinit`, `--ciuser --sshkeys --ipconfig0`); avoid `cicustom` snippets (no upload API) | Pure API |
| VNF day-0 (PA bootstrap, IOS-XE config) | Generated CD-ROM ISO: PA = `/config/init-cfg.txt` (+ empty `/license /software /content`); C8000v & 9800-CL = `iosxe_config.txt` at ISO root; SD-WAN = unmodified Manager-generated `ciscosdwan_cloud_init.cfg` | Jinja2 → ISO build → storage `upload` API (content=iso) → attach `media=cdrom` |
| VM autostart + start delay (legacy default 15 s) | `onboot=1` + `startup=order=N,up=15` (pve-guests at boot; reverse order at shutdown) | VM config API |
| Host serial → OpenGear | Firmware COM1 console-redirect (Redfish BIOS, standardize 115200 8N1) + kernel `console=tty0 console=ttyS0,115200n8` + GRUB serial. Two persistence paths: `/etc/default/grub` + `update-grub` (GRUB installs) vs `/etc/kernel/cmdline` + `proxmox-boot-tool refresh` (UEFI/ZFS-root) — automation must handle both | `bmc/` policy + firstboot |
| VM console access (legacy: per-VM telnet to host TCP ports through the host firewall) | `serial0: socket` on every VNF (+ `vga=serial0` for serial-primary guests) → `qm terminal <vmid>` natively; per-VM TCP exposure reproduced with socat/ser2net units generated from the VM list, mgmt-VLAN-bound and firewalled to OpenGear/mgmt sources | VM config API + host baseline |
| vCenter (fleet view) | Proxmox Datacenter Manager 1.1.x — visibility/ops only | PDM enrollment token needs `Sys.Audit` at `/` (fixes the colleague's 403) |
| ESXi host firmware updates (OneCLI) | OneCLI does **not** support Debian — use out-of-band XCC web UI / Redfish UpdateService | `bmc/` track |

---

## 3. Architecture Spine

**Layers, each an idempotent job runnable standalone, chained by a thin wrapper:**

```
L0 bare-metal    BMC vmedia (primary; PXE secondary) → auto-install → answer.toml → firstboot
L1 host baseline verify/converge tuning (cmdline asserts, ethtool unit, apt repos, certs)
L2 host network  verify/converge bond0 + vmbr0 (firstboot builds it; job diffs, no-ops)
L3 VM provision  image ingest → VM create from profile → day-0 ISO → autoboot order
L4 VNF day-1     hand-off checks: mgmt reachable, onboarding to Panorama/SCM/Manager/APs
```

Progress is checkpointed on the Device as `provisioning_state`
(`awaiting_install → bm_installed → baseline_done → fabric_done → vms_deployed →
handed_off`). **Hard requirement: every layer job re-checks state on entry and no-ops
completed work** — chained enqueues do not survive worker restarts, so the state field is
the durable resume truth, not the chain.

**Scenario mapping:** new-office build = L0→L4 in lab; RMA rebuild = same wrapper against
existing intent; field one-off = L3 entry only (`RedeployVmJob`), gated by approval.

**Two components live outside Nautobot** (small, but production-critical — they need an
owner, monitoring, and a documented fallback):

- **Answer service** (FastAPI sidecar): receives the installer's DMI-serial POST, looks up
  the Device in Nautobot, renders `answer.toml.j2`, returns it. Security is not optional:
  the answer contains the root password hash and webhook token. HTTPS with the cert
  fingerprint baked into the prepared ISO, the PVE 9.2 fetch auth-token, and a serial
  allowlist — only Devices in `provisioning_state=awaiting_install` get answers. (The
  vmedia ISO itself must be plain HTTP for SE350/XCC1; the answer fetch can and should be
  HTTPS.) Fallback when it's down: per-node baked ISO.
- **Install webhook receiver**: the Proxmox `[post-installation-webhook]` cannot target a
  Nautobot JobHookReceiver (those fire only on Nautobot object CRUD events, not external
  HTTP). The webhook lands on the answer service, which updates `provisioning_state` and
  enqueues the next job via the Nautobot REST API. The webhook payload carries the node's
  SSH host keys and API-token bootstrap (below) — use them.

**Per-node credential bootstrap** (standalone nodes = no cluster-wide auth realm, every
node needs its own service account): firstboot runs `pveum` to create the automation
user + custom role + API token and POSTs the token value (shown exactly once) to the
answer-service webhook endpoint over HTTPS, which stores it into Nautobot Secrets/Vault.
Same payload delivers the node's TLS cert/CA so `proxmoxer` runs with `verify_ssl`
pointing at a real bundle instead of `False`-forever, plus SSH host keys for break-glass
`known_hosts`. Token is lost on reinstall — rebuild path re-runs the same bootstrap.

**Proxmox service-account role** (privilege-separated token) — **validated live on
PVE 9.2.2 (lab NUC, Aug 2026)** as role `NFVAutomation`, user `svc-nfv@pve`, token
`deploy`: `VM.Allocate, VM.Clone, VM.Config.* (enumerated), VM.PowerMgmt, VM.Audit,
VM.Console, Datastore.AllocateSpace, Datastore.AllocateTemplate, Datastore.Audit,
Sys.Audit, Sys.Modify (required for the /nodes/{n}/network endpoints — note it widens
blast radius), SDN.Use`. Confirmed sufficient for: VM create with the full profile,
cloud-init config, power ops, network staged-create + apply, `download-url`, and
`import-from` volume-ID imports. **Critical setup gotcha (bit us in testing): a
privilege-separated token's effective rights are the INTERSECTION of the token's ACLs
and its owning user's ACLs — grant the role to BOTH the user and the token.** Known
gap by design: storage *content deletion* (image rotation) needs `Datastore.Allocate`
— grant it scoped to the import storage only if `IngestImageJob` is to prune old
images, else leave cleanup root-side.

**The most strand-prone step, designed away:** the auto-installer configures management on
one NIC with a simple bridge; naively having a later API job replace that with
bond0 + VLAN-aware vmbr0 means the API call's own transport drops mid-apply. Instead the
**firstboot hook builds the final topology** (bond0 + vmbr0 + mgmt IP) while nothing
depends on the network, and the L2 job becomes verify/converge — diff-then-apply, normally
a no-op. Where the L2 job *does* apply changes on a live node: the staged/apply/revert API
sequence (`DELETE /nodes/{n}/network` discards staged changes) only protects **before**
apply; after a bad apply, recovery is console-only. Gate: **no management-affecting apply
until the OpenGear serial path to that node is proven.**

**Guardrails encoded in jobs, not documented in runbooks:**

- No job may act on both servers of a redundant pair simultaneously.
- `RedeployVmJob` pre-flight verifies the twin VM is healthy — with "healthy" defined per
  VNF type in its profile (Proxmox `running` is trivially satisfied by a hung VNF):
  qemu-guest-agent ping where available, plus a VNF-level probe (PA-VM HA/API state,
  C8000v control connections, at minimum mgmt reachability). The pre-flight must fail
  closed during exactly the partial outages when field redeploys actually happen.
- Oversubscription guardrail (a headline constraint, and written policy: *used CPU/RAM
  must not exceed actuals; storage may be thin-provisioned*): `SiteNfvDesignJob`
  validates sum(vCPU) and sum(RAM) of intended VMs against host actuals at intent time;
  `DeployVmJob` re-checks at deploy time. "Actuals" counts **physical cores, not SMT
  threads** (fleet: 16-core/32-thread D-2183IT, 256 GB): budget = 16 cores minus 2–4
  reserved for host/housekeeping (vhost threads, bridge, mgmt) ≈ 12–14 VNF vCPUs; RAM =
  256 GB minus host overhead (no ZFS ARC to reserve — boot storage is the hardware
  RAID volume with ext4+LVM-thin). Budget `queues=` too —
  multiqueue adds host CPU load. This guardrail **is** the CPU-reservation equivalent —
  KVM has no MHz-floor primitive, so admission control lives here. The same computation
  can emit disjoint `affinity` cpusets (host/housekeeping cores excluded) when the
  pinning escalation is invoked — reproducing ESXi latency-sensitivity=high exclusive
  placement declaratively.
- Replaced VM disks are renamed, not deleted, until post-deploy checks pass.

---

## 4. Phased Plan

Two tracks converge: **Track A** (Nautobot + Proxmox jobs — new work) and **Track B**
(bare-metal — extend the colleague's repo). Read-only precedes write; each phase ships
visible value.

The team's two target processes map directly onto these tracks: **Process 1** ("deploy
images onto an initially provisioned Proxmox server" — pull qcow2/ISO images from the
nautobot-composer server, deploy VMs with cloud-init-style day-0 injection, configure
the virtual network infrastructure, tune the host for realtime network workloads) is
Track A = Phases 1–2 plus the L1/L2 host jobs. **Process 2** ("initially deploy the
server itself") is Track B = Phase 3. The plan keeps Process 1's steps as separately
runnable jobs (image ingest / VM deploy / host network / host baseline) rather than one
monolithic robot — a thin wrapper can still present them as a single operator action,
but field one-off redeploys and partial re-runs need the per-layer entry points.

**Deployment posture:** the primary case is a **net-new build** (lab as-built → ship),
exactly like the legacy robot's normal job — new sites get the target switch config
(LACP, IP-hash, jumbo `system mtu`) from day one and no flip runbooks are involved.
In-place conversion of live ESXi sites is the supported-but-secondary case; the
coordinated-flip and cutover-policy machinery below applies only there.

### Phase 0 — Platform decisions + lab bring-up

Lock the one-way choices; get one SE350 pair + a Nautobot instance in the lab.

- Decided: build on the existing Nautobot 2.4 LTM (pin 2.4-LTM app trains; schedule the
  3.x upgrade as its own effort, ideally before Phase 4). Still to decide: standalone
  pair (recommend yes); VNF-as-VirtualMachine modeling (recommend yes, revisit only if
  Golden Config needed).
- SE350 platform verification checklist on a real unit: XCC Enterprise FoD present? `EXT`
  vmedia members visible? Dump `GET /redfish/v1/Systems/1/Bios` (capture exact
  `OperatingModes_*` / `DevicesandIOPorts_*` attribute spellings). Does the Marvell
  88SE9230 M.2 adapter storage: answered — the fleet's RAID controller presents a
  single volume (individual M.2s not visible at install), so boot storage is
  ext4+LVM-thin on that volume; capture its Linux enumeration (model/serial string)
  for the answer.toml disk filter, and determine what alerting exists for a degraded
  mirror (XCC event vs none)?
  `proxmox-auto-install-assistant system-info` DMI serial matches Nautobot serial? X722
  `disable-fw-lldp` honored at fleet NIC firmware? Current motion-detection/tamper
  configuration on the (confirmed Security Pack) units?
- Manually install PVE 9.x (pin a point release) on one SE350; burn in the 6.14+ kernel
  on Xeon D-2100.
- **Decouple procurement from engineering:** if XCC Enterprise FoD licenses must be
  bought, kick that off now but do not block Phases 1–2 on it (they need no BMC).

Deliverables: decision record; SE350 verification results; Nautobot lab instance with
Git-repository job delivery.
Exit: decisions signed off; SE350 Redfish vmedia proven (or FoD procurement in flight).

### Phase 1 — Site design job: intent into Nautobot (zero device risk)

- `SiteNfvDesignJob`: inputs `site_code`, `supernet`, `server_number` (1/2/pair). Pure
  functions compute the layout (naming, standard IPAM carve → Prefixes/VLANs/IPs, VM set
  with server-2 skip logic); `get_or_create`-style application; `DryRunVar` on every
  mutating job. **Identity trap:** keying objects on computed names means a naming-
  convention change silently creates a duplicate parallel tree — stamp objects with a
  stable design-key custom field, or adopt an explicit no-rename policy with a documented
  decommission/recreate path.
- Data model: ClusterType "Proxmox VE"; one Cluster per SE350 (name = hypervisor
  hostname); ClusterGroup per site holds the pair; VNFs as VirtualMachines with Role +
  Platform + pinned MACs and SMBIOS UUID recorded as intent; `SoftwareVersion` /
  `SoftwareImageFile` (checksum + URL) for every golden image; `ExternalIntegration` +
  SecretsGroup per Proxmox API and XCC endpoint; `provisioning_state` custom field.
  Resolve the lab-staged-build Location question here (build under destination Location
  vs staging Location + move step) `[decision-needed]`.
- Config contexts (Git-managed, schema-validated) for per-site VLAN maps, NIC tuning
  values, serial/OpenGear parameters, image maps, oversubscription policy.

Exit: running the job twice for a site is a no-op the second time; a full site's intent
(2 hosts, all VMs, IPAM) is browsable in Nautobot from three inputs.

### Phase 2 — VM deploy engine, one guest at a time (the ESXi-robot replacement core)

Against a hand-built lab PVE node, API-only. Split so each sub-phase ships alone:

- **2a — Engine + Ubuntu jump host** (easiest guest, native cloud-init, no ISO builder):
  `proxmox_client` library (proxmoxer 2.3+, token auth, task-UPID polling);
  `IngestImageJob` (download-url + checksum verify); `DeployVmJob` — the generic
  "qcow2 import + optional bootstrap ISO" engine with per-VNF profile objects (cpu=host,
  `sockets=1` + cores per profile, numa=1, `balloon=0` (memory fully committed —
  reservation parity), `cpuunits` weighting VNFs above utility VMs, optional
  `affinity=<cpuset>` escalation from the design job's disjoint core assignment
  (root@pam-only option, applied via the `HostBaselineJob` root-context step — §6 #31;
  invoked only on measured jitter), machine type
  with **pinned version**, NIC list with bridge/tag/trunks/queues/pinned MAC (virtio
  `mtu=1` inherits bridge MTU), `smbios1 uuid=` from Nautobot, `serial0: socket`,
  onboot + `startup` (default up=15), firewall=0 on dataplane NICs, no
  cpulimit/rate/I-O caps, qemu-guest-agent where supported) and pluggable `iso_builder`
  types. Pre-flight before any write: Nautobot Device ↔ node hostname **and** DMI
  serial match, storage target exists/is-intended/has-capacity (replaces the robot's
  name-match and datastore sanity checks). Destroy-and-recreate semantics (day-0 ISOs
  are first-boot-only).
- **2b — PA-VM**: `pa_bootstrap` builder (init-cfg.txt tree); q35 + SeaBIOS + virtio,
  pinned q35 machine version validated per PAN-OS release `[lab-verify — history of
  version-specific boot failures]`. Management model decided: **standalone or SCM, no
  Panorama** — the builder ships two template variants: `standalone` (full local day-0
  mgmt/admin config; licensing via auth code in the bootstrap `/license` folder or CSP
  activation) and `scm` (minimal init-cfg with `panorama-server=cloud` +
  `vm-series-auto-registration-pin-id/value` from the CSP portal). Note Proxmox is absent
  from PA's qualified-hypervisor matrix — accept that support posture explicitly
  `[decision-needed]`.
- **2c — Cisco guests**: `iosxe_config` builder (C8000v autonomous — use the `_serial`
  qcow2 variant, set `platform hardware throughput level` day-0 since the image boots with
  a 10 Mbps shaper; 9800-CL small template, 3-vNIC Gi1/Gi2/Gi3 layout, day-0 must include
  `wireless country`, WMI, `vwlc-ssc` cert, `platform console serial`); `sdwan_cloud_init`
  builder (pass-through of the Manager-generated file). vEdge Cloud is EoS — C8000v
  controller mode is the only SD-WAN edge worth automating.
- `RedeployVmJob` as a **normal Job with `ObjectVar(VirtualMachine)`** so approval gating
  works (`approval_required` on 2.4 today; Approval Workflows after the 3.x upgrade —
  receiver-class jobs are not reliably approval-gateable on either); a JobButton on the
  VM page is a lab-only convenience wrapper. Twin-health pre-flight per §3.
  `is_singleton`.
- Dedicated JobQueue + worker; per-job `soft_time_limit`/`time_limit` (celery's default
  hard limit SIGKILLs long jobs).

Exit per sub-phase: VNF boots from Nautobot intent with day-0 config applied and mgmt
reachable, zero SSH. 2a additionally stands up the **latency/jitter measurement
harness**; 2b/2c include a datapath latency/jitter soak against acceptance thresholds —
the decision gate for the per-VM pinning escalation (§6 #31). Onboarding criteria
(PA→SCM registration, AP joins) are separate line items gated on the licensing/BOM
decisions in §6.

### Phase 3 — Bare-metal track: port L0 to SE350 + close the loop (parallel with Phase 2)

- Refactor `xcc_client.py` to dual-mode vmedia: `EXT{N}` members present → XCC1 path
  (PATCH on member, select by `Id` prefix "EXT", HTTP-only ISO URL); else XCC2 path (POST
  InsertMedia). Fail explicitly on missing Enterprise FoD ("EXT members absent"). Fix
  `_select_cd_media()` (MediaTypes doesn't distinguish insertable members on XCC1).
- **Platform profiles** as data (per Nautobot DeviceType YAML in `bmc/`): vmedia method,
  BIOS attribute map, disk filter, NIC naming, license requirements, ThinkShield steps.
- `ApplyBiosPolicyJob`: generic Redfish `Bios/Settings` PATCH → reboot → readback-verify,
  driven by the YAML. `bmc/se350_bios.yaml` is seeded: `legacy_standard` carries the
  team's existing XCC BIOS tool settings verbatim (Custom Mode + explicit
  C-state/power/PCI knobs — the CustomMode-not-preset pattern the tool already
  follows); `proposed_additions` (COM1 console redirect explicit, serial-port sharing
  off, Secure Boot off) pend team sign-off. Exact Redfish attribute spellings/enums
  get verified by the discovery job dump before first PATCH `[lab-verify]`.
- ISO/answer decision resolved: **one generic prepared ISO + HTTP answer fetch**
  (verified Aug 2026 against installer source: the answer request POSTs DMI
  system/baseboard/chassis serials, UUID, product name, and every NIC's link+MAC —
  per-node server-side rendering is the documented pattern; PVE 9.2 adds a bearer
  `--answer-auth-token` so the service can authenticate nodes). **Delivery decided
  (2026-08-09, decision #41): Redfish vmedia primary, PXE secondary.** PVE 9.2's
  `prepare-iso --pxe --pxe-loader ipxe` emits the *same* prepared artifact as
  kernel + initrd + iPXE snippet, so PXE is a delivery-layer option, not a fork —
  its caveats (full ~1.6–1.9 GB ISO into RAM ≥4 GB; UEFI+HTTP the reliable combo;
  needs DHCP/proxyDHCP; Secure-Boot-over-PXE undemonstrated) make it the lab/staging
  path and the unlicensed-BMC escape hatch. Discovery mode follows delivery: vmedia
  at field sites (no DHCP control) → baked `--url` + cert fingerprint (accepting one
  ISO per PVE release × endpoint); PXE/lab → DHCP option 250
  (`proxmox-auto-installer-manifest-url`) or DNS TXT
  `proxmox-auto-installer.<search-domain>`. CI runs `proxmox-auto-install-assistant
  validate-answer` on rendered answers. answer.toml: **ext4 + LVM-thin on the
  hardware-RAID volume** (the Marvell controller presents a single disk — fleet
  standard; no ZFS, no ARC reservation), disk filter matched to the RAID volume's
  model/serial string so a data disk can never be selected.
- Firstboot hook (small fetch-and-exec stub): kernel cmdline (C-states, serial console —
  both GRUB and proxmox-boot-tool paths), ethtool/`disable-fw-lldp` systemd oneshot, NIC
  name pinning, **final network topology** (`vmbr0` = active-backup bond on the copper
  pair, untagged; f1/f2 10G 802.3ad bond → VLAN-aware `vmbr1`, MTU 9000 on the data
  path pending the switch-jumbo answer), KSM disabled, **host-service confinement**
  (`system.slice`/`user.slice` `AllowedCPUs=` → housekeeping cores; NIC IRQ affinity
  steered there — the "reserve the host away from VNFs" half of reservation parity),
  lldpd with CDP mode, apt repo config (no-subscription) + point-release pin,
  qemu-guest-agent-ready defaults, socat/ser2net per-VM console exposure scaffolding,
  root SSH key, **pveum credential bootstrap**, phone-home webhook.
- `HostBaselineJob` (L1, bounded idempotent SSH): day-N remediation for firstboot-domain
  settings (new ethtool flag, cmdline change) on in-service hosts — the alternative is
  "tuning changes require rebuild," which is not acceptable for field fleets.
- Replace free-form BMC-IP job input with `ObjectVar(Device)` + ExternalIntegration; write
  state back to the Device (the current job writes nothing back).
- **Lab install infrastructure as a named deliverable with an owner and bring-up order:**
  plain-HTTP ISO vhost (XCC1 constraint), HTTPS answer service, HTTPS image repo — the
  first and third are roles of the existing nautobot-composer server; the answer service
  is the new component.

Exit: blank SE350 → racked in lab → one Nautobot job → fully tuned PVE node with
bond/bridge up and `provisioning_state=fabric_done`, no human between.

### Phase 4 — End-to-end scenarios + hardening

- `SiteBuildJob` thin wrapper: L0→L4 per device with `provisioning_state` checkpointing.
  Scenario 3 = same wrapper against existing intent (RMA runbook starts with the
  ThinkShield claim — fleet confirmed Security Pack — and includes the PA-VM licensing
  caveat: the VM-Series serial derives
  from **UUID + CPUID**, so pinned SMBIOS UUID is necessary but not sufficient — enforce
  same-CPU-SKU spares or pre-plan the PAN support re-map step).
- **Scenario-1 ship procedure** (was missing from every earlier draft): pre-ship job —
  disable motion/tamper SED lock on Security Pack units, graceful VM+host shutdown,
  verify serial/OpenGear config persisted, record an as-built audit snapshot; post-arrival
  turn-up verification — power, LACP converged, VMs autostarted in order, twin-pair
  health.
- `AuditNodeJob` (read-only compliance): diff actual vs intent — network config, VM
  configs, onboot/startup, governor + `/proc/cmdline` asserts, image checksums,
  agent-reported IPs vs SoT. Report drift, never auto-fix in this phase.
- **Field pilot milestone** (scenario 2 is not "supported" until this passes): read-only
  `AuditNodeJob` against a real field host first — validates WAN reachability, timeouts,
  per-site secrets — then one approval-gated `RedeployVmJob`, including the image-delivery
  path to a field node (download-url from the repo vs slow upload-API fallback)
  `[decision-needed — image-repo reachability model]`.
- Approval gating on field-targeted jobs (`approval_required` on 2.4; re-expressed as
  Approval Workflows once the 3.x upgrade lands — ideally before this phase); lab runs
  ungated.
- Bounded **Design Builder spike**: re-express the Phase 1 layout as a design; adopt only
  if the update/decommission lifecycle earns its YAML-DSL cost (active — v3.1.0 Apr
  2026 — but small community; keep the plain-ORM exit path).
- PDM 1.1.x enrollment for fleet visibility.

Exit: a complete new-office pair built in lab from three inputs; audit green; field pilot
passed; first production site cut over (new builds/rebuilds go Proxmox; in-service ESXi
never converted in place; pairs stay homogeneous).

### Phase 5 — Later / on-demand (explicitly not now)

Proxmox→Nautobot SSoT drift sync (model on the official vSphere DiffSync adapter; the two
community `nautobot-ssot-proxmox` repos are reference reading only). Per-VM `affinity`
pinning, hugepages + memory locking, and emulator-thread isolation — the escalation
ladder, invoked only if the Phase 2 jitter/latency soak (or field experience) demands
it (hugepages and affinity are root@pam-only; the `HostBaselineJob` path applies them).
Golden Config
for VNF configs (forces the dual-record modeling decision). Packaged Nautobot App (when
custom models or pinned pip dependencies appear — Git-synced jobs can't declare
dependencies). Packer-built Ubuntu golden templates in CI. PXE delivery exercised in the lab as the decided secondary path (same prepared artifact via `prepare-iso --pxe`; decision #41). SoT converge engine (`ConvergeVmJob`: intent-vs-actual diff/apply with hot/restart/redeploy change classes) + JobHook-triggered drift reports on watched-field edits — the SoT-as-control-plane trajectory (decision #40).

---

## 5. Proposed Repo Layout

Merges the colleague's structure with the phase deliverables (his `jobs/xcc_deploy`
content survives, refactored into `jobs/baremetal/` + `jobs/lib/bmc_client/`):

```
nautobot-proxmox/
├── README.md
├── jobs/                          # Nautobot Git-repository job root
│   ├── __init__.py                # register_jobs() for everything below
│   ├── design/
│   │   └── site_nfv_design.py     # SiteNfvDesignJob (P1)
│   ├── baremetal/
│   │   ├── deploy_proxmox_node.py # DeployNodeJob — L0 trigger (colleague's, refactored)
│   │   └── apply_bios_policy.py   # ApplyBiosPolicyJob (P3)
│   ├── proxmox/
│   │   ├── host_baseline.py       # HostBaselineJob (L1, bounded SSH)
│   │   ├── deploy_host_network.py # DeployHostNetworkJob (L2, verify/converge)
│   │   ├── ingest_image.py        # IngestImageJob
│   │   ├── deploy_vm.py           # DeployVmJob (L3/L4) + iso_builder plugins
│   │   ├── redeploy_vm.py         # RedeployVmJob (normal Job; JobButton wrapper for lab)
│   │   ├── site_build.py          # SiteBuildJob wrapper (P4)
│   │   └── audit_node.py          # AuditNodeJob (read-only)
│   └── lib/
│       ├── bmc_client/            # dual-mode Redfish (XCC1 PATCH-EXT / XCC2 InsertMedia)
│       ├── proxmox_client/        # proxmoxer wrapper, task-UPID polling, role docs
│       ├── layout/                # pure-function site layout computation
│       └── platform_profiles/     # SE350.yaml, SE455V3.yaml
├── bmc/                           # BIOS/RAID policy as data
│   ├── se350_bios.yaml
│   └── se455v3_bios.yaml
├── proxmox-install/
│   ├── answer.toml.j2             # kebab-case keys; ext4+LVM-thin on HW-RAID volume; webhook + first-boot
│   ├── answer_service/            # FastAPI: DMI POST → Nautobot lookup → rendered TOML
│   │                              #   + install webhook receiver + credential-bootstrap sink
│   ├── firstboot/
│   │   ├── firstboot.sh           # fetch-and-exec stub
│   │   └── payload/               # cmdline, ethtool oneshot, nic pinning, final bond/bridge,
│   │                              #   apt config, pveum bootstrap, phone-home
│   └── iso/                       # prepare-iso scripts, pinned PVE point release
├── proxmox-config/
│   ├── network_profiles/          # bond0/vmbr0 templates keyed by config context
│   └── pdm/                       # enrollment notes (token: Sys.Audit at /)
├── vnf-profiles/                  # per-guest deploy profiles + day-0 templates
│   ├── palo_vm/                   # profile.yaml, init-cfg.txt.j2          [absorbs palo-vm/]
│   ├── c8000v/                    # profile.yaml (_serial qcow2), iosxe_config.txt.j2, sdwan
│   ├── c9800cl/                   # profile.yaml (3-vNIC), iosxe_config.txt.j2
│   └── ubuntu/                    # profile.yaml (native cloud-init)       [absorbs ubuntu/]
├── config-contexts/               # Git-synced Nautobot config contexts + schemas
├── pipelines/                     # orchestration docs; escape-hatch designs (docs only P1–4)
├── docs/                          # this plan, runbooks, decision records
└── tests/                         # layout unit tests; validate-answer CI; profile lint
```

---

## 6. Key Risks & Open Questions

**Bare-metal / SE350 platform**
1. Answered: XCC licenses are **Enterprise fleet-wide** — the Redfish vmedia bare-metal
   track is unblocked. Remaining `[lab-verify]`: EXT members visible and PATCH-insert
   works at the fleet's actual XCC firmware level.
2. Answered: fleet is Security Pack (ThinkShield activation already required today).
   ThinkShield claim is step 1 of the scenario-3 runbook; pre-ship procedure handles
   motion detection (scenario 1). `[lab-verify]` current motion/tamper settings on
   in-service units; whether ThinkShield/FoD keys transfer to spare units.
3. Answered (Aug 2026): the BIOS standard is provided — the legacy XCC BIOS tool's
   settings are carried verbatim in `bmc/se350_bios.yaml` (Custom Mode + explicit
   knobs; the preset-locking caveat was already solved by the tool's own pattern).
   `[lab-verify]` remaining: exact Redfish attribute spellings/enums via the discovery
   job dump (incl. the Thermal Mode attribute name), and sign-off on the
   `proposed_additions` (console redirect, serial sharing off, Secure Boot off).
4. Answered (Aug 2026): boot storage is the hardware-RAID single volume →
   ext4 + LVM-thin, no ZFS. `[lab-verify]` the volume's Linux model/serial enumeration
   (answer.toml disk filter) and degraded-mirror alerting visibility (checklist §4).
5. `[lab-verify]` DMI serial as POSTed by the installer matches Nautobot serials;
   auto-install boot under Secure Boot (default: disable).
6. `[lab-verify]` X722 `disable-fw-lldp` at fleet NIC firmware; OOB NIC-firmware updates
   via XCC (OneCLI has no Debian support); RJ45 serial pinout vs OpenGear; standardize
   115200 8N1 (physical port default is 9600).
7. `[decision-needed]` Run Lenovo's EOS lookup for 7Z46/7D1X/7D27; pin a known-good
   firmware bundle and stop chasing updates; spares strategy (CPU/NIC soldered → whole-
   unit spares; same-CPU-SKU rule for PA-VM license survival).

**Proxmox / networking**
8. `[lab-verify]` `trunks=` behavior to PA-VM and C8000v (native VLAN, LLDP/CDP) —
   packet-level test vs multiple access vNICs.
9. `[lab-verify]` Whether custom post-up lines survive network-API edits on PVE 9 —
   decides systemd-oneshot (recommended) vs interfaces-stanza for ethtool persistence.
10. `[lab-verify]` `affinity` settable by non-root token (hugepages confirmed root-only);
    node-level `startall-onboot-delay` in PVE 9 (LACP convergence before VNF boot).
11. Decided (Aug 2026): **nautobot-composer will be reachable from field nodes** —
    scenario-2 field redeploys use `download-url` pulls directly; no upload-API
    fallback needed. (XCC1 vmedia separately forces plain HTTP for the install ISO.)
12. Discovery mode resolved with delivery decision #41 (2026-08-09): baked `--url` +
    cert fingerprint for the vmedia/field path (no site-DHCP dependency); DHCP option
    250 for the PXE/lab path. The service runs alongside nautobot-composer. Remaining
    `[decision-needed]`: who owns the answer service in production.
13. `[lab-verify]` Xeon D-2100 + PVE 9 (kernel 6.14+) burn-in before fleet rollout.

**VNF guests**
14. `[lab-verify]` q35 machine-version matrix for chosen PAN-OS on PVE 9.x; re-test on
    PVE upgrades.
15. Decided (Aug 2026): **PAN-OS 11.2.x** (the environment's current supported
    version); KVM support posture **accepted** (Proxmox is standard KVM/QEMU —
    functionally supported, formally unqualified by PA). **Standalone firewalls now,
    SCM in the near future** — the builder ships both templates from day one; per-VM
    attribute selects. Remaining `[decision-needed]`: fixed model vs flex credits.
16. `[lab-verify]` Cisco day-0 CD-ROM: ISO volume-label requirements on KVM; official
    support of iosxe_config.txt-via-CD on 9800-CL.
17. Ratified (Aug 2026): C8000v as proposed — `_serial` qcow2 variant, iosxe_config
    day-0, throughput level set day-0. Process items remain: UUID allocation flow
    (Smart Account → Manager WAN-edge list), Manager-version image gate, HSECK9 SLAC
    for >250 Mbps crypto sites.
18. Decided (Aug 2026): Ubuntu jump host is **Desktop** → golden-template bake path
    (no cloud image exists for Desktop): build the template once (bake in remote-access
    tooling and **autolock off**), clone + native cloud-init per deploy.
19. Decided (Aug 2026): 9800-CL sized for **Catalyst 9136 APs** → IOS-XE 17.18.x train
    (9136 support requires ≥17.7; 17.18 is the current extended-maintenance train).
    `[lab-verify]` final release pick + template size from that release's install guide.

**Nautobot**
20. Decided: build on the existing 2.4 LTM. Plan the 3.x upgrade as its own effort
    (target: before Phase 4). Prerequisites when the time comes: upgrade to ≥2.4.15, run
    `check_job_approval_status`, re-express approval gating as Approval Workflows, move
    app pins to 3.x trains. Risk while on 2.4: Django 4.2 passed upstream EOL Apr 2026
    (security patches case-by-case); the LTM window closes when Nautobot 3.3 ships.
21. `[decision-needed]` Golden Config for VNF configs? If yes, the dual-record Device+VM
    pattern must enter the initial data model.
22. `[lab-verify]` Nautobot 3.1: any change to VM→host pinning; whether cluster-host
    Devices must share the Cluster's Location (affects lab-staged builds — the
    staging-vs-destination Location decision in Phase 1).
23. `[decision-needed]` Design Builder adoption after the Phase 4 spike.
24. `[decision-needed]` Secrets backend of record: env-var Secrets on the worker now vs
    pointing nautobot-app-secrets-providers at existing Vault.
25. Decided (Aug 2026): **no approval gating at first** — jobs run ungated everywhere;
    revisit at the Phase 4 field pilot (the twin-health pre-flight and dry-run preview
    remain the guardrails). Simplifies the 2.4→3.x approval-migration surface to zero
    for now.

**Process / cutover**
26. `[decision-needed]` Cutover rule: pairs stay homogeneous, so a failure-driven
    migration converts both nodes of a pair — accept and cost that, or rebuild failed
    ESXi nodes as ESXi until a site's planned migration.
27. `[decision-needed]` Field bare-metal reinstall over WAN: recommend **never** (ship a
    lab-rebuilt unit); reversing this materially changes the answer-service/webhook
    connectivity and TLS design.
28. Partially answered: the robot's documented host tweaks are power policy, VM
    autostart, SSH/serial shells, plus "Enable TCPIP LRO" and "Enable Network Queue
    Pairing" — the latter two assert ESXi *defaults* and need no Proxmox port (see
    site-reference-architecture.md). `[lab-verify]` only that no additional undocumented
    ethtool/ring tweaks exist in the robot code.

**Site architecture (added Aug 2026)**
29. **Decided (Aug 2026):** server-facing EtherChannels flip `channel-group mode on` →
    `mode active` (LACP) in the same maintenance window as each server's Proxmox
    conversion — both mismatch directions black-hole (802.3ad into mode-on = partial
    hash-dependent loss; balance-xor into mode-active = members suspended). Post-flip
    verification: `show etherchannel summary` flags `P`, `/proc/net/bonding/bond0`
    partner MAC non-zero. Capture per-site `show etherchannel load-balance` (9300
    default `src-mac` polarizes NFV traffic) and standardize an IP-based hash both
    sides (`src-dst-ip` ↔ Linux `layer2+3`).
30. Resolved (Aug 2026): port roles confirmed — mgmt = p3/p4 copper as an
    active/passive pair (no port-channel) into switch **access ports** → Proxmox
    `active-backup` bond under untagged `vmbr0`; data = f1/f2 10G fiber, channeled →
    `802.3ad` bond under VLAN-aware `vmbr1`. Consequence: the LACP flip (#29) touches
    only the data pair — mgmt switch config never changes at conversion. Jumbo decided
    (Aug 2026): the L2 fabric is always jumbo-capable (`system mtu` in the standard
    switch build; existing sites verified/raised proactively — confirm live-vs-reload
    on the fleet IOS-XE release first); Proxmox data path mirrors MTU 9000.
    `[lab-verify]` remaining: Linux NIC-name↔faceplate pinning map (PCI path), and
    per-site `show system mtu` capture during rollout.
31. **Empirically confirmed on PVE 9.2.2 (lab NUC, Aug 2026)**: `affinity` and
    `hugepages` refuse **every** API token — including a full-privilege token of
    root@pam — with `only root can set 'affinity' config`. Likewise `import-from`
    with a filesystem path: `Only root can pass arbitrary filesystem paths`. So:
    pinning (the escalation path, per the reservations-vs-pinning stance) applies
    only via root-context SSH (`HostBaselineJob`, `qm set <vmid> --affinity`), and
    the image pipeline's volume-ID mechanism is mandatory — both now facts, not
    research claims. The volume-ID import path itself was validated end-to-end via
    the privilege-separated token (`download-url` → `import-from=<vol-id>` → disk
    created on LVM-thin). The **positive half is also proven** (same NUC, over SSH):
    `qm set --affinity 0-1` succeeds as root, `/proc` shows the QEMU main process
    and every thread pinned to `0-1` at runtime, and the pinning persists across
    stop/start. Host confinement demonstrated live: `system.slice AllowedCPUs=2-3`
    confined pvedaemon while the VM in qemu.slice stayed untouched — exactly the
    baseline split. Bonus: socat ships installed on PVE 9 (console-exposure units
    need no extra package); ksmtuned runs by default (firstboot disables it as
    planned).
