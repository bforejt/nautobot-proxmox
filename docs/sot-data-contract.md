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
| Image to deploy | `device.software_version` → its default `SoftwareImageFile` | **PROPOSED**: layout sets `software_version` explicitly; deploy refuses if unset or if the version's status ≠ Active (keeps the Staged→Active promotion gate authoritative) |
| Sizing (vcpus / memory / disk) | Custom-field overrides on the device (`vcpus`, `memory_mb`, `disk_gb`) **when set**; otherwise the role/platform standard from the config-context profile | **PROPOSED** precedence: device CF > profile standard. Standards stay in Git-synced config context (still SoT) so sizing changes are one edit, not N device edits |
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
- **MAC addresses**: **PROPOSED** — layout pins `mac_address` on each
  interface; deploy passes it through (deterministic guest NIC identity,
  PA licensing stability).
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

## 4. The hypervisor record

| Need | Source | Notes |
|---|---|---|
| Node name (Proxmox) | `device.name` | Settled |
| API endpoint | `device.primary_ip4` | **PROPOSED** |
| API credentials | Secrets `proxmox_token_id`/`proxmox_token_secret` (global pair now; per-device SecretsGroup when field rollout warrants) | Settled for now |
| BMC/XCC address | **Settled (2026-08-08)**: a dedicated interface named `xcc` on the SE350 device with its IP assigned — native, visible, cable-truthful | Layout process creates it |
| Storage names, bridge names | Platform profile / config context (`local-lvm`, `local`, `vmbr0`/`vmbr1` as site standards) | Settled pattern |

## 5. Normalization guardrails (the standing rule, operationalized)

- Job inputs are **object references** (Device, SoftwareVersion), never
  free-form strings, wherever the object exists in Nautobot.
- Prefer **native fields** (Status, Role, primary_ip, interface VLANs) over
  custom fields; custom fields only where no native slot exists (`vmid`).
- Standards (sizing, port maps, storage/bridge names) live in **Git-synced
  config contexts** — one definition, many consumers, still inside the SoT.
- Anything a consumer reads that is not in this document is a bug in this
  document.
