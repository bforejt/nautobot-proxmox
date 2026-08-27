# Answer service — configuration reference

The SoT-backed engine of the bare-metal install loop (architecture and
runbook: [docs/baremetal-install.md](../../docs/baremetal-install.md)).
nautobot-composer's `answer-service` profile (the supported deployment)
maps these from `ANSWER_*` names in its `.env`. This table is the canonical
list.

## Core (every instance)

| Variable | Default | Purpose |
|---|---|---|
| `NAUTOBOT_URL` | — (required) | Nautobot API base, e.g. `http://nautobot:8080` |
| `NAUTOBOT_TOKEN` | — (required) | API token (device lookups, Secrets/state write-back) |
| `PUBLIC_URL` | — (required) | How **installing nodes** reach this service (LAN address, never localhost) |
| `SSL_CERTFILE` / `SSL_KEYFILE` | unset = plain HTTP | TLS keypair paths; HTTPS strongly preferred (the phone-home carries a live API token) |
| `CERT_FINGERPRINT` | empty | SHA256 of the TLS cert — rendered into `[first-boot]`/webhook pins and `/info`; must match what installer media was prepared with |
| `ANSWER_AUTH_TOKEN` | empty = off | Optional shared bearer on `/answer` (`prepare-iso --answer-auth-token`) |
| `NFV_ROLE` | `NFV` | Device role required by the serial allowlist (team convention) |
| `SECRETS_DIR` | `/secrets/nodes` | Where phone-home token files are written |
| `NAUTOBOT_SECRETS_PATH` | `/opt/nautobot/secrets/nodes` | The SAME files as the Nautobot containers see them (shared mount) |
| `NAUTOBOT_FS_UID` / `_GID` | `999` | chown target so Nautobot's text-file provider can read written secrets |
| `ROOT_PASSWORD_HASH_FILE` | `/secrets/root_password_hash` | SHA-512 crypt hash baked into answers (never plaintext) |
| `ROOT_SSH_KEYS_FILE` | empty = none | Optional root authorized keys, one per line |
| `VERIFY_PHONE_HOME_SOURCE` | `true` | Credentials phone-home must originate from the device's primary IP (skipped when no primary is set, e.g. DHCP installs) |
| `DOMAIN` / `COUNTRY` / `KEYBOARD` / `TIMEZONE` / `MAILTO` / `DNS_SERVER` | `nfv.lab` / `us` / `en-us` / `America/Chicago` / `root@localhost` / gateway | Answer-file fills |
| `KEY_TTL_SECONDS` | 4 h | One-time firstboot/webhook key lifetime |
| `CREDENTIALS_KEY_TTL_SECONDS` | 14 d | Phone-home key lifetime (long: nested installs power off between install and first boot) |
| `MAX_WEBHOOK_BYTES` | 256 KiB | Webhook payload cap |
| `PVE_ROLE_NAME` / `PVE_ROLE_PRIVS` / `PVE_SERVICE_USER` / `PVE_TOKEN_NAME` | `NFVAutomation` / validated set / `svc-nfv@pve` / `deploy` | What the firstboot `pveum` bootstrap creates on each node |
| `PROFILE_DIR` / `DATA_DIR` | `/app/profiles` / `/data` | Install profiles (baked at build; bind-mount to override) / key store + ISO cache + archives |

## Media forge (decision #44 — **off by default**, lab/build instances only)

| Variable | Default | Purpose |
|---|---|---|
| `ADMIN_ENABLED` | `false` | `false` = the `/admin/*` surface answers 404 (field posture). Enable only where media is prepared |
| `ADMIN_TOKEN` | empty | Bearer required on `/admin/*` when enabled |
| `FIRMWARE_PUBLISH_DIR` | empty = don't publish | Writable mount of the firmware server's storage; artifacts land as `proxmox-ve_<version>.iso` + `pxe/<version>/` |
| `FIRMWARE_BASE_URL` | empty = don't register | Device-facing base URL (plain HTTP for XCC1) used to build `download_url` at Staged registration |
| `PVE_ISO_BASE_URL` | `https://enterprise.proxmox.com/iso` | Stock-ISO mirror (SHA256SUMS-verified, cached in `DATA_DIR`) |

The forge's Nautobot-side plumbing (ExternalIntegration `nfv-answer-service`,
its SecretsGroup, the token Secret record) is created by
`Bootstrap NFV Data Model`. On composer stacks, **`./setup.sh --enable-forge`
supplies everything else in one command** — generates the bearer once, sets
the four `ANSWER_*` values, and mirrors the token into the secrets file the
job reads (`--disable-forge` reverses the enable, keeping credentials).
Elsewhere, supply the bearer value by hand into
`secrets/answer_service_admin_token` and set the variables above.
