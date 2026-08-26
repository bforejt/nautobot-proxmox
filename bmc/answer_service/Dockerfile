# Build context is bmc/ (one level up), so the profiles can be baked in:
#   docker build -f answer_service/Dockerfile bmc/
# Base pinned to the trixie (Debian 13) variant: the media-forge tool below
# comes from Proxmox's trixie repo and must match the base's libc/deps.
FROM python:3.12-slim-trixie
# Media forge (decision #44): proxmox-auto-install-assistant prepares
# installer media against this service's own identity. The tool is present
# in every image but the /admin surface is DISABLED unless ADMIN_ENABLED.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-trixie.gpg \
      -o /usr/share/keyrings/proxmox-release-trixie.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/proxmox-release-trixie.gpg] http://download.proxmox.com/debian/pve trixie pve-no-subscription" \
      > /etc/apt/sources.list.d/pve.list \
 && apt-get update && apt-get install -y --no-install-recommends proxmox-auto-install-assistant xorriso \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY answer_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY answer_service/app.py .
COPY answer_service/templates/ templates/
# Profiles baked at build time from bmc/profiles. Mount over /app/profiles at
# runtime to update without rebuilding.
COPY profiles/ profiles/
ENV PROFILE_DIR=/app/profiles DATA_DIR=/data
EXPOSE 8800
# HTTPS when SSL_CERTFILE/SSL_KEYFILE are set (pair the cert's SHA256 with
# CERT_FINGERPRINT and prepare-iso --cert-fingerprint); plain HTTP otherwise.
CMD ["sh", "-c", "if [ -n \"${SSL_CERTFILE:-}\" ]; then exec uvicorn app:app --host 0.0.0.0 --port 8800 --ssl-certfile \"$SSL_CERTFILE\" --ssl-keyfile \"$SSL_KEYFILE\"; else exec uvicorn app:app --host 0.0.0.0 --port 8800; fi"]
