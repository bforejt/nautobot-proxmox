#!/usr/bin/env bash
# Build the Ubuntu jump-host golden template on a designated BUILD node.
#
# This is the scripted form of the template build described in
# docs/image-lifecycle.md (a Nautobot "build template" job is planned; this
# script is the working process today). It:
#   1. generates an EPHEMERAL build SSH keypair and injects the public key
#      into the seed (template-build.user-data.yaml, __BUILD_SSH_KEY__) —
#      nothing session-specific is ever committed;
#   2. ensures the vendor cloud image is on the node (checksum-verified
#      download-url pull from cloud-images.ubuntu.com if absent);
#   3. boots a build VM once, unattended, with the seed as a cloud-init
#      user-data snippet; waits for cloud-init to finish;
#   4. verifies the build (wizard suppression, telemetry removal) and captures
#      the package manifest;
#   5. seals (cloud-init clean, machine-id truncate, build user removed) and
#      exports the immutable version set: qcow2 + .sha256 + the exact seed +
#      manifest.json.
#
# Usage:   ./build-template.sh root@<build-node> <version-label> [vmid]
# Example: ./build-template.sh root@10.40.3.253 24.04-v3 9902
#
# Requirements on the build node (NEVER a field node):
#   - root SSH access (this script's SSH use is a build-node-only privilege;
#     deploys go through the API token and never SSH)
#   - a storage with "Snippets" content (default: local) and one with
#     "Import" content for the vendor base
#   - outbound HTTPS to cloud-images.ubuntu.com (first build only)
#
# After it finishes: copy the four published files from $PUBLISH_DIR on the
# node to your firmware server's image root, then register the version in
# Nautobot (SoftwareVersion Staged + SoftwareImageFile) per
# docs/image-lifecycle.md.
set -euo pipefail

NODE="${1:?usage: build-template.sh root@<build-node> <version-label> [vmid]}"
VER="${2:?usage: build-template.sh root@<build-node> <version-label> [vmid]}"
VMID="${3:-9900}"

IMPORT_STORAGE="${IMPORT_STORAGE:-local}"     # holds the vendor base (Import content)
SNIPPET_STORAGE="${SNIPPET_STORAGE:-local}"   # holds the seed snippet (Snippets content)
VM_STORAGE="${VM_STORAGE:-local-lvm}"         # build VM disk
BRIDGE="${BRIDGE:-vmbr0}"                     # DHCP-capable bridge for the build VM
DISK_SIZE="${DISK_SIZE:-32G}"
PUBLISH_DIR="${PUBLISH_DIR:-/var/lib/vz/publish}"
VENDOR_FILE="${VENDOR_FILE:-ubuntu-24.04-server-cloudimg-amd64.qcow2}"
VENDOR_URL="${VENDOR_URL:-https://cloud-images.ubuntu.com/releases/noble/release/ubuntu-24.04-server-cloudimg-amd64.img}"
VENDOR_SHA_URL="${VENDOR_SHA_URL:-https://cloud-images.ubuntu.com/releases/noble/release/SHA256SUMS}"

NAME="ubuntu-jumphost-${VER}"
SEED_SRC="$(cd "$(dirname "$0")" && pwd)/template-build.user-data.yaml"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

SSHN="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $NODE"
log(){ echo "[$(date +%H:%M:%S)] $*"; }

log "=== ephemeral build key + seed staging ==="
ssh-keygen -q -t ed25519 -N '' -C "nfv-template-build-${VER}" -f "$WORK/buildkey"
BUILD_PUB="$(cat "$WORK/buildkey.pub")"
sed "s|__BUILD_SSH_KEY__|${BUILD_PUB}|" "$SEED_SRC" > "$WORK/${NAME}.user-data.yaml"
scp -o StrictHostKeyChecking=accept-new "$WORK/${NAME}.user-data.yaml" \
  "$NODE:/var/lib/vz/snippets/${NAME}.user-data.yaml"

log "=== vendor base (checksum-verified, idempotent) ==="
PVENODE="$($SSHN hostname)"
if ! $SSHN "pvesm list $IMPORT_STORAGE --content import 2>/dev/null" | grep -q "$VENDOR_FILE"; then
  VENDOR_SHA="$(curl -fsS "$VENDOR_SHA_URL" | awk -v f="$(basename "$VENDOR_URL")" '$2=="*"f||$2==f{print $1}')"
  [ -n "$VENDOR_SHA" ] || { log "FAIL: could not resolve vendor checksum"; exit 1; }
  log "pulling vendor base (sha256 $VENDOR_SHA)"
  $SSHN "pvesh create /nodes/$PVENODE/storage/$IMPORT_STORAGE/download-url \
    --content import --filename $VENDOR_FILE --url $VENDOR_URL \
    --checksum $VENDOR_SHA --checksum-algorithm sha256" >/dev/null
else
  log "vendor base already present"
fi

log "=== create + boot build VM $VMID ==="
$SSHN "qm create $VMID --name ${NAME}-build --memory 8192 --balloon 0 --sockets 1 --cores 4 \
  --cpu host --net0 virtio,bridge=$BRIDGE --scsihw virtio-scsi-single \
  --scsi0 ${VM_STORAGE}:0,import-from=${IMPORT_STORAGE}:import/${VENDOR_FILE} \
  --ide2 ${VM_STORAGE}:cloudinit --serial0 socket --agent 1 --ostype l26 --boot order=scsi0 \
  --cicustom user=${SNIPPET_STORAGE}:snippets/${NAME}.user-data.yaml --ipconfig0 ip=dhcp"
