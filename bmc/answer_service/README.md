# Answer service — configuration reference

The SoT-backed engine of the bare-metal install loop (architecture and
runbook: [docs/baremetal-install.md](../../docs/baremetal-install.md)).
nautobot-composer's `answer-service` profile (the supported deployment)
maps these from `ANSWER_*` names in its `.env`. This table is the canonical
list.

Platform contract — what any deployment must provide and how the composer
does: [docs/platform-contract.md](../../docs/platform-contract.md).

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

## Install profiles (`PROFILE_DIR`)

One YAML per Nautobot DeviceType, named by the slugified model
(`ThinkEdge SE455 V3` → `thinkedge-se455-v3.yaml`); no profile = the
DeviceType is not installable (403). The service reads the `install`
section; the jobs read `delivery` and `storage`. Full semantics and the
runbooks: [docs/baremetal-install.md](../../docs/baremetal-install.md).

| Key | Purpose |
|---|---|
| `install.filesystem`, `install.lvm` / `.zfs` / `.btrfs` | `[disk-setup]` filesystem and its option family (`lvm.*` for ext4/xfs) |
| `install.disk_filter`, `install.filter_match` | udev-property globs selecting the installer's target disk(s); `any` (default) / `all` |
| `install.network_source` | `from-answer` (static from `primary_ip4` + DefaultGW; NIC filter from the pinned mgmt MAC — **required**: no pinned MAC, or one the installer did not report, is a 409 refusal) or `from-dhcp` |
| `install.interface_name_pinning` | PVE ≥ 9.1 name pinning; Device interfaces with a MAC supply the Linux names (transliterated to the Linux rule, e.g. `OCP-1` → `ocp_1`), the rest get `nic<N>` (decisions #51, #52) |
| `install.reboot_mode` | `reboot` / `power-off` after install |
| `install.data_pool` | Firstboot: ZFS mirror over the largest unused disk pair → zfspool storage (JBOD boxes) |
| `install.data_volume` | Firstboot: LVM-thin on the largest unused disk → lvmthin storage (RAID-adapter boxes, e.g. the SE455 V3's data volume) |
| `install.serial_console` | `{unit: N}` — the DeviceType HAS a serial port, `ttyS<N>` (0–7; only `unit` is accepted). The line settings come from the config context (below); with both, firstboot configures GRUB + kernel console and `serial-getty`. On for the SE455 V3 (decision #55) |
| `delivery.method`, `delivery.iso_url_schemes`, `delivery.vm` | Jobs: `pve-nested` / `redfish-vmedia` / `pxe`; ISO URL schemes the BMC can mount; nested VM sizing |
| `storage.controller`, `storage.volumes` | Jobs: out-of-band RAID volumes to ensure via the BMC before the installer boots (decision #50) |

## Config-context inputs (`host_baseline`, decision #55)

The Device is fetched with `?include=config_context`; firstboot renders these
keys of the rendered context (contract §4c). Absent keys are logged and
skipped; a malformed one is a `409 config context: …` at answer time, before
the installer runs:

| Key | Firstboot does |
|---|---|
| `packages` | Installs `lldpd` + `snmpd` (always) plus these Debian packages; enables `lldpd` |
| `serial_console` (`speed`; optional `word`, `parity`, `stop`) | With the profile's port: GRUB drop-in `/etc/default/grub.d/nfv-serial-console.cfg` (`console=tty0 console=ttyS<N>,<speed><parity><word>`, `GRUB_TERMINAL="console serial"`, `GRUB_SERIAL_COMMAND`), `/etc/kernel/cmdline` on systemd-boot layouts, `proxmox-boot-tool refresh` when it manages the ESPs else `update-grub`, `serial-getty@ttyS<N>` |
| `zfs_arc_max_bytes` | `/etc/modprobe.d/zfs.conf` + `update-initramfs -u -k all` (and the runtime limit when ZFS is loaded) |
| `remove_subscription_nag` | `/usr/local/sbin/nfv-remove-subscription-nag` + apt `DPkg::Post-Invoke` hook (`/etc/apt/apt.conf.d/86nfv-remove-subscription-nag`); never fails apt, logs when the pattern is not found |

These steps run after the credentials phone-home and the data-storage step,
each idempotent and non-fatal. The install NIC for static installs is
derived through the Device's bridge/LAG model (primary IP → bridge → port or
bond → member flagged `primary_member`); an ambiguous model is a
`409 install NIC: …`.

`GET /info` lists `profile_features` (now with `serial_console`) and
`firstboot_features` (`packages`, `serial_console`, `zfs_arc_max_bytes`,
`remove_subscription_nag`): the install job refuses a stale image that lacks a
profile feature the DeviceType uses, and warns when the Device's config
context sets a firstboot input the image would ignore. Both checks run only
after the version handshake below has passed.

## Versioning and the jobs handshake (decision #56)

The two halves of the install loop reach their hosts by different roads —
the jobs through Nautobot's Git sync, this service as an image the composer
pulls — so each states what it is and the oldest counterpart it accepts,
and the jobs refuse before touching a BMC or the forge when the pair is out
of step.

| Where | Value | Meaning |
|---|---|---|
| `bmc/answer_service/VERSION` | `X.Y.Z` | The one repo version. Copied to `/app/VERSION` by the Dockerfile; `app.py` refuses to start without it (never serves `version: ""`) |
| `ghcr.io/bforejt/nautobot-proxmox-answer-service:vX.Y.Z` | image tag | Published by the tag workflow on every `vX.Y.Z` tag (`latest` too, but it defeats the pin). The tag must equal `VERSION` and the jobs' `JOBS_VERSION` or the workflow refuses it |
| `GET /info` → `version` | `X.Y.Z` | What this build is |
| `GET /info` → `min_jobs_version` | `X.Y.Z` | The oldest jobs this build accepts (`MIN_JOBS_VERSION` in `app.py`) |
| `jobs/lib/version.py` → `JOBS_VERSION` | `X.Y.Z` | What the synced jobs are — equal to `VERSION` on the same commit |
| `jobs/lib/version.py` → `MIN_ANSWER_SERVICE_VERSION` | `X.Y.Z` | The oldest service the jobs accept |
| composer `.env` → `ANSWER_SERVICE_VERSION` | `vX.Y.Z` | The pin the composer pulls (and the git ref it builds from when it builds) |

The handshake (`version_handshake` in `jobs/lib/answer_service.py`, run by
the install job's preflight and by Prepare Installer Media before any POST):
`version` missing or not a plain `X.Y.Z` (`-dev`, `+build`, `latest` do not
count) → refuse, the service predates the handshake; `version` below
`MIN_ANSWER_SERVICE_VERSION` → refuse, naming both versions and the fix (set
`ANSWER_SERVICE_VERSION` to the required tag or newer, pull, `up -d`; a
checkout rebuilds with `up -d --build`); `min_jobs_version` present and
above `JOBS_VERSION` → refuse, the fix is a Git-repository sync in Nautobot;
an unreachable service from the worker is a warning only (the installing
node is what must reach it). Every refusal text is in
[docs/baremetal-install.md](../../docs/baremetal-install.md) troubleshooting.

When to bump what: every release bumps `VERSION` and `JOBS_VERSION`
together (one commit, one `vX.Y.Z` tag). Raise `MIN_ANSWER_SERVICE_VERSION`
when the jobs start depending on something only a newer service does (a new
`/info` field, a profile key, a firstboot input they must see rendered).
Raise `MIN_JOBS_VERSION` when the service starts depending on something only
newer jobs do (a new `/answer` input, a changed Secret name). Operationally:
after syncing this repo into Nautobot, move the composer's
`ANSWER_SERVICE_VERSION` to a tag the jobs accept and pull; after pulling a
newer service, sync jobs it accepts.

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
