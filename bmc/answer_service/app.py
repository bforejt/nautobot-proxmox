"""
NFV answer service — the SoT-backed brain of the bare-metal install loop.

Delivery-agnostic by design: it never knows (or cares) how the installer was
booted — nested lab VM, Redfish virtual media, or PXE all land on the same
endpoints. The Proxmox automated installer POSTs its identity (DMI serials,
UUID, NIC MACs); this service matches that against Nautobot and answers only
for Devices it expects to be installing.

Endpoints (see docs/baremetal-install.md for the full flow):
  POST /answer                 installer identity POST -> per-node answer.toml
  GET  /firstboot              one-time-key gated per-node firstboot script
  POST /firstboot-credentials  pveum bootstrap phone-home -> Nautobot Secrets
  POST /webhook                installer post-install webhook -> state flip
  GET  /healthz

Security model (defense in depth, smallest-possible trust):
  - Serial allowlist: only Devices with the NFV role (team convention;
    NFV_ROLE env to override) and provisioning_state=awaiting_install get
    answers. Unknown machines that boot the installer get a 403 and install
    nothing.
  - Optional shared bearer token (ANSWER_AUTH_TOKEN) on /answer, matching
    `prepare-iso --answer-auth-token` (PVE 9.2+).
  - The firstboot URL, the credentials phone-home, and the webhook are all
    gated by ONE-TIME, per-answer keys: minted when an answer is issued,
    consumed only after their step fully succeeds (a transient Nautobot
    error never burns a key). Credentials keys get a long TTL because the
    nested profile deliberately powers off between install and first boot.
  - The credentials phone-home is additionally source-checked against the
    node's own management IP (VERIFY_PHONE_HOME_SOURCE).
  - This service holds the root password HASH (never plaintext) and writes
    per-node API tokens straight into text-file Secrets — nothing secret is
    ever rendered into logs. Run it over HTTPS (SSL_CERTFILE/SSL_KEYFILE +
    prepare-iso --cert-fingerprint); plain HTTP is acceptable only on an
    isolated lab VLAN, and the docs say so explicitly.
"""

import json
import logging
import os
import re
import secrets as pysecrets
import shlex
import threading
import time
from pathlib import Path

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from jinja2 import Environment, FileSystemLoader, StrictUndefined

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

