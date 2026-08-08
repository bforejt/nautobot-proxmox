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
- **PROPOSED — deploy trigger via native Status**: a VNF Device in status
  **Planned** is intent-not-yet-deployed; the deploy job builds it and flips it
  to **Active**. Decommissioned/parked states use the existing status set.
  Native field, no new custom anything, and the Jobs UI/API can filter on it.

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
- **PROPOSED — NIC ordering contract**: Proxmox `netN` index follows the
  platform profile's declared interface-name order (e.g. jump host: `eth0`;
  PA: `mgmt`, then `ethernet1/1`, `ethernet1/2`, …). The layout names
  interfaces exactly per that convention; the profile is the order authority.
  (Guest NIC enumeration must match — this is the one place naming is
  load-bearing.)
- **MAC addresses**: **PROPOSED** — layout pins `mac_address` on each
  interface; deploy passes it through (deterministic guest NIC identity,
  PA licensing stability).
- **Primary/mgmt IP**: `device.primary_ip4` — settled Nautobot convention.
- **Gateway (needed for static cloud-init/day-0)**: **QUESTION for the team** —
  what marks a gateway today in your data? The role list suggests an existing
  convention (`VIP`/`HSRP`/`VRRP`/`Anycast` roles exist). Recommendation if none
  is established for these sites: the PA SVI address in each prefix carries an
  IPAddress **Role = "Gateway"**; consumers resolve gateway =
  the role-tagged IP within the interface's prefix. Native objects only.
- **DNS/NTP and similar site services**: config context. *(Settled pattern.)*

## 4. The hypervisor record

| Need | Source | Notes |
|---|---|---|
| Node name (Proxmox) | `device.name` | Settled |
| API endpoint | `device.primary_ip4` | **PROPOSED** |
| API credentials | Secrets `proxmox_token_id`/`proxmox_token_secret` (global pair now; per-device SecretsGroup when field rollout warrants) | Settled for now |
| BMC/XCC address | **QUESTION for the team** — how are BMC IPs modeled today? Recommendation: a dedicated interface (e.g. `xcc`) on the SE350 device with its IP assigned — native, visible, cable-truthful | |
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
