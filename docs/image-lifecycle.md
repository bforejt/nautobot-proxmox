# Image & Template Lifecycle

How golden images are built, versioned, promoted, deployed, and retired. Core
principle: **templates are build outputs, never pets.** The sources of truth
are (1) the vendor's pristine base image (URL + upstream checksum) and (2) the
build seed in this repo (reviewed via change control). Every template is a
derived, disposable artifact — if one were lost, rerunning the build
reproduces it. Vendor-sealed appliance images (PA-VM, C8000v) are the one
exception: no seed, no rebuild, and vendor portals delist old versions — for
those the published firmware-server copy IS the durable artifact, so
retention matters more there, not less.

The Staged/Active/Retired statuses this lifecycle uses on SoftwareVersion /
SoftwareImageFile are provisioned by the **`Bootstrap NFV Data Model`** job.

## Three tracks, three cadences, three blast radii

### Template build — runs rarely (new base release, seed change, CVE refresh)

**Today this is scripted**:
[vnf-profiles/ubuntu/build-template.sh](../vnf-profiles/ubuntu/build-template.sh)
runs the whole sequence below plus verification; a Nautobot job form of it is
planned (see the gap register in
[deployment-onboarding.md](deployment-onboarding.md)).

For image classes that need customization (today: the Ubuntu jump host). Runs
against a **designated build node** (a lab box you have root SSH to — build
SSH is a build-node-only privilege; field deploys use only the API token) —
never a field node.

1. **Pull the vendor base** via `download-url`: the Ubuntu Server *cloud image*
   (qcow2 — same maintainability property as "ISO from the vendor" but with no
   installer to drive), verified against the vendor's published SHA256. Both URL
   and upstream checksum are recorded on the version record — provenance starts
   at the vendor.
