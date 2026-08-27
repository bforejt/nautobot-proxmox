#!/usr/bin/env bash
# Publish + register a VENDOR-SEALED appliance image (PA-VM qcow2) — the
# register-only track from docs/image-lifecycle.md.
#
# Unlike the Ubuntu template build there is NO build, NO seal, NO seed: Palo
# Alto requires the vendor qcow2 be deployed as-is, one independent copy per
# firewall (cloning a booted instance invalidates its license — serial number
# is derived from VM UUID + CPU ID). So this script only:
#   1. verifies the local file (qcow2 magic; against the vendor-portal SHA256
#      when you provide one — the portal download page shows it, login-gated);
#   2. emits the immutable version set NEXT TO the image: the untouched qcow2
#      + .sha256 sidecar + manifest.json (vendor provenance — no seed file);
#   3. optionally copies the set to your firmware server's image root;
#   4. prints the exact Nautobot registration recipe (SoftwareVersion Staged +
#      SoftwareImageFile), same operator flow as build-template.sh.
#
# Usage:   ./register-vendor-image.sh <path-to-qcow2> <version-label> [scp-target]
# Example: VENDOR_SHA256=abc123... \
#          ./register-vendor-image.sh ~/PA-VM-KVM-11.2.8.qcow2 11.2.8 \
#          user@firmware:/srv/firmware/images
#
# Defaults are Palo Alto VM-Series; the vendor identity is env-overridable so
# the other vendor-qcow2 classes (C8000v, 9800-CL) can reuse this script:
#   VENDOR, PRODUCT, PLATFORM, SOURCE_NOTE, VENDOR_SHA256
#
# The registration half is job-driven ("Register Image from Published Set",
# jobs/design/register_image.py); this script is the verify/publish half —
# the parts that need the operator's workstation and portal entitlement.
set -euo pipefail

IMG="${1:?usage: register-vendor-image.sh <path-to-qcow2> <version-label> [scp-target]}"
VER="${2:?usage: register-vendor-image.sh <path-to-qcow2> <version-label> [scp-target]}"
DEST="${3:-}"

VENDOR="${VENDOR:-Palo Alto Networks}"
PRODUCT="${PRODUCT:-VM-Series}"
PLATFORM="${PLATFORM:-paloalto-panos}"
SOURCE_NOTE="${SOURCE_NOTE:-Palo Alto Networks support portal (entitlement-gated download)}"
VENDOR_SHA256="${VENDOR_SHA256:-}"

log(){ echo "[$(date +%H:%M:%S)] $*"; }

command -v python3 >/dev/null 2>&1 || { log "FAIL: python3 is required (manifest generation)"; exit 1; }
if [ -n "$VENDOR_SHA256" ]; then
  # Normalize realistic paste forms: lowercase, and tolerate the portal's
  # "hash  filename" line or stray whitespace by keeping the first field.
  VENDOR_SHA256="$(printf '%s' "$VENDOR_SHA256" | awk '{print tolower($1)}')"
  case "$VENDOR_SHA256" in
    *[!0-9a-f]*) log "FAIL: VENDOR_SHA256 does not look like a sha256 (need 64 hex chars)"; exit 1;;
  esac
  [ "${#VENDOR_SHA256}" -eq 64 ] || { log "FAIL: VENDOR_SHA256 does not look like a sha256 (need 64 hex chars)"; exit 1; }
fi

[ -f "$IMG" ] || { log "FAIL: no such file: $IMG"; exit 1; }
FILE="$(basename "$IMG")"
DIR="$(cd "$(dirname "$IMG")" && pwd)"
BASE="${FILE%.qcow2}"
[ "$BASE" != "$FILE" ] || { log "FAIL: expected a .qcow2 file, got: $FILE"; exit 1; }
case "$FILE" in *"$VER"*) : ;; *) log "WARN: filename does not contain version label '$VER' — double-check both";; esac

log "=== verify vendor image ==="
# qcow2 magic: QFI\xfb
MAGIC="$(head -c 3 "$IMG")"
[ "$MAGIC" = "QFI" ] || { log "FAIL: $FILE is not a qcow2 image (bad magic)"; exit 1; }

if command -v shasum >/dev/null 2>&1; then
  SHA="$(shasum -a 256 "$IMG" | awk '{print $1}')"
else
  SHA="$(sha256sum "$IMG" | awk '{print $1}')"
