# Image & Template Lifecycle

How golden images are built, versioned, promoted, deployed, and retired. Core
principle: **templates are build outputs, never pets.** The sources of truth are
(1) the vendor's pristine base image (URL + upstream checksum), (2) the build seed
in this repo (reviewed via PR), and (3) the platform profile (Git-synced config
context). Every template is a derived, disposable artifact — if one were lost,
rerunning the build job reproduces it.

## Two jobs, two cadences, two blast radii

### `BuildTemplateJob` — runs rarely (new base release, seed change, CVE refresh)

For image classes that need customization (today: the Ubuntu jump host). Runs
against a **designated build node** (the lab NUC now, a lab SE350 later) — never
a field node.

1. **Pull the vendor base** via `download-url`: the Ubuntu Server *cloud image*
   (qcow2 — same maintainability property as "ISO from the vendor" but with no
   installer to drive), verified against the vendor's published SHA256. Both URL
   and upstream checksum are recorded on the version record — provenance starts
   at the vendor.
2. **Create the build VM** from it (`import-from`), attach the build seed
   ([vnf-profiles/ubuntu/template-build.user-data.yaml](../vnf-profiles/ubuntu/template-build.user-data.yaml))
   as a generated NoCloud ISO — the same `iso_builder` mechanism the VNF day-0
   engine uses, so the build job exercises the deploy engine's own machinery.
3. **Boot once, unattended.** cloud-init installs the desktop, tooling, and
   settings. Completion is signaled (guest agent / cloud-init phone-home), not
   watched by a human.
4. **Seal**: `cloud-init clean`, truncate `machine-id`, drop the build-only SSH
   key the seed injected, detach the seed ISO, power off.
5. **Publish**: extract the disk as qcow2 and push a complete, immutable
   **version set** to nautobot-composer:
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
   → seed commit → vendor base.

Vendor-qcow2 image classes (PAN-OS, C8000v, 9800-CL) skip steps 2–4: a lighter
`RegisterVendorImageJob` ingests the manually-downloaded, entitlement-gated file
onto composer and registers it. Both paths end in the same place: a **Staged**
SoftwareVersion.

### `DeployVmJob` — runs constantly (every site build, every field redeploy)

Consumes only Nautobot intent, never touches vendor sources or seeds:

1. `IngestImageJob` pulls the published artifact to the target node
   (`download-url` from composer, checksum from the `SoftwareImageFile`).
2. VM created from it (`import-from` volume ID) with config generated from the
   VM object + VMInterface/VLANs + platform profile.
3. Per-VM cloud-init is **identity only**: hostname, IP/gateway, users/keys from
   Nautobot. Seconds per clone, no package installs, no internet dependency at
   deploy time — the gigabytes are already in the template.

Deploy-time customization stays on Proxmox's *native* cloud-init keys (no
snippet uploads, which the API can't do). Anything richer — like default users
belonging to the `wireshark` group — is baked at build time via
`/etc/cloud/cloud.cfg.d/` defaults in the seed, so deploys never need custom
user-data files.

## The version lifecycle

```
seed PR merged ──▶ BuildTemplateJob ──▶ SoftwareVersion: Staged
                                              │  validate: deploy one from
                                              │  Staged in the lab, check it
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