2. **Create the build VM** from it (`import-from`), attach the build seed
   ([vnf-profiles/ubuntu/template-build.user-data.yaml](../vnf-profiles/ubuntu/template-build.user-data.yaml))
   as a cloud-init user-data snippet (`--cicustom`) with a fresh, ephemeral
   build SSH key substituted for `__BUILD_SSH_KEY__`. Snippets require node
   shell access — fine on the build node, and exactly why field deploys never
   use them (they stay on Proxmox's native cloud-init keys, API-settable).
3. **Boot once, unattended.** cloud-init installs the desktop, tooling, and
   settings. Completion is signaled (guest agent / cloud-init phone-home), not
   watched by a human.
4. **Seal**: `cloud-init clean`, truncate `machine-id`, drop the build-only SSH
   key the seed injected, detach the seed ISO, power off.
5. **Publish**: extract the disk as qcow2 and push a complete, immutable
   **version set** to the firmware server (with the nautobot-composer
   `firmware` profile, files go at the share's **root** — nginx serves that
   root at `/images/<file>`):
   - `ubuntu-jumphost-24.04-v2.qcow2` — the template artifact
   - `ubuntu-jumphost-24.04-v2.qcow2.sha256` — its checksum sidecar
   - `ubuntu-jumphost-24.04-v2.user-data.yaml` — the exact seed used, copied
     verbatim from the synced repo at build time
   - `ubuntu-jumphost-24.04-v2.manifest.json` — build metadata: vendor base URL
     + upstream checksum, seed git commit, build date/node, and the guest's
     full package list (`dpkg -l`, captured just before sealing — answers
     "which template versions ship package X?" without booting anything)

   The composer copies are **frozen build output** — never edited there. The
   editable source of the seed stays in git; a change produces a new version
   set. Two copies, two roles: git = mutable, reviewed source; composer =
   immutable per-version record. Every published version therefore carries its
   own complete recipe, independent of any git host.
6. **Register**: create the `SoftwareVersion` (status **Staged**) +
   `SoftwareImageFile` (composer URL, checksum, size) in Nautobot, stamped with
   the seed's git commit — full provenance chain: running clone → version record
   → seed commit → vendor base. The **`Register Image from Published Set`**
   job does this from the artifact URL (template manifests don't carry
   platform/version, so supply those two inputs; checksum and size come from
   the published set).

### Vendor-sealed appliance images — runs per vendor release (register-only)

Vendor-qcow2 image classes (PAN-OS, C8000v, 9800-CL) have **no build, no
seal, no seed**: the vendor image must be deployed exactly as shipped. For
PA-VM this is a hard vendor rule — each firewall must run its own independent
copy of the image, and cloning a *booted* instance invalidates its license
(the serial number derives from VM UUID + CPU ID). So the pipeline reduces to
**acquire → verify → publish → register**, scripted by
[vnf-profiles/paloalto/register-vendor-image.sh](../vnf-profiles/paloalto/register-vendor-image.sh)
(PA defaults; vendor identity env-overridable for the other classes — runs on
your workstation, no node SSH):

1. **Acquire** manually from the vendor's entitlement-gated portal (the one
   step that can't be automated), and capture the portal's published SHA256.
2. **Verify + emit the version set** — the script checks the qcow2 magic,
   verifies against the portal checksum when provided (`VENDOR_SHA256=…`,
   recorded in the manifest as `vendor-portal-verified` vs `computed-local`),
   and writes the set next to the image. Three files, not four — there is no
   seed: the **untouched** vendor qcow2 (never re-converted; the shipped
   bytes ARE the artifact), the `.sha256` sidecar, and a `manifest.json`
   with vendor-shaped provenance (vendor, product, platform, version label,
   source, checksums, sizes) in place of the build-shaped one.
3. **Publish** the three files to the firmware server image root (the script
   scp's them when given a target) and **register** with the
   **`Register Image from Published Set`** job — give it the artifact URL
   and it reads checksum, size, platform, and version from the published set
   itself (vendor-track manifests carry all four), verifies the artifact is
   actually served (optional full re-hash), and creates the Staged
   SoftwareVersion + SoftwareImageFile. Manual entry from the script's
   printed recipe remains the fallback.

Lifecycle differences from templates: **refresh = register a new vendor
version** (there is nothing to rebuild — the CVE story is the vendor's
release cadence), and the published artifact is never booted directly — a
checksum-verified copy is pulled per node (**`Ingest Image onto Proxmox
Node`** pre-warms; **`Deploy VNF Device`** pulls at deploy). PAN-OS's
`pa-bootstrap` day-0 builder shipped 2026-08-27 (lab validation pending —
decision #46); IOS-XE remains Phase 2c and registers-but-cannot-deploy until
its builder ships. Both tracks end in the same place: a **Staged**
SoftwareVersion, promoted through the same Staged → Active gate below.

### `Deploy VNF Device (SoT-driven)` — runs constantly (every site build, every field redeploy)

Consumes only Nautobot intent, never touches vendor sources or seeds:

1. The published artifact is pulled to the target node (`download-url` +
   checksum from the `SoftwareImageFile`) — the deploy job does this itself,
   idempotently; **`Ingest Image onto Proxmox Node`** does the same thing
   standalone to warm nodes ahead of a window.
2. VM created from it (`import-from` volume ID) with config generated from the
   Device record + its Interfaces/VLANs + platform facts and Platform CFs.
3. Per-VM day-0 is **identity only**. Cloud-init platforms: hostname from the
   device, IP/gateway from Nautobot IPAM (or DHCP), console user from the
   Platform's `console_user` CF with the password from the
   `jumphost_console_password` Secret. pa-bootstrap platforms get a per-device
   bootstrap ISO instead (contract §4a) — no cloud-init at all. Seconds per
   clone, no package installs, no internet dependency at deploy time — the
   gigabytes are already in the template.

Deploy-time customization stays on Proxmox's *native* cloud-init keys (no
snippet uploads, which the API can't do). Anything richer — like default users
belonging to the `wireshark` group — is baked at build time via
`/etc/cloud/cloud.cfg.d/` defaults in the seed, so deploys never need custom
user-data files.

## The version lifecycle

```
seed change merged ─▶ template build ─▶ SoftwareVersion: Staged
                                              │  lab: flip to Active,
                                              │  validate one deploy
                                              ▼
                                        human flips ──▶ Active   ◀── rollback is
                                              │                      flipping back
                                              ▼
                            design/deploy jobs select Active only
                                              │
                                              ▼
                              superseded ──▶ Retired (artifact retained
                                             on composer per retention)
```

- **A change is a PR**, not an edit to a golden VM: add a tool → one line in the
  seed → merge → rebuild → new Staged version. Nobody ever "logs into the
  template to fix something."
- **Fleet rollout is a SoT operation**: flipping v2 to Active changes what every
  subsequent deploy uses; existing VMs are upgraded by redeploy (VMs are cattle
  too — jump hosts hold no state worth preserving).
- **Rollback** is flipping the old version back to Active — its artifact never
  left composer.
- **CVE refresh** is a rebuild with an unchanged seed (package_upgrade pulls
  current packages) — new version number, same recipe.

## Why not the installer-ISO path

The Ubuntu Desktop installer ISO + autoinstall would also automate, but it adds
an installer to drive, an answer file dialect to maintain, and a slower build —
for the identical end state. The cloud image *is* the vendor's supported
"automation base." The ISO path stays documented as the fallback if a future
image class can't be reached from a cloud image.