$SSHN "qm resize $VMID scsi0 $DISK_SIZE"
$SSHN "qm start $VMID"

log "=== wait for guest agent IP ==="
GIP=""
for _ in $(seq 1 120); do
  GIP=$($SSHN "qm agent $VMID network-get-interfaces 2>/dev/null" | python3 -c "
import json,sys
try:
    for i in json.load(sys.stdin):
        if i.get('name')=='lo': continue
        for a in i.get('ip-addresses',[]):
            if a['ip-address-type']=='ipv4' and not a['ip-address'].startswith('127'):
                print(a['ip-address']); raise SystemExit
except Exception: pass")
  [ -n "$GIP" ] && break
  sleep 10
done
[ -n "$GIP" ] || { log "FAIL: no agent IP after 20m"; exit 1; }
log "guest IP: $GIP"

# Guest SSH uses an ephemeral known_hosts: build VMs recycle DHCP addresses,
# so a persistent known_hosts would collide across builds.
GSSH="ssh -i $WORK/buildkey -o UserKnownHostsFile=$WORK/known_hosts \
  -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30 nfvbuild@$GIP"

log "=== wait for cloud-init (package install takes a while) ==="
$GSSH "timeout 2700 cloud-init status --wait" || true
$GSSH "cloud-init status" | grep -q done || {
  log "FAIL: cloud-init not done"; $GSSH "sudo tail -30 /var/log/cloud-init.log"; exit 1; }

log "=== verify wizard suppression + telemetry removal ==="
$GSSH "cat /etc/skel/.config/gnome-initial-setup-done | grep -qx yes" || { log "FAIL: wizard marker"; exit 1; }
$GSSH "systemctl is-enabled whoopsie 2>/dev/null" | grep -qv enabled || { log "FAIL: whoopsie enabled"; exit 1; }
log "verified"

log "=== capture manifest data ==="
$GSSH "dpkg-query -W -f '\${Package} \${Version}\n'" > "$WORK/packages.txt"
OS_DESC="$($GSSH "sed -n 's/^PRETTY_NAME=\"\(.*\)\"/\1/p' /etc/os-release; uname -r" | tr '\n' ' ')"
SEED_SHA="$(shasum -a 256 "$SEED_SRC" | awk '{print $1}')"
SEED_COMMIT="$(git -C "$(dirname "$SEED_SRC")" rev-parse HEAD 2>/dev/null || echo unknown)"

log "=== seal ==="
$GSSH "sudo bash -c 'cloud-init clean --logs; truncate -s0 /etc/machine-id; \
  rm -f /var/lib/dbus/machine-id; ln -sf /etc/machine-id /var/lib/dbus/machine-id; \
  userdel -f -r nfvbuild 2>/dev/null; poweroff -f'" || true
for _ in $(seq 1 40); do
  [ "$($SSHN "qm status $VMID" | awk '{print $2}')" = "stopped" ] && break; sleep 5
done
[ "$($SSHN "qm status $VMID" | awk '{print $2}')" = "stopped" ] || { log "FAIL: VM did not stop"; exit 1; }

log "=== export + publish version set ==="
$SSHN "qm set $VMID --delete cicustom; qm set $VMID --ide2 none,media=cdrom" >/dev/null 2>&1 || true
DISK_VOLID="$($SSHN "qm config $VMID" | sed -n 's/^scsi0: \([^,]*\),.*/\1/p')"
DISK_PATH="$($SSHN "pvesm path $DISK_VOLID")"
$SSHN "mkdir -p $PUBLISH_DIR && qemu-img convert -O qcow2 $DISK_PATH $PUBLISH_DIR/${NAME}.qcow2 \
  && cd $PUBLISH_DIR && sha256sum ${NAME}.qcow2 > ${NAME}.qcow2.sha256 \
  && cp /var/lib/vz/snippets/${NAME}.user-data.yaml ."
python3 - "$WORK/packages.txt" > "$WORK/${NAME}.manifest.json" <<PYEOF
import json, sys, datetime
packages = [l.strip() for l in open(sys.argv[1]) if l.strip()]
print(json.dumps({
    "name": "${NAME}",
    "built": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "build_node": "${PVENODE}",
    "vendor_base_url": "${VENDOR_URL}",
    "os": "${OS_DESC}".strip(),
    "seed_sha256": "${SEED_SHA}",
    "seed_git_commit": "${SEED_COMMIT}",
    "package_count": len(packages),
    "packages": packages,
}, indent=1))
PYEOF
scp -o StrictHostKeyChecking=accept-new "$WORK/${NAME}.manifest.json" "$NODE:$PUBLISH_DIR/"
$SSHN "qm template $VMID"

SHA="$($SSHN "awk '{print \$1}' $PUBLISH_DIR/${NAME}.qcow2.sha256")"
SIZE="$($SSHN "stat -c%s $PUBLISH_DIR/${NAME}.qcow2")"
log "=== BUILD COMPLETE ==="
cat <<EOF

Published on $NODE:$PUBLISH_DIR :
  ${NAME}.qcow2   (sha256 $SHA, $SIZE bytes)
  ${NAME}.qcow2.sha256
  ${NAME}.user-data.yaml   (exact seed used)
  ${NAME}.manifest.json

Next (docs/image-lifecycle.md):
  1. Copy all four files to your firmware server's image root.
  2. Register in Nautobot: SoftwareVersion "${VER}" (status STAGED) +
     SoftwareImageFile: image_file_name=${NAME}.qcow2, sha256 above,
     size ${SIZE}, download_url=https://<firmware-server>/images/${NAME}.qcow2
  3. Validate a Staged deploy, then promote the version Staged -> Active.
EOF
