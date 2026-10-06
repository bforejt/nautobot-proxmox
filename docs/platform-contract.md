# Platform Contract — What Any Deployment of the Install Loop Must Provide

The jobs in this repo are portable: they import only `nautobot`, `django` and
`requests`, and `Bootstrap NFV Data Model` creates the data model on any
Nautobot 2.4/3.x. What is *not* free-floating is the **answer service's
deployment contract** — a Nautobot URL and token, a secrets directory the
service writes and every Nautobot container reads, a TLS identity that
prepared media pins, a root password hash, firmware storage, a handful of
network paths and about twenty-five environment knobs. That contract is the
portability boundary of the bare-metal install loop. Decision #45 made the
[nautobot-composer](https://github.com/bforejt/nautobot-composer)
`answer-service` profile the only supported deployment (anything else is
documented-but-unsupported until it has been tested end to end); decision #56
made the dependency one-directional and versioned — the composer pins a
published service image and syncs this repo into Nautobot, this repo never
depends on the composer — and added a version handshake so the two halves
refuse to run out of step. This document is the contract both decisions rest
on.

How to read it: nine items, in the order a deployment meets them. Each states
the **Requirement** — what the code needs, with the service variable or job
input that carries it — and **Composer** — exactly how the composer satisfies
it (the `setup.sh` flag, the file, the compose service). A non-composer
deployment must satisfy every Requirement; the Composer paragraph is the
worked example and the source of each default. Addresses and names here are
fictional (`192.0.2.10`, `example.net`); nothing in this document is a site
value.

## 1. Nautobot, this repo as a Git repository, the bootstrap

**Requirement.** Nautobot 2.4.x or 3.x (built on 2.4.30, validated on 3.2;
core `SoftwareVersion`/`SoftwareImageFile`, so ≥ 2.2) with this repo added as
a **Git Repository** providing *jobs* and synced — the repo-root
[`__init__.py`](../__init__.py) is load-bearing, Nautobot imports the checkout
as a package — and **`Bootstrap NFV Data Model`** run once per environment
and again after every sync (idempotent: it adds only what is new). The
bootstrap creates everything the service and the install jobs look up by
name: the `NFV` role, the `proxmox-ve` platform, the Staged/Retired statuses,
`provisioning_state` and the other custom fields, the standard Secret records
(item 3), the `nfv-answer-service` ExternalIntegration (item 7) and the
`nfv-host-baseline` config-context schema. Nautobot must reach the git host.
A sync pulls a branch tip, not a release: the jobs' version is whatever was
synced, which is why item 9 exists.

**Composer.** `./setup.sh --with-nfv-jobs` — registers the repo
(`NFV_JOBS_REPO_URL` / `NFV_JOBS_REPO_BRANCH` in `.env`, matched by remote
URL), syncs it, enables its jobs and runs the bootstrap through the API with
no inputs against a healthy stack (so the bootstrap's defaults, item 3, must
reproduce the composer layout — they do). Re-run the flag, or sync and
re-run the bootstrap by hand, after every merge.

## 2. A Nautobot API token for the service

**Requirement.** `NAUTOBOT_URL` + `NAUTOBOT_TOKEN`. The token needs exactly
the object permissions below, derived from the REST calls in
[`bmc/answer_service/app.py`](../bmc/answer_service/app.py) (the `_nb()`
call sites); nothing else is called, so a scoped token is the right posture
outside a lab.

| Permission | Why (the call) |
|---|---|
| view `dcim.device` | `GET /dcim/devices/?serial=…&include=config_context` — the serial allowlist and the rendered config context firstboot reads (`host_baseline`) |
| change `dcim.device` | `PATCH /dcim/devices/<id>/` with `custom_fields` — `provisioning_state` (webhook / phone-home) and `secrets_group` (phone-home); no other field is written |
| view `dcim.interface` | `GET /dcim/interfaces/?device_id=…` — the install NIC derivation and interface name pinning |
| view `ipam.ipaddress` | `GET /ipam/ip-addresses/<id>/` and the list filtered by `parent` + `role=DefaultGW` — static network, gateway, the pinned mgmt MAC |
| view + add + change `extras.secret` | `GET`/`POST`/`PATCH /extras/secrets/` — the per-node token records (`PATCH` on a reinstall re-points the existing record) |
| view + add `extras.secretsgroup` | `GET`/`POST /extras/secrets-groups/` — the per-node group |
| view + add `extras.secretsgroupassociation` | `GET`/`POST /extras/secrets-groups-associations/` — username + secret into the group |
| view `extras.status` *(forge only)* | `GET /extras/statuses/` — Staged and Active for registration |
| view `dcim.platform` *(forge only)* | `GET /dcim/platforms/?name=proxmox-ve` |
| view + add `dcim.softwareversion` *(forge only)* | `GET`/`POST /dcim/software-versions/` — the collision check, then the Staged version |
| add `dcim.softwareimagefile` *(forge only)* | `POST /dcim/software-image-files/` — the prepared ISO's checksum, size and `download_url` |

The four *forge only* rows are reached only with `ADMIN_ENABLED=true`
(item 6); a field instance's token can omit them. The media-forge job itself
authenticates to the service with the admin bearer (item 6), not with this
token.

**Composer.** `ANSWER_NAUTOBOT_TOKEN` in `.env`, passed as `NAUTOBOT_TOKEN`;
`NAUTOBOT_URL` is fixed to `http://nautobot:8080` on the compose network. On
the lab tier (`NAUTOBOT_ENV=lab`) `--with-answer-service` copies the
generated superuser token when the value is empty — written visibly, so
swapping in a scoped token is a one-line edit; staging and production get no
automatic admin credential and set a scoped token themselves. The
`answer-service-preflight` one-shot refuses `up` while the token or
`ANSWER_PUBLIC_URL` is empty.

## 3. The secrets channel

**Requirement.** One directory, two views, three settings that must agree.
The service writes each captured per-node token as two files (`0640`, owner
`NAUTOBOT_FS_UID`:`NAUTOBOT_FS_GID`, default `999:999` — Nautobot's container
uid/gid, so the text-file provider can read them) into **`SECRETS_DIR`**
(default `/secrets/nodes`) and creates text-file Secret records whose `path`
is **`NAUTOBOT_SECRETS_PATH`**`/<file>` (default `/opt/nautobot/secrets/nodes`)
— the same files *as the Nautobot containers see them*. Every Nautobot
container that resolves Secrets (web for *Check Secret*, the Celery worker for
jobs) therefore mounts that directory at that path, readable by its uid/gid.
The **Celery worker mounts it read-write**: the Host Baseline job writes the
service-account tokens beside the deploy token, into
**`NFV_NODE_SECRETS_DIR`** (default `/opt/nautobot/secrets/nodes`), and
refuses when it cannot. The bootstrap's own records (`xcc_username`,
`xcc_password`, `host_ssh_username`, `host_ssh_password`,
`proxmox_token_id`/`_secret`, `answer_service_admin_token`, the host-baseline
names …) are created once with the provider and prefix the job is given —
`secrets_provider` (`text-file`, the default, or `environment-variable`),
`secrets_path_prefix` (default `/opt/nautobot/secrets`; text-file records
point at `<prefix>/<name>`; ignored for `environment-variable`),
`secrets_env_prefix`. The per-node token records are never the bootstrap's:
the service creates them at `NAUTOBOT_SECRETS_PATH/<file>` and the Host
Baseline job at `NFV_NODE_SECRETS_DIR/<file>`, always text-file, whatever
provider the bootstrap used. **All three must agree** with the text-file
provider: the bootstrap's `secrets_path_prefix` + `/nodes` =
`NAUTOBOT_SECRETS_PATH` = `NFV_NODE_SECRETS_DIR`, or the records point at
files nobody wrote; with `environment-variable` the prefix drops out and the
latter two must still agree — and the directory must still be mounted there
in every Nautobot container. Values are never created by the bootstrap: the
operator writes the files (or sets the variables) the records name.

