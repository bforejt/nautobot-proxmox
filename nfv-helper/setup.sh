#!/bin/bash
# One-time scaffold for the NFV add-on. Run from inside the nfv-helper
# directory AFTER copying it into your compose project (see README.md):
#
#   cp -r nautobot-proxmox/nfv-helper /opt/nautobot-docker/
#   cd /opt/nautobot-docker/nfv-helper && ./setup.sh
#
# Creates the secrets/tls directories, generates the TLS keypair (printing
# the fingerprint you bake into installer media), captures the root-password
# hash for installed nodes, and seeds NFV_* variables into the project's
# .env. Idempotent. NEVER touches docker-compose.yaml or your systemd unit.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR=$(dirname "$(pwd)")

mkdir -p secrets/nodes tls
chmod 750 secrets secrets/nodes

if [ ! -f tls/answer-service.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 730 \
    -subj "/CN=answer-service" \
    -keyout tls/answer-service.key -out tls/answer-service.crt 2>/dev/null
  chmod 600 tls/answer-service.key
  echo "TLS keypair generated (tls/answer-service.crt)."
fi
FP=$(openssl x509 -in tls/answer-service.crt -outform der | sha256sum | cut -d' ' -f1)

if [ ! -f secrets/root_password_hash ]; then
  echo "Root password for INSTALLED nodes (input hidden; stored only as a SHA-512 hash):"
  read -rs PW
  openssl passwd -6 "$PW" > secrets/root_password_hash
  chmod 600 secrets/root_password_hash
  unset PW
  echo "Root password hash stored."
fi

ENVFILE="$PROJECT_DIR/.env"
touch "$ENVFILE"
add_var() { grep -q "^$1=" "$ENVFILE" 2>/dev/null || echo "$1=$2" >> "$ENVFILE"; }
add_var NFV_PROXMOX_REPO "/opt/nautobot-proxmox"
add_var NFV_NAUTOBOT_TOKEN ""
add_var NFV_PUBLIC_URL "https://CHANGE-ME:8800"
add_var NFV_CERT_FINGERPRINT "$FP"

cat <<EOF

Answer-service TLS fingerprint (pass to prepare-install-iso.sh --fingerprint,
already seeded into .env as NFV_CERT_FINGERPRINT):

  $FP

Next steps (details in README.md):
  1. Edit $ENVFILE — set NFV_NAUTOBOT_TOKEN, NFV_PUBLIC_URL, NFV_PROXMOX_REPO.
  2. From $PROJECT_DIR run:  docker compose config --services
     If your Nautobot service is not named "nautobot", rename that key in
     nfv-addon.yaml; if a separate celery worker service exists, duplicate
     the block for it.
  3. Activate the overlay — mode A (override filename, zero startup changes)
     or mode B (explicit -f in your systemd unit). See README.md.
  4. docker compose up -d --build   (first build takes a minute)
  5. Verify:  curl -k https://<NFV_PUBLIC_URL host>:8800/healthz   ->  ok
EOF
