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
- **Walking slowly is right.** Several tempting features (CPU pinning, hugepages, OVS,
  Proxmox SDN zones, Proxmox HA, drift-sync/SSoT) are all correctly deferrable — and some
  have hard blockers anyway (hugepages settings are root@pam-only; API tokens can't set
  them).
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
   at "bridge-per-VLAN mapping" — steer this). One LACP bond + one VLAN-aware bridge
   covers everything (§2).
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
| Fresh ESXi install (manual/kickstart) | Automated-installer ISO + `answer.toml` | Redfish vmedia mount + one-time CD boot; `proxmox-auto-install-assistant` |
| Host power policy = High Performance | Two layers: firmware `OperatingModes_ChooseOperatingMode = MaximumPerformance` (Redfish BIOS) + kernel `intel_idle.max_cstate=1 processor.max_cstate=1` (governor already defaults to `performance` with intel_pstate) | `bmc/` BIOS policy + firstboot hook |
| vSwitch + LAG (dot1q trunk; today `channel-group mode on` — static, a vSS limitation) | `bond0`: bond-mode `802.3ad`, xmit-hash `layer2+3` (pair with Cisco `src-dst-ip`; 9300 default is `src-mac` — change it), miimon 100. **Switch ports flip `mode on`→`mode active` in the same window** — both mismatch directions black-hole (see site-reference-architecture.md) | `POST /nodes/{n}/network` (staged) + `PUT /nodes/{n}/network` (apply, ifupdown2, live) |
| Port groups (one per VLAN) | ONE VLAN-aware bridge `vmbr0` (`bridge_vlan_aware=1`, `bridge_vids` = site VLAN list from IPAM; note VLAN 1 is excluded by default) | Same network API |
| VM vNIC on access port group | `net0: virtio,bridge=vmbr0,tag=<vid>[,queues=<vCPUs>]` | VM config API |
| VLAN 4095 / VGT trunk (PA-VM, C8000v self-tagging) | vNIC with no `tag` (full trunk) or `trunks=<vid;vid;…>` (filtered — preferred, generated from IPAM) | VM config API |
| Port-group "forged transmits / promiscuous" | Not needed — Linux bridge does no MAC anti-spoofing; keep `firewall=0` on VNF dataplane NICs so floating MACs (VRRP, PA HA) are never filtered | VM config |
| NIC tuning (offloads, rings) | `ethtool -K … gro/lro/tso off` on the bridged data path (standard NFV/PA KVM guidance; LRO is kernel-auto-disabled on bridged ports anyway) + X722 `disable-fw-lldp on`; persist via systemd oneshot unit (no API exists for this). Keep GRO on the mgmt path | firstboot hook |
| "Enable TCPIP LRO" robot step | Asserts an ESXi *default* (vmkernel-stack LRO for host-terminated TCP) — no Proxmox action needed; Linux GRO on the mgmt path is the default-on analog | — |
| "Enable Network Queue Pairing" robot step | Also an ESXi default (NetQueue RX/TX thread pairing); functional analog is virtio-net multiqueue `queues=<vCPUs>` on dataplane vNICs | VM config API |
| Golden VMDK deploy | qcow2 in `import`-typed storage; VM create with `import-from=<volume ID>` | `download-url` + VM create APIs |
| Guest customization (Ubuntu) | Native cloud-init (`ide2=<store>:cloudinit`, `--ciuser --sshkeys --ipconfig0`); avoid `cicustom` snippets (no upload API) | Pure API |
| VNF day-0 (PA bootstrap, IOS-XE config) | Generated CD-ROM ISO: PA = `/config/init-cfg.txt` (+ empty `/license /software /content`); C8000v & 9800-CL = `iosxe_config.txt` at ISO root; SD-WAN = unmodified Manager-generated `ciscosdwan_cloud_init.cfg` | Jinja2 → ISO build → storage `upload` API (content=iso) → attach `media=cdrom` |
| VM autostart + start delay | `onboot=1` + `startup=order=N,up=S` (pve-guests at boot; reverse order at shutdown) | VM config API |
| Host serial → OpenGear | Firmware COM1 console-redirect (Redfish BIOS, standardize 115200 8N1) + kernel `console=tty0 console=ttyS0,115200n8` + GRUB serial. Two persistence paths: `/etc/default/grub` + `update-grub` (GRUB installs) vs `/etc/kernel/cmdline` + `proxmox-boot-tool refresh` (UEFI/ZFS-root) — automation must handle both | `bmc/` policy + firstboot |
| VM console access | `serial0: socket` on every VNF (+ `vga=serial0` for serial-primary guests) → `qm terminal <vmid>` from the host shell reached via OpenGear (Ctrl-O detaches) | VM config API |
| vCenter (fleet view) | Proxmox Datacenter Manager 1.1.x — visibility/ops only | PDM enrollment token needs `Sys.Audit` at `/` (fixes the colleague's 403) |
| ESXi host firmware updates (OneCLI) | OneCLI does **not** support Debian — use out-of-band XCC web UI / Redfish UpdateService | `bmc/` track |

---

## 3. Architecture Spine

**Layers, each an idempotent job runnable standalone, chained by a thin wrapper:**

```
L0 bare-metal    XCC Redfish vmedia → auto-install ISO → answer.toml → firstboot hook
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

**Proxmox service-account role** (privilege-separated token):
`VM.Allocate, VM.Clone, VM.Config.*, VM.PowerMgmt, VM.Audit, VM.Console,
Datastore.AllocateSpace, Datastore.AllocateTemplate, Datastore.Audit, Sys.Audit,
Sys.Modify (required for the /nodes/{n}/network endpoints — note it widens blast radius),
SDN.Use on /sdn/zones/localnetwork`.

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
  256 GB minus host overhead and the pinned ZFS `arc-max`. Budget `queues=` too —
  multiqueue adds host CPU load.
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

### Phase 0 — Platform decisions + lab bring-up

Lock the one-way choices; get one SE350 pair + a Nautobot instance in the lab.

- Decided: build on the existing Nautobot 2.4 LTM (pin 2.4-LTM app trains; schedule the
  3.x upgrade as its own effort, ideally before Phase 4). Still to decide: standalone
  pair (recommend yes); VNF-as-VirtualMachine modeling (recommend yes, revisit only if
  Golden Config needed).
- SE350 platform verification checklist on a real unit: XCC Enterprise FoD present? `EXT`
  vmedia members visible? Dump `GET /redfish/v1/Systems/1/Bios` (capture exact
  `OperatingModes_*` / `DevicesandIOPorts_*` attribute spellings). Does the Marvell
  88SE9230 M.2 adapter expose disks as plain AHCI/JBOD (required for the ZFS-mirror
  design — if it only presents RAID volumes, the boot-storage design changes)?
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
  numa=1, machine type with **pinned version**, NIC list with bridge/tag/trunks/queues/
  pinned MAC, `smbios1 uuid=` from Nautobot, `serial0: socket`, onboot + startup order,
  firewall=0 on dataplane NICs, qemu-guest-agent where supported) and pluggable
  `iso_builder` types. Destroy-and-recreate semantics (day-0 ISOs are first-boot-only).
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
reachable, zero SSH. Onboarding criteria (PA→Panorama/SCM registration, AP joins) are
separate line items gated on the licensing/BOM decisions in §6.

### Phase 3 — Bare-metal track: port L0 to SE350 + close the loop (parallel with Phase 2)

- Refactor `xcc_client.py` to dual-mode vmedia: `EXT{N}` members present → XCC1 path
  (PATCH on member, select by `Id` prefix "EXT", HTTP-only ISO URL); else XCC2 path (POST
  InsertMedia). Fail explicitly on missing Enterprise FoD ("EXT members absent"). Fix
  `_select_cd_media()` (MediaTypes doesn't distinguish insertable members on XCC1).
- **Platform profiles** as data (per Nautobot DeviceType YAML in `bmc/`): vmedia method,
  BIOS attribute map, disk filter, NIC naming, license requirements, ThinkShield steps.
- `ApplyBiosPolicyJob`: generic Redfish `Bios/Settings` PATCH → reboot → readback-verify,
  driven by the YAML (SE350: MaximumPerformance, COM1 redirect, Secure Boot disabled
  pending shim test). Note: preset operating modes lock individual C-state knobs —
  combining MaximumPerformance with custom C-states requires CustomMode with every knob
  explicit `[lab-verify]`.
- ISO/answer decision resolved: **one generic prepared ISO + HTTP answer fetch**. Note the
  real coupling — with `--fetch-from http` the URL + cert fingerprint are baked at
  prepare time, so it's one ISO per (PVE release × answer-service endpoint); DHCP option
  250 or DNS TXT discovery would make it fully generic `[decision-needed — pick the
  discovery mode]`. CI runs `proxmox-auto-install-assistant validate-answer` on rendered
  answers. answer.toml: ZFS RAID1 by disk filter with explicit `zfs.arc-max` (4–8 GiB,
  subtracted from the VM RAM budget) — pending the Phase 0 M.2/AHCI answer.
- Firstboot hook (small fetch-and-exec stub): kernel cmdline (C-states, serial console —
  both GRUB and proxmox-boot-tool paths), ethtool/`disable-fw-lldp` systemd oneshot, NIC
  name pinning, **final bond0/vmbr0 + mgmt topology**, apt repo config (no-subscription)
  + point-release pin, lldpd, qemu-guest-agent-ready defaults, root SSH key, **pveum
  credential bootstrap**, phone-home webhook.
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
community `nautobot-ssot-proxmox` repos are reference reading only). `affinity` pinning /
hugepages — only on measured jitter (and note hugepages is root@pam-only). Golden Config
for VNF configs (forces the dual-record modeling decision). Packaged Nautobot App (when
custom models or pinned pip dependencies appear — Git-synced jobs can't declare
dependencies). Packer-built Ubuntu golden templates in CI. PXE as an ISO alternative.

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
│   ├── answer.toml.j2             # kebab-case keys; zfs.arc-max explicit; webhook + first-boot
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
3. `[lab-verify]` Exact SE350 Redfish BIOS attribute names/enums (dump on real hardware);
   MaximumPerformance vs CustomMode C-state interplay.
4. `[lab-verify]` Marvell 88SE9230 M.2: AHCI/JBOD exposure (gates ZFS RAID1 boot design).
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
11. `[decision-needed]` Image-repo reachability: lab-only vs field-reachable for
    scenario-2 redeploys vs upload-API fallback. (XCC1 vmedia separately forces plain
    HTTP for the install ISO.)
12. `[decision-needed]` Answer-service discovery mode: baked `--fetch-from` URL vs DHCP
    option 250 vs DNS TXT; where the service runs; who owns it.
13. `[lab-verify]` Xeon D-2100 + PVE 9 (kernel 6.14+) burn-in before fleet rollout.

**VNF guests**
14. `[lab-verify]` q35 machine-version matrix for chosen PAN-OS on PVE 9.x; re-test on
    PVE upgrades.
15. `[decision-needed]` PAN-OS 11.2.x vs 12.1; fixed model vs flex credits. Management
    model decided (standalone or SCM, no Panorama); remaining choice is which sites get
    which, and it only selects the bootstrap template. Proxmox absent from PA's
    qualified matrix — accept posture explicitly.
16. `[lab-verify]` Cisco day-0 CD-ROM: ISO volume-label requirements on KVM; official
    support of iosxe_config.txt-via-CD on 9800-CL.
17. `[decision-needed]` C8000v `_serial` qcow2 vs standard image + `platform console
    serial`; UUID allocation flow (Smart Account → Manager WAN-edge list) and
    Manager-version image gate; HSECK9 SLAC for >250 Mbps crypto sites.
18. `[decision-needed]` Ubuntu jump host: Desktop (template-bake only — no cloud image
    exists) vs Server + tooling on the standard cloud image.
19. `[lab-verify]` 9800-CL template sizing from the official guide of the chosen 17.18.x
    release; release choice gated by fleet AP models.

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
25. `[decision-needed]` Approval policy: gate field redeploys from day one (recommended)
    vs lab friction.

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
29. Recommended, `[decision-needed]` to ratify: server-facing EtherChannels flip
    `channel-group mode on` → `mode active` (LACP) in the same maintenance window as
    each server's Proxmox conversion — both mismatch directions black-hole (802.3ad
    into mode-on = partial hash-dependent loss; balance-xor into mode-active = members
    suspended). Post-flip verification: `show etherchannel summary` flags `P`,
    `/proc/net/bonding/bond0` partner MAC non-zero. Capture per-site
    `show etherchannel load-balance` (9300 default `src-mac` polarizes NFV traffic) and
    standardize an IP-based hash both sides (`src-dst-ip` ↔ Linux `layer2+3`).
30. `[lab-verify]` SE350 port-role map under Proxmox: today p1 = XCC, p3/p4 = 1G copper
    ("esx"), f1/f2 = 10G MMF to both stack members. Decide and encode in the network
    profile whether Proxmox uses a single 10G data bond with mgmt on copper, or mirrors
    some other role split — the plan's single-bond assumption needs this confirmed
    against real cabling before the firstboot network template is written.
