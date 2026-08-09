#!/bin/bash
# Prepare the fleet-generic Proxmox auto-install artifact (decision #11/#41).
#
# Runs on a PVE 9.x host or Debian box with proxmox-auto-install-assistant
# available (apt install proxmox-auto-install-assistant — Proxmox repo).
# One prepared artifact serves the whole fleet: node identity travels in the
# installer's HTTP POST; the answer service renders per-node answer files.
#
# Usage:
#   prepare-install-iso.sh --iso proxmox-ve_9.2-1.iso --url https://svc:8800/answer \
#       [--fingerprint <sha256-of-answer-service-cert>] \
#       [--auth-token name:secret] [--pxe] [--out DIR]
#
#   --iso          Stock Proxmox VE installer ISO (download from proxmox.com,
#                  verify its published SHA256 first).
#   --url          The answer service's /answer endpoint as NODES reach it.
#                  Baked in (vmedia/field path needs no DHCP option 250).
#   --fingerprint  SHA256 fingerprint of the answer service's TLS cert —
#                  required for HTTPS with a self-signed cert.
#   --auth-token   'name:secret' — installer sends it as a bearer token on the
#                  answer POST (PVE 9.2+); pair with ANSWER_AUTH_TOKEN.
#   --pxe          Additionally emit the PXE/iPXE artifact set (PVE 9.2+,
#                  decision #41 secondary path): vmlinuz + initrd + boot.ipxe.
#   --out          Output directory (default ./prepared). Publish the results
#                  to the composer firmware server and register the prepared
#                  ISO as a SoftwareVersion/SoftwareImageFile (Staged->Active)
#                  exactly like a golden VM image.
set -euo pipefail

ISO="" URL="" FINGERPRINT="" AUTH_TOKEN="" PXE=0 OUT="./prepared"
while [ $# -gt 0 ]; do
  case "$1" in
    --iso) ISO="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --fingerprint) FINGERPRINT="$2"; shift 2;;
    --auth-token) AUTH_TOKEN="$2"; shift 2;;
    --pxe) PXE=1; shift;;
    --out) OUT="$2"; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
[ -n "$ISO" ] && [ -n "$URL" ] || { echo "usage: --iso <file> --url <answer-url> [...]" >&2; exit 2; }
[ -f "$ISO" ] || { echo "ISO not found: $ISO" >&2; exit 2; }
command -v proxmox-auto-install-assistant >/dev/null \
  || { echo "proxmox-auto-install-assistant not installed (apt install proxmox-auto-install-assistant)" >&2; exit 2; }

mkdir -p "$OUT"
ARGS=(--fetch-from http --url "$URL")
[ -n "$FINGERPRINT" ] && ARGS+=(--cert-fingerprint "$FINGERPRINT")
[ -n "$AUTH_TOKEN" ] && ARGS+=(--answer-auth-token "$AUTH_TOKEN")

echo "== preparing auto-install ISO (fetch-from http) =="
proxmox-auto-install-assistant prepare-iso "$ISO" "${ARGS[@]}" --output "$OUT/$(basename "${ISO%.iso}")-auto.iso"

if [ "$PXE" = 1 ]; then
  echo "== preparing PXE/iPXE artifact set =="
  mkdir -p "$OUT/pxe"
  proxmox-auto-install-assistant prepare-iso "$ISO" "${ARGS[@]}" --pxe --pxe-loader ipxe --output "$OUT/pxe"
fi

echo "== checksums =="
( cd "$OUT" && find . -maxdepth 2 -type f ! -name '*.sha256' -exec sh -c \
    'sha256sum "$1" > "$1.sha256"' _ {} \; && cat ./*.sha256 2>/dev/null || true )

cat <<EOF

Done. Next steps (docs/baremetal-install.md):
  1. Publish $OUT/* to the composer firmware server. The prepared ISO must be
     reachable over plain HTTP for XCC1 virtual media; PXE artifacts go on the
     lab boot server.
  2. Register the prepared ISO in Nautobot: SoftwareVersion (platform
     proxmox-ve, status Staged) + SoftwareImageFile (filename, SHA256,
     download_url). Promote Staged -> Active after a lab install validates it.
EOF
