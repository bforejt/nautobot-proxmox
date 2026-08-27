# nfv-helper — bolt the answer service onto an existing Nautobot compose project

For teams running Nautobot from **their own docker compose project** (not
[nautobot-composer](https://github.com/bforejt/nautobot-composer), which has
this pre-wired as the `answer-service` profile). This directory is copied
wholesale into your project and adds the
[bare-metal install engine](../docs/baremetal-install.md) via Docker Compose's
native multi-file merge — **your `docker-compose.yaml` is never edited**, and
removal restores your project byte-for-byte.

What the overlay does: (a) runs the answer-service container, (b) adds one
read-only mount to your existing Nautobot service so the per-node Proxmox API
tokens captured at firstboot resolve as text-file Secrets. That mount is the
single point of contact with your stack; your existing secrets workflow
(`.env` / `env_file` based) is untouched — the directory exists only for the
machine-generated `nodes/` credentials.

## Prerequisites

- A checkout of this repo on the docker host (build context), current `main`.
- A Nautobot API token for the service.
- Nautobot 2.4.x with this repo synced as a jobs Git Repository and the
  bootstrap job run ([getting-started](../docs/getting-started.md)).

## Install

```bash
cp -r /path/to/nautobot-proxmox/nfv-helper /opt/nautobot-docker/   # your project dir
cd /opt/nautobot-docker/nfv-helper
./setup.sh          # dirs, TLS keypair + fingerprint, root hash, .env scaffold
vi ../.env          # set NFV_NAUTOBOT_TOKEN, NFV_PUBLIC_URL, NFV_PROXMOX_REPO
```

Then check two things in [nfv-addon.yaml](nfv-addon.yaml):

- The `nautobot:` service key must match **your** service name
  (`docker compose config --services`). If a **separate celery worker**
  service exists, duplicate that block for it — workers resolve Secrets
  during job runs.
- `NFV_NAUTOBOT_URL` (default `http://nautobot:8080`) must match your
  service name and internal port.

## Activate — pick ONE mode

**Mode A — override filename (zero startup changes).** Works when your
startup command (systemd `ExecStart` or manual) runs plain
`docker compose up -d` **without `-f` flags** — compose then auto-loads
`docker-compose.override.yaml` from the project directory:

```bash
cp nfv-addon.yaml /opt/nautobot-docker/docker-compose.override.yaml
```

Nothing else changes: your systemd unit, aliases, and habits keep working,
and every future `docker compose` command in that directory includes the
add-on automatically. (Only if a `docker-compose.override.y*ml` already
exists would you merge by hand — rare.)

**Mode B — explicit `-f` (one edit, one place).** If your startup passes
`-f docker-compose.yaml` explicitly, compose **ignores** override files and
`COMPOSE_FILE`, so mode A silently won't apply. Add the second file to the
same command in your systemd unit:

```
ExecStart=... docker compose -f docker-compose.yaml -f nfv-helper/nfv-addon.yaml up -d
```

`systemctl daemon-reload` after. This is the only "chase it down" case, and
it is one line in one unit — interactive commands can keep using mode A
semantics by just adding the same `-f` pair.

Then:

```bash
cd /opt/nautobot-docker
docker compose up -d --build     # (with the -f pair if mode B)
```

This recreates the Nautobot service once (to pick up the mount) and builds +
starts the answer service.

## Media forge (optional)

The service can also prepare installer media (see
[docs/baremetal-install.md](../docs/baremetal-install.md), "media forge") —
**off by default**. To enable on a lab/build instance: set the
`NFV_ADMIN_*` values in the project `.env` (see
[nfv-addon.env.example](nfv-addon.env.example)), add a writable mount of
your firmware storage for publishing, write the same bearer to
`secrets/answer_service_admin_token`, and rebuild. (nautobot-composer
stacks get all of this via `./setup.sh --enable-forge`.)

## Verify

```bash
curl -k https://<NFV_PUBLIC_URL host>:8800/healthz     # -> ok  (run from the install network)
curl -k -X POST -H "Content-Type: application/json" \
  -d '{"dmi":{"system":{"serial":"BOGUS"}}}' https://<host>:8800/answer
# -> {"detail":"unknown machine"}   = up and talking to Nautobot
```

Then prepare installer media with `--url https://<host>:8800/answer
--fingerprint <NFV_CERT_FINGERPRINT>` — the fingerprint is **baked into the
ISO/PXE artifacts**, so if you ever regenerate the cert, re-prepare the media.

## Backup / update / remove

- **Back up**: `nfv-helper/tls/` (fingerprint baked into media),
  `nfv-helper/secrets/nodes/` (per-node tokens — recoverable only by
  reinstalling the node), and the `nfv_answer_data` volume.
- **Update**: `git pull` the checkout, then `docker compose up -d --build`.
- **Remove**: delete the override file (mode A) or the `-f` addition (mode B),
  `docker compose up -d --remove-orphans`, delete `nfv-helper/`. Your project
  is back to its original state.