fi
SIZE="$(wc -c < "$IMG" | tr -d ' ')"
log "sha256 $SHA ($SIZE bytes)"

if [ -n "$VENDOR_SHA256" ]; then
  if [ "$SHA" = "$VENDOR_SHA256" ]; then
    PROVENANCE="vendor-portal-verified"
    log "matches the vendor-published checksum"
  else
    log "FAIL: sha256 mismatch against VENDOR_SHA256 — corrupt or wrong download"
    log "  computed: $SHA"
    log "  vendor:   $VENDOR_SHA256"
    exit 1
  fi
else
  PROVENANCE="computed-local"
  log "WARN: no VENDOR_SHA256 provided — checksum is computed locally, not"
  log "      verified against the vendor portal. Capture the portal value and"
  log "      re-run for full provenance if you can."
fi

VIRTUAL_SIZE=""
if command -v qemu-img >/dev/null 2>&1; then
  VIRTUAL_SIZE="$(qemu-img info --output=json "$IMG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["virtual-size"])' 2>/dev/null || true)"
fi

log "=== emit version set (next to the image) ==="
# Same sidecar format the Ubuntu track publishes (sha256sum output format).
printf '%s  %s\n' "$SHA" "$FILE" > "$DIR/${FILE}.sha256"
python3 - "$FILE" "$VER" "$VENDOR" "$PRODUCT" "$PLATFORM" "$SOURCE_NOTE" \
  "$SHA" "$PROVENANCE" "$VENDOR_SHA256" "$SIZE" "$VIRTUAL_SIZE" \
  > "$DIR/${BASE}.manifest.json" <<'PYEOF'
import json, sys, datetime
(file, ver, vendor, product, platform, source,
 sha, provenance, vendor_sha, size, vsize) = sys.argv[1:12]
print(json.dumps({
    "name": file,
    "version_label": ver,
    "vendor": vendor,
    "product": product,
    "platform": platform,
    "image_class": "vendor-sealed",
    "published": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source": source,
    "sha256": sha,
    "checksum_provenance": provenance,
    "vendor_sha256": vendor_sha or None,
    "size_bytes": int(size),
    "virtual_size_bytes": int(vsize) if vsize else None,
    "note": ("vendor bytes untouched - no build, no seal, no seed. Each "
             "deployed instance must run its own independent copy, pulled "
             "per node checksum-verified - never boot this published "
             "artifact."),
}, indent=1))
PYEOF
log "wrote ${FILE}.sha256 + ${BASE}.manifest.json"

if [ -n "$DEST" ]; then
  log "=== copy version set to firmware server ==="
  scp -o StrictHostKeyChecking=accept-new \
    "$IMG" "$DIR/${FILE}.sha256" "$DIR/${BASE}.manifest.json" "$DEST/"
  PUBLISHED_AT="$DEST"
else
  PUBLISHED_AT="$DIR (local only — copy the three files to your firmware server's image root)"
fi

log "=== REGISTRATION READY ==="
cat <<EOF

Version set ($PUBLISHED_AT):
  ${FILE}   (sha256 $SHA, $SIZE bytes)
  ${FILE}.sha256
  ${BASE}.manifest.json

Next (docs/image-lifecycle.md — vendor-sealed track):
  1. Ensure all three files are at the firmware server's image root
     (served at https://<firmware-server>/images/<file>).
  2. Register in Nautobot: run the "Register Image from Published Set" job
     with Artifact URL = https://<firmware-server>/images/${FILE} — it reads
     checksum/size/platform/version from the set you just published.
     (Manual fallback, on platform "${PLATFORM}":
       SoftwareVersion: version "${VER}", status STAGED
       SoftwareImageFile: image_file_name=${FILE},
         image_file_checksum=$SHA,
         hashing_algorithm=sha256, image_file_size=${SIZE},
         download_url=https://<firmware-server>/images/${FILE},
         default_image=true)
  3. Promote Staged -> Active in the lab and validate one deploy — the
     deploy job refuses non-Active versions (that IS the gate).

Reminders for this image class:
  - Refresh = register a NEW vendor version; never rebuild or edit this one.
  - Never boot the published artifact itself; each instance gets its own
    checksum-verified copy pulled per node (Ingest works today; Deploy
    requires the platform's day-0 builder to have shipped).
EOF