**Composer.** `./secrets` → `/opt/nautobot/secrets:ro` in the `nautobot` and
`celery_beat` containers (the shared `x-nautobot-volumes` anchor); the
`celery_worker` restates that list and adds `./secrets/nodes` →
`/opt/nautobot/secrets/nodes` read-write (nautobot-composer#66; the worker
keeps the default `NFV_NODE_SECRETS_DIR`); the service mounts `./secrets` at
`/secrets` with `SECRETS_DIR=/secrets/nodes` and
`NAUTOBOT_SECRETS_PATH=/opt/nautobot/secrets/nodes`. `setup.sh` creates
`secrets/nodes` before the first `up` (a Compose-created directory would be
root-owned) and sets `./secrets` to owner = you, group = `999`, directories
`750`, files `640`, and `secrets/nodes` **`770`** so the worker can write it.
The bootstrap's defaults reproduce exactly this layout, which is what lets
`--with-nfv-jobs` run it without inputs; `./add-secret.sh <name>` and
`./setup.sh --nfv-secrets` supply the values.

## 4. TLS identity and media pinning

**Requirement.** **`PUBLIC_URL`** — how *installing nodes* reach the service
(a LAN address or name, never localhost; e.g. `https://192.0.2.10:8800`);
**`SSL_CERTFILE`** / **`SSL_KEYFILE`** — HTTPS is the expected posture, the
phone-home carries a live API token; **`CERT_FINGERPRINT`** — the SHA256 of
that certificate. Prepared installer media bakes `PUBLIC_URL` in and pins the
fingerprint: the firstboot script and the webhook phone home only to that
identity, on the same TLS connection that carries the token (decision #52).
The media forge reads both values from `/info` so media can only ever be
prepared against the running identity (decision #44). The consequence is the
one operational rule here: **never casually regenerate the certificate** — a
new fingerprint invalidates every prepared artifact, and the fix is to
re-prepare each one (one `Prepare Installer Media (Media Forge)` run per
version, which is what #44 bought). Back the keypair up; it is not a
repopulatable cache.

**Composer.** `answer-service/certs/answer-service.crt` + `.key`, mounted at
`/tls`, generated once by `./setup.sh --with-answer-service` and left
untouched on every later run when present; `ANSWER_CERT_FINGERPRINT` written
from the certificate's DER SHA256 and passed as `CERT_FINGERPRINT`;
`ANSWER_PUBLIC_URL` auto-detected only when empty (`https://<host primary
IP>:<ANSWER_PORT>` from the routing table — set it yourself on a multi-homed
host or when nodes use a DNS name) and passed as `PUBLIC_URL`. `backup.sh`
does not cover `answer-service/certs/` — back it up separately.

## 5. The root password hash

**Requirement.** **`ROOT_PASSWORD_HASH_FILE`** (default
`/secrets/root_password_hash`): a SHA-512 crypt hash (`openssl passwd -6`),
readable by the service, rendered into every answer as the installed node's
root password — the service never holds the plaintext. Rotate by replacing
the file. It is a file, not a variable, on purpose: a `$`-laden crypt hash
does not survive Compose's `${…}` interpolation of `.env`.

**Composer.** `secrets/root_password_hash`, generated by
`--with-answer-service` (a random password, printed once at generation —
save it; only the hash is stored), owner = you, group `999`, mode `640`,
left untouched when present; reached through the service's `./secrets` →
`/secrets` mount.

## 6. Firmware storage and the media forge

**Requirement.** Two halves. Every deployment needs a **firmware server** the
Proxmox nodes and their BMCs reach at stable `/images/<file>` URLs — golden
images, prepared installer ISOs, PXE artifacts (XCC1 virtual media needs plain
HTTP). The **forge** — the one instance that *prepares* media — additionally
sets **`ADMIN_ENABLED=true`**, **`ADMIN_TOKEN`** (the bearer on `/admin/*`;
the same value in the Secret `answer_service_admin_token`, which the
bootstrap-created SecretsGroup `nfv-answer-service-admin` carries for the
`nfv-answer-service` ExternalIntegration the forge job authenticates
through), **`FIRMWARE_PUBLISH_DIR`** (a writable mount of the firmware
server's storage; artifacts land as `proxmox-ve_<version>.iso` +
`pxe/<version>/`), **`FIRMWARE_BASE_URL`** (the device-facing base used for
the registered `download_url`) and optionally `PVE_ISO_BASE_URL`. Field
instances keep `ADMIN_ENABLED=false` — the admin surface answers 404 (decision
#44) — and need none of the forge variables.

**Composer.** `./setup.sh --with-firmware --enable-forge`: the `firmware`
profile (nginx download endpoint + Filebrowser on the `nautobot_firmware`
volume, which the service also mounts at `/firmware-publish`);
`--enable-forge` implies the answer-service profile, sets
`ANSWER_ADMIN_ENABLED=true`, generates `ANSWER_ADMIN_TOKEN` once (never
rotated automatically) and mirrors it into
`secrets/answer_service_admin_token`, defaults
`ANSWER_FIRMWARE_PUBLISH_DIR=/firmware-publish` and
`ANSWER_FIRMWARE_BASE_URL` from the firmware profile's `FIRMWARE_BASE_URL`.
`--disable-forge` flips `ANSWER_ADMIN_ENABLED` back off, keeping the token.

## 7. Network paths

**Requirement.**

| From → to | Port / protocol | Why |
|---|---|---|
| Installing node → service | HTTPS to `PUBLIC_URL` (the service listens on 8800) | `/answer`, `/firstboot`, `/firstboot-credentials`, `/webhook`. The credentials phone-home must arrive from the Device's primary IP unless `VERIFY_PHONE_HOME_SOURCE=false` (published-port NAT, e.g. Docker Desktop) |
| Service → Nautobot | REST API at `NAUTOBOT_URL` | Item 2 |
| Celery worker → BMCs | Redfish, HTTPS 443 | Discovery, RAID layout, virtual media, boot override, power — each after the BMC serial check |
| Celery worker → nodes | SSH 22, root | Host verification, Host Baseline |
| Celery worker → Proxmox API | HTTPS 8006 | VNF deploys, the nested install carrier, verifying captured tokens |
| Celery worker → service `/info` | HTTPS, through the `nfv-answer-service` ExternalIntegration (`remote_url`, `verify_ssl`) | **The version handshake and the profile preflight (item 9)** — refusals before any BMC or node is touched. An unreachable `/info` is only a warning, so without this path the mismatch the handshake exists to catch surfaces as a wasted boot cycle instead |
| Nodes and BMCs → firmware server | HTTP(S) to `FIRMWARE_BASE_URL` | Image pulls, PXE artifacts, virtual-media ISO mounts |
| Nautobot → git host | HTTPS/SSH | The Git Repository sync (item 1) |

**Composer.** One compose network: the service is `answer-service:8800`
inside it — the bootstrap's ExternalIntegration default is
`https://answer-service:8800` with `verify_ssl: false`, so the worker's
`/info` path works out of the box — and is published on
`${ANSWER_BIND_ADDRESS:-0.0.0.0}:${ANSWER_PORT:-8800}` for the nodes;
Nautobot is `http://nautobot:8080`; the firmware server publishes
`FIRMWARE_HTTP_PORT` (80). The worker's paths to BMCs, nodes and the Proxmox
API are the host's own routing — the composer adds nothing there.

## 8. Environment reference

**Requirement.** The canonical list of every service variable, its default
and purpose is the service README —
[`bmc/answer_service/README.md`](../bmc/answer_service/README.md) — and is
deliberately not duplicated here: items 2–6 name the variables that *are*
the contract; the rest (`DOMAIN`, `TIMEZONE`, `NFV_ROLE`,
`ANSWER_AUTH_TOKEN`, `ROOT_SSH_KEYS_FILE`, `VERIFY_PHONE_HOME_SOURCE`, the
key TTLs …) tune behaviour without changing what the deployment must
provide. The `pveum` bootstrap names (`PVE_SERVICE_USER`, `PVE_TOKEN_NAME`,
`PVE_ROLE_NAME`, `PVE_ROLE_PRIVS`) are documented as variables but the jobs
assume their defaults (`svc-nfv@pve` is a reserved account in the Host
Baseline; the Proxmox client names the `NFVAutomation` role in its privilege
diagnostics) — leave them.

**Composer.** The container has no `env_file` (the stack `.env` holds
database credentials it must not see). The contract variables of items 2–6
are either fixed by the stack layout (`NAUTOBOT_URL`,
`SSL_CERTFILE`/`SSL_KEYFILE`, `ROOT_PASSWORD_HASH_FILE`, `SECRETS_DIR`,
`NAUTOBOT_SECRETS_PATH` — the compose network and mounts) or carried from
their `ANSWER_*` value. Every documented tuning knob is an explicit
`ANSWER_*` passthrough in `docker-compose.yml` (nautobot-composer#65) —
`ANSWER_<NAME>` → `<NAME>`, e.g. `ANSWER_NFV_ROLE` → `NFV_ROLE` — and an unset
one keeps the service default, **except** the ones the composer fixes on
purpose and never reads from `.env`: the `pveum` names above (must match what
the jobs expect), `NAUTOBOT_FS_UID`/`_GID` (the Nautobot image's uid/gid) and
`PROFILE_DIR`/`DATA_DIR` (the image and volume layout) — setting
`ANSWER_PVE_SERVICE_USER` or the like in `.env` is a silent no-op.
`env.example` documents each passthrough with its default.

## 9. The image version pin and the handshake

**Requirement.** The service and the jobs are two halves of **one repo
version** (`bmc/answer_service/VERSION`, copied into the image, and
`JOBS_VERSION` in `jobs/lib/version.py` — always equal). The service
**image** is published as
`ghcr.io/bforejt/nautobot-proxmox-answer-service:vX.Y.Z` (and `latest`) by
`.github/workflows/publish-answer-service.yml` on every `v*.*.*` tag, which
fails unless `VERSION` and `JOBS_VERSION` both equal the tag's version — the
tag minus its leading `v`: tag `v0.1.0` ↔ `VERSION` `0.1.0` ↔
`JOBS_VERSION = "0.1.0"`; the package is made public once in GitHub's
package settings. First tag: **`v0.1.0`**. A deployment runs a *pinned*
image (or builds the same tag's `bmc/` context).

The **handshake**: `GET /info` reports `version` (the running service) and
`min_jobs_version` (the oldest jobs it accepts); the jobs carry
`JOBS_VERSION` and `MIN_ANSWER_SERVICE_VERSION` (the oldest service they
accept). Versions are strict `X.Y.Z` with an optional leading `v`; anything
else (`-dev`, `+build`, `latest`, empty) is unparseable and treated as too
old — fail closed. `Install Proxmox Node (SoT-driven)` (its answer-service
preflight, before any BMC action) and `Prepare Installer Media (Media Forge)`
(right after `/info`, before any `POST`) **refuse** when the service's
`version` is older than `MIN_ANSWER_SERVICE_VERSION`, when `/info` carries no
usable `version` (the service predates the handshake), or when its
`min_jobs_version` is newer than `JOBS_VERSION` (or unparseable). An
unreachable `/info` stays a warning — the
installing node, not the worker, must reach the service. Each refusal names
both versions and the fix; the exact texts are troubleshooting rows in
[baremetal-install.md](baremetal-install.md#troubleshooting). The
operational rule that follows: **after a repo sync, pull a service the jobs
accept; after a pull, sync jobs the service accepts.** Bump
`MIN_ANSWER_SERVICE_VERSION` when the jobs start depending on something only a
newer service does; bump `MIN_JOBS_VERSION` in the service when it starts
depending on newer jobs; the repo version moves on every release.

**Composer.** `ANSWER_SERVICE_VERSION=v0.1.0` in `.env` (set with
`./setup.sh --answer-service-version vX.Y.Z` or by editing it): the
`answer-service` service is
`image: ghcr.io/bforejt/nautobot-proxmox-answer-service:${ANSWER_SERVICE_VERSION:-v0.1.0}`
(an unset pin still resolves to `v0.1.0`) with `pull_policy: missing` and a
build context tracking the same tag, so
`docker compose --profile answer-service pull answer-service && docker compose --profile answer-service up -d answer-service`
moves the pin, a checkout-less `docker compose build` reproduces it, and only
an explicit `ANSWER_SERVICE_BUILD_CONTEXT` (a local `bmc/` checkout, for
development) builds unversioned source. `latest` exists but defeats the pin.
After `--with-nfv-jobs` or a manual sync, move the pin to a tag the synced
jobs accept and pull; the handshake refuses until the two agree.

## What is not portable yet

- **The native Nautobot App** (decision #45's long-term answer, reaffirmed by
  #56) is out of scope. Until it exists the service is a container beside
  Nautobot with a shared secrets directory and its own TLS identity — this
  contract — and media preparation stays a small container even after the
  App (a native binary cannot live in the Nautobot image, #44).
- **Only the composer profile is tested.** A deployment that satisfies every
  item above should work, and the bootstrap's `environment-variable` provider
  and alternative path prefixes exist for it — but they are covered by unit
  tests, not by an end-to-end run; #45's ship bar (no supported feature
  without basic-use testing) applies until one does.
- **The handshake checks versions, not the deployment.** It catches a stale
  or too-new service image; it cannot see a secrets directory mounted at the
  wrong path or a mismatched fingerprint — items 3 and 4 remain the
  operator's to get right.