log = logging.getLogger("answer-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
TEMPLATES.filters["shquote"] = shlex.quote

# ---- configuration (env) ----
NAUTOBOT_URL = os.environ.get("NAUTOBOT_URL", "").rstrip("/")
NAUTOBOT_TOKEN = os.environ.get("NAUTOBOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # how NODES reach this service
ANSWER_AUTH_TOKEN = os.environ.get("ANSWER_AUTH_TOKEN", "")  # optional bearer on /answer
# SHA256 of this service's TLS cert; rendered into [first-boot] and pinned by
# the phone-home when PUBLIC_URL is https with a self-signed cert.
CERT_FINGERPRINT = os.environ.get("CERT_FINGERPRINT", "")
DOMAIN = os.environ.get("DOMAIN", "nfv.lab")
COUNTRY = os.environ.get("COUNTRY", "us")
KEYBOARD = os.environ.get("KEYBOARD", "en-us")
TIMEZONE = os.environ.get("TIMEZONE", "America/Chicago")
MAILTO = os.environ.get("MAILTO", "root@localhost")
DNS_SERVER = os.environ.get("DNS_SERVER", "")  # from-answer installs; empty -> gateway
ROOT_PASSWORD_HASH_FILE = os.environ.get("ROOT_PASSWORD_HASH_FILE", "/secrets/root_password_hash")
ROOT_SSH_KEYS_FILE = os.environ.get("ROOT_SSH_KEYS_FILE", "")  # optional, one key per line
PROFILE_DIR = Path(os.environ.get("PROFILE_DIR", str(BASE_DIR.parent / "profiles")))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
# Where THIS service writes per-node secret files, and where the SAME files
# appear from Nautobot's point of view (shared volume, two mount points).
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/secrets/nodes"))
NAUTOBOT_SECRETS_PATH = os.environ.get("NAUTOBOT_SECRETS_PATH", "/opt/nautobot/secrets/nodes")
# uid/gid of the Nautobot container user (default 999 in the official image)
# so the text-file provider can read what we write on the shared volume.
NAUTOBOT_FS_UID = int(os.environ.get("NAUTOBOT_FS_UID", "999"))
NAUTOBOT_FS_GID = int(os.environ.get("NAUTOBOT_FS_GID", "999"))
# Credentials phone-home must originate from the node's own management IP.
VERIFY_PHONE_HOME_SOURCE = os.environ.get("VERIFY_PHONE_HOME_SOURCE", "true").lower() == "true"
MAX_WEBHOOK_BYTES = int(os.environ.get("MAX_WEBHOOK_BYTES", str(256 * 1024)))
# Service role + account created on every node by the firstboot bootstrap.
PVE_ROLE_NAME = os.environ.get("PVE_ROLE_NAME", "NFVAutomation")
PVE_ROLE_PRIVS = os.environ.get(
    "PVE_ROLE_PRIVS",
    "VM.Allocate,VM.Clone,VM.Config.CDROM,VM.Config.CPU,VM.Config.Cloudinit,"
    "VM.Config.Disk,VM.Config.HWType,VM.Config.Memory,VM.Config.Network,"
    "VM.Config.Options,VM.PowerMgmt,VM.Audit,VM.Console,"
    "Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,"
    "Sys.Audit,Sys.Modify,SDN.Use",
)
PVE_SERVICE_USER = os.environ.get("PVE_SERVICE_USER", "svc-nfv@pve")
PVE_TOKEN_NAME = os.environ.get("PVE_TOKEN_NAME", "deploy")
# Role a Device must carry to be answerable (team convention: "NFV" — the
# role for the servers; "Hypervisor" was judged not specific enough).
NFV_ROLE = os.environ.get("NFV_ROLE", "NFV")

app = FastAPI(title="NFV Answer Service", docs_url=None, redoc_url=None)

# ---- one-time keys ----
# {key: {"serial", "purpose": "firstboot"|"credentials"|"webhook", "issued"}}
# Persisted (atomically) so a container restart mid-install doesn't strand a
# node. Keys are PEEKED before fallible work and CONSUMED only after the step
# fully succeeds. Credentials keys live long: the nested profile powers off
# between install (key minted) and first boot (key used) by design.
_KEYS_FILE = DATA_DIR / "issued-keys.json"
_keys_lock = threading.Lock()
KEY_TTL_SECONDS = int(os.environ.get("KEY_TTL_SECONDS", str(4 * 3600)))
CREDENTIALS_KEY_TTL_SECONDS = int(
    os.environ.get("CREDENTIALS_KEY_TTL_SECONDS", str(14 * 86400))
)


def _ttl_for(entry: dict) -> int:
    return CREDENTIALS_KEY_TTL_SECONDS if entry.get("purpose") == "credentials" else KEY_TTL_SECONDS


def _load_keys() -> dict:
    try:
        return json.loads(_KEYS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_keys(keys: dict) -> None:
    now = time.time()
    keys = {k: v for k, v in keys.items() if now - v.get("issued", 0) < _ttl_for(v)}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _KEYS_FILE.with_name(_KEYS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(keys))
    os.replace(tmp, _KEYS_FILE)  # atomic: a crash never truncates the store


def issue_key(serial: str, purpose: str) -> str:
    key = pysecrets.token_urlsafe(24)
    with _keys_lock:
        keys = _load_keys()
        keys[key] = {"serial": serial, "purpose": purpose, "issued": time.time()}
        _save_keys(keys)
    return key


def _entry_valid(entry: dict | None, serial: str, purpose: str) -> bool:
    return bool(
        entry
        and entry.get("serial") == serial
        and entry.get("purpose") == purpose
        and time.time() - entry.get("issued", 0) < _ttl_for(entry)
    )


def peek_key(key: str, serial: str, purpose: str) -> bool:
    """Validate without consuming — use before any fallible work."""
    with _keys_lock:
        return _entry_valid(_load_keys().get(key), serial, purpose)


def consume_key(key: str, serial: str, purpose: str) -> bool:
    """Destructive only on a full match — a bad guess can't burn a key."""
    with _keys_lock:
        keys = _load_keys()
        if not _entry_valid(keys.get(key), serial, purpose):
            return False
        keys.pop(key)
        _save_keys(keys)
        return True


# ---- Nautobot REST helpers ----

def _nb(method: str, path: str, **kwargs):
    if not (NAUTOBOT_URL and NAUTOBOT_TOKEN):
        raise HTTPException(500, "answer service is not configured (NAUTOBOT_URL/TOKEN)")
    resp = requests.request(
        method,
        f"{NAUTOBOT_URL}/api{path}",
        headers={"Authorization": f"Token {NAUTOBOT_TOKEN}", "Accept": "application/json"},
        timeout=30,
        **kwargs,
    )
    if resp.status_code >= 400:
        log.error("Nautobot %s %s -> %s: %s", method, path, resp.status_code, resp.text[:500])
        raise HTTPException(502, f"Nautobot API error {resp.status_code} on {path}")
    return resp.json() if resp.text else {}


def device_by_serial(serial: str) -> dict | None:
    results = _nb("GET", "/dcim/devices/", params={"serial": serial, "depth": 1}).get("results", [])
    return results[0] if results else None


def default_gateway_for(ip_cidr: str) -> str | None:
    """Contract §3: gateway = the DefaultGW-role IP inside the address's prefix.
    NOTE: the ip-addresses `parent` filter takes a Prefix PK (UUID), not a
    CIDR string — Nautobot 2.4 rejects the string form with a 400."""
    addr = ip_cidr.split("/")[0]
    prefixes = _nb("GET", "/ipam/prefixes/", params={"contains": addr}).get("results", [])
    if not prefixes:
        return None
    prefixes.sort(key=lambda p: int(p["prefix"].split("/")[1]))
    parent = prefixes[-1]
    gws = _nb(
        "GET", "/ipam/ip-addresses/", params={"parent": parent["id"], "role": "DefaultGW"}
    ).get("results", [])
    if not gws:
        return None
    return gws[0]["address"].split("/")[0]


def mgmt_interface_mac(device: dict) -> str | None:
    """MAC pinned on the interface carrying primary_ip4, if the SoT has one."""
    primary = device.get("primary_ip4")
    if not primary:
        return None
    detail = _nb("GET", f"/ipam/ip-addresses/{primary['id']}/", params={"depth": 1})
    for assignment in detail.get("interfaces", []) or []:
        iface = _nb("GET", f"/dcim/interfaces/{assignment['id']}/")
        if iface.get("mac_address"):
            return iface["mac_address"]
    return None


# ---- profiles (bmc/profiles/<device-type-slug>.yaml) ----

def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def load_profile(device_type_model: str) -> dict:
    if yaml is None:
        raise HTTPException(500, "PyYAML is not installed in the answer-service image")
    path = PROFILE_DIR / f"{slugify(device_type_model)}.yaml"
    if not path.exists():
        raise HTTPException(
            403, f"no install profile for DeviceType {device_type_model!r} ({path.name})"
        )
    return yaml.safe_load(path.read_text())


# ---- endpoints ----

def _check_bearer(authorization: str | None) -> None:
    if ANSWER_AUTH_TOKEN and authorization != f"Bearer {ANSWER_AUTH_TOKEN}":
        raise HTTPException(401, "bad or missing answer auth token")


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


def _answer_impl(identity: dict) -> PlainTextResponse:
    dmi = identity.get("dmi", {})
    serial = (dmi.get("system") or {}).get("serial") or ""
    nics = identity.get("network_interfaces", []) or []
    if not serial:
        raise HTTPException(400, "identity POST carries no DMI system serial")

    device = device_by_serial(serial)
    if device is None:
        log.warning("REFUSED: unknown serial %r (NICs: %s)", serial, [n.get("mac") for n in nics])
        raise HTTPException(403, "unknown machine")
    role = (device.get("role") or {}).get("name", "")
    state = (device.get("custom_fields") or {}).get("provisioning_state")
    if role != NFV_ROLE or state != "awaiting_install":
        log.warning(
            "REFUSED: %s (serial %s) role=%r provisioning_state=%r",
            device["name"], serial, role, state,
        )
        raise HTTPException(403, "device is not awaiting install")

    profile = load_profile((device.get("device_type") or {}).get("model", ""))
    install = profile.get("install", {})

    # Network: static from the SoT when primary_ip4 exists, else DHCP.
    network_source = "from-dhcp"
    cidr = gateway = dns = ""
    net_filter: dict[str, str] = {}
    primary = device.get("primary_ip4")
    if primary and install.get("network_source", "from-answer") == "from-answer":
        cidr = primary["address"]
        gateway = default_gateway_for(cidr) or ""
        if not gateway:
            log.warning(
                "REFUSED: %s has primary_ip4 %s but no DefaultGW-role IP in its prefix",
                device["name"], cidr,
            )
            raise HTTPException(409, "no DefaultGW-role IP in the management prefix (contract §3)")
        dns = DNS_SERVER or gateway
        network_source = "from-answer"
        pinned = mgmt_interface_mac(device)
        mac = (pinned or (nics[0].get("mac") if nics else "") or "").lower()
        if mac:
            net_filter["ID_NET_NAME_MAC"] = f"*{mac.replace(':', '')}"

    root_hash = ""
    try:
        root_hash = Path(ROOT_PASSWORD_HASH_FILE).read_text().strip()
    except OSError:
        pass
    if not root_hash:
        raise HTTPException(500, "root password hash not provisioned (ROOT_PASSWORD_HASH_FILE)")
    root_ssh_keys: list[str] = []
    if ROOT_SSH_KEYS_FILE:
        try:
            root_ssh_keys = [
                line.strip()
                for line in Path(ROOT_SSH_KEYS_FILE).read_text().splitlines()
                if line.strip()
            ]
        except OSError:
            pass

    # Filesystem tuning: the installer's option family is `lvm.*` for
    # ext4/xfs (there is no ext4.*/xfs.* key family); zfs/btrfs use their
    # own names. Profiles therefore declare install.lvm / install.zfs / ...
    filesystem = install.get("filesystem", "ext4")
    fs_family = "lvm" if filesystem in ("ext4", "xfs") else filesystem

    firstboot_key = issue_key(serial, "firstboot")
    webhook_key = issue_key(serial, "webhook")
    rendered = TEMPLATES.get_template("answer.toml.j2").render(
        keyboard=KEYBOARD,
        country=COUNTRY,
        timezone=TIMEZONE,
        mailto=MAILTO,
        fqdn=f"{device['name']}.{DOMAIN}",
        root_password_hashed=root_hash,
        root_ssh_keys=root_ssh_keys,
        reboot_mode=install.get("reboot_mode", "reboot"),
        network_source=network_source,
        cidr=cidr,
        gateway=gateway,
        dns=dns,
        net_filter=net_filter,
        filesystem=filesystem,
        disk_filter=install.get("disk_filter", {}),
        fs_family=fs_family,
        fs_options=install.get(fs_family, {}),
        firstboot_url=f"{PUBLIC_URL}/firstboot?serial={serial}&key={firstboot_key}",
        cert_fingerprint=CERT_FINGERPRINT,
        webhook_url=f"{PUBLIC_URL}/webhook?serial={serial}&key={webhook_key}",
    )
    log.info(
        "ANSWERED: %s (serial %s) source=%s fs=%s",
        device["name"], serial, network_source, filesystem,
    )
    return PlainTextResponse(rendered, media_type="application/toml")


@app.post("/answer")
async def answer(request: Request, authorization: str | None = Header(default=None)):
    _check_bearer(authorization)
    identity = await request.json()
    # Nautobot calls are blocking `requests` — keep them off the event loop.
    return await run_in_threadpool(_answer_impl, identity)


def _firstboot_impl(serial: str, key: str) -> PlainTextResponse:
    if not peek_key(key, serial, "firstboot"):
        raise HTTPException(403, "invalid, expired, or already-used firstboot key")
    device = device_by_serial(serial)
    if device is None:
        raise HTTPException(403, "unknown machine")
    cred_key = issue_key(serial, "credentials")
    rendered = TEMPLATES.get_template("firstboot.sh.j2").render(
        node_name=device["name"],
        serial=serial,
        service_url=PUBLIC_URL,
        cert_fingerprint=CERT_FINGERPRINT,
        credentials_key=cred_key,
        pve_role=PVE_ROLE_NAME,
        pve_privs=PVE_ROLE_PRIVS,
        pve_user=PVE_SERVICE_USER,
        pve_token=PVE_TOKEN_NAME,
    )
    # Consume last: rendering succeeded, the script (with its credentials
    # key) is about to leave — only now is the firstboot key spent.
    consume_key(key, serial, "firstboot")
    return PlainTextResponse(rendered, media_type="text/x-shellscript")


@app.get("/firstboot")
def firstboot(serial: str, key: str):
    """Per-node firstboot script. The URL (incl. one-time key) was minted into
    this node's answer file; the installer fetches it once at install time.
    (Sync endpoint: FastAPI runs it in the threadpool.)"""
    return _firstboot_impl(serial, key)


def _write_secret_file(path: Path, content: str, mode: int) -> None:
    """Create with the right mode from the first byte; chown so the Nautobot
    container's text-file provider (uid/gid 999 by default) can read it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.chmod(path, mode)
    try:
        os.chown(path, NAUTOBOT_FS_UID, NAUTOBOT_FS_GID)
    except OSError as exc:
        log.error("chown %s to %s:%s failed (%s) — Nautobot may not be able to "
                  "read this secret", path, NAUTOBOT_FS_UID, NAUTOBOT_FS_GID, exc)


def _firstboot_credentials_impl(body: dict, client_host: str | None) -> dict:
    serial = body.get("serial", "")
    key = body.get("key", "")
    if not peek_key(key, serial, "credentials"):
        raise HTTPException(403, "invalid, expired, or already-used credentials key")
    device = device_by_serial(serial)
    if device is None:
        raise HTTPException(403, "unknown machine")
    if VERIFY_PHONE_HOME_SOURCE:
        primary = device.get("primary_ip4")
        expected = primary["address"].split("/")[0] if primary else None
        if expected and client_host != expected:
            log.warning(
                "REFUSED credentials for %s: phone-home from %s, expected %s",
                device["name"], client_host, expected,
            )
            raise HTTPException(403, "phone-home source does not match the node's management IP")
    token_id = body.get("token_id", "")
    token_secret = body.get("token_secret", "")
    if not (token_id and token_secret):
        raise HTTPException(400, "token_id and token_secret are required")

    name = device["name"]
    slug = slugify(name)
    if (device.get("custom_fields") or {}).get("secrets_group"):
        # Reinstall path (designed): a fresh install re-runs the bootstrap and
        # the old token died with the old OS — overwriting is correct, but say so.
        log.warning("OVERWRITING stored credentials for %s (reinstall)", name)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    id_file = SECRETS_DIR / f"{slug}_proxmox_token_id"
    secret_file = SECRETS_DIR / f"{slug}_proxmox_token_secret"
    _write_secret_file(id_file, token_id + "\n", 0o640)
    _write_secret_file(secret_file, token_secret + "\n", 0o640)

    group_name = f"{name}-proxmox"
    secret_ids = {}
    for kind, path in (("username", id_file), ("secret", secret_file)):
        secret_name = f"{slug}-proxmox-token-{kind}"
        existing = _nb("GET", "/extras/secrets/", params={"name": secret_name}).get("results", [])
        payload = {
            "name": secret_name,
            "provider": "text-file",
            "parameters": {"path": f"{NAUTOBOT_SECRETS_PATH}/{path.name}"},
        }
        if existing:
            secret_ids[kind] = existing[0]["id"]
            _nb("PATCH", f"/extras/secrets/{existing[0]['id']}/", json=payload)
        else:
            secret_ids[kind] = _nb("POST", "/extras/secrets/", json=payload)["id"]

    groups = _nb("GET", "/extras/secrets-groups/", params={"name": group_name}).get("results", [])
    group_id = groups[0]["id"] if groups else _nb(
        "POST", "/extras/secrets-groups/", json={"name": group_name}
    )["id"]
    have = {
        (a["secret_type"], a["secret"]["id"])
        for a in _nb(
            "GET", "/extras/secrets-groups-associations/", params={"secrets_group": group_id}
        ).get("results", [])
    }
    for kind, secret_id in secret_ids.items():
        if (kind, secret_id) not in have:
            _nb(
                "POST",
                "/extras/secrets-groups-associations/",
                json={
                    "secrets_group": group_id,
                    "secret": secret_id,
                    "access_type": "Generic",
                    "secret_type": kind,
                },
            )

    cf = dict(device.get("custom_fields") or {})
    cf["secrets_group"] = group_name
    # Belt and suspenders with the webhook: firstboot running IS proof the
    # install succeeded (it only executes on the installed OS), so advance
    # the state here too. Field lesson (PXE NUC, 2026-08-09): the webhook
    # can be lost, and on the PXE path no job is watching — a stuck
    # awaiting_install leaves the reinstall gate open.
    if cf.get("provisioning_state") == "awaiting_install":
        cf["provisioning_state"] = "bm_installed"
        log.info("state advanced to bm_installed via credentials phone-home (webhook missed?)")
    _nb("PATCH", f"/dcim/devices/{device['id']}/", json={"custom_fields": cf})
    # Everything durable — only now is the one-time key spent.
    consume_key(key, serial, "credentials")
    log.info("CREDENTIALS STORED: %s -> SecretsGroup %r (token id %s)", name, group_name, token_id)
    return {"status": "stored", "secrets_group": group_name}


@app.post("/firstboot-credentials")
async def firstboot_credentials(request: Request) -> dict:
    body = await request.json()
    client_host = request.client.host if request.client else None
    return await run_in_threadpool(_firstboot_credentials_impl, body, client_host)


def _webhook_impl(serial: str, key: str, body: dict) -> dict:
    if not peek_key(key, serial, "webhook"):
        log.warning("REFUSED webhook: bad key for serial %r", serial)
        raise HTTPException(403, "invalid, expired, or already-used webhook key")
    device = device_by_serial(serial)
    if device is None:
        raise HTTPException(403, "unknown machine")
    safe_serial = re.sub(r"[^A-Za-z0-9._-]", "_", serial)[:64] or "unknown"
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"install-{safe_serial}.json").write_text(json.dumps(body, indent=2))
    except OSError as exc:
        log.error("webhook payload archive failed for %s: %s", serial, exc)
    cf = dict(device.get("custom_fields") or {})
    if cf.get("provisioning_state") == "awaiting_install":
        cf["provisioning_state"] = "bm_installed"
        _nb("PATCH", f"/dcim/devices/{device['id']}/", json={"custom_fields": cf})
        log.info(
            "INSTALLED: %s (serial %s) -> provisioning_state=bm_installed", device["name"], serial
        )
    else:
        log.info("webhook for %s: state already %r", device["name"], cf.get("provisioning_state"))
    consume_key(key, serial, "webhook")
    return {"status": "recorded", "device": device["name"]}


@app.post("/webhook")
async def webhook(request: Request, serial: str, key: str) -> dict:
    """Proxmox [post-installation-webhook]: record the install, advance state.
    The serial+key ride the URL minted into this node's answer file."""
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "webhook payload too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "webhook payload is not JSON")
    return await run_in_threadpool(_webhook_impl, serial, key, body)
