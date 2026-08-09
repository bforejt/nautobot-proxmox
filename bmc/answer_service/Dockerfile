# Build context is bmc/ (one level up), so the profiles can be baked in:
#   docker build -f answer_service/Dockerfile bmc/
FROM python:3.12-slim
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
