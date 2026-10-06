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
  GET  /info                   identity, version + min_jobs_version (the jobs'
                               handshake), baked-in profile list (jobs' preflight)
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
  - Fail closed on ambiguity (decision #52): a serial matching several
    Devices, a static install whose mgmt interface pins no MAC (or a MAC the
    installer did not report), and an invalid profile key are refused at
    answer time — never resolved by a guess.
  - Per-DeviceType install profiles (bmc/profiles/<slug>.yaml) are the only
    place hardware policy lives: disk filter / filter-match, filesystem
    options, install.data_pool (JBOD boxes: firstboot builds a ZFS data
    mirror) or install.data_volume (RAID-adapter boxes such as the SE455 V3:
    firstboot turns the data virtual drive into LVM-thin), each registered as
    a PVE storage. The RAID adapter itself is laid out by the install job over
    Redfish from the profile's `storage` section (decision #50).
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


def _read_service_version() -> str:
    """The one repo version (bmc/answer_service/VERSION, copied next to app.py
    by the Dockerfile). A build without it is broken: fail here, at container
    start, rather than serve version "" and let the jobs' handshake guess."""
    path = BASE_DIR / "VERSION"
    try:
        text = path.read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"{path} is missing — the image was built without VERSION ({exc})") from exc
    if not re.match(r"^\d+\.\d+\.\d+\Z", text):
        raise RuntimeError(f"{path} must hold one X.Y.Z version, got {text!r}")
    return text


# Version handshake (decision #56). The jobs (synced into Nautobot from the
# same repo) read these from GET /info before touching a BMC or the forge:
# a service older than their MIN_ANSWER_SERVICE_VERSION, or one whose
# MIN_JOBS_VERSION is above their JOBS_VERSION, is refused with the fix.
SERVICE_VERSION = _read_service_version()
# The oldest jobs this build accepts — bump when the service starts relying
# on something only newer jobs do (a new /answer input, a changed Secret name).
MIN_JOBS_VERSION = "0.1.0"

TEMPLATES = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)
TEMPLATES.filters["shquote"] = shlex.quote

_TOML_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
                 "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")


def toml_value(value) -> str:
    """Render a scalar as a TOML literal: bool/int/float as-is, everything
    else as an escaped basic string. Every value the answer template places
    in answer.toml goes through this — a SoT/profile/env string can never
    close its quotes and inject keys."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    out = []
    for ch in str(value):
        if ch in _TOML_ESCAPES:
            out.append(_TOML_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def toml_key(key) -> str:
    """A TOML key: bare when it is a plain identifier, else quoted."""
    key = str(key)
    return key if _TOML_BARE_KEY_RE.match(key) else toml_value(key)


TEMPLATES.filters["toml"] = toml_value
TEMPLATES.filters["tomlkey"] = toml_key

# The Device name becomes the node's hostname (fqdn = <name>.<DOMAIN>): an
# RFC 1123 label — letters, digits, hyphen, 1-63 chars, no leading/trailing
# hyphen — and not all digits (the PVE installer rejects a numeric host).
# Keep in step with jobs/lib/answer_service.py HOSTNAME_LABEL_RE.
HOSTNAME_LABEL_RE = re.compile(r"^(?![0-9]+\Z)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

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
    "VM.Config.Options,VM.PowerMgmt,VM.Audit,VM.GuestAgent.Audit,VM.Console,"
    "Datastore.Allocate,Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit,"
    "Sys.Audit,Sys.Modify,SDN.Use",
)
PVE_SERVICE_USER = os.environ.get("PVE_SERVICE_USER", "svc-nfv@pve")
PVE_TOKEN_NAME = os.environ.get("PVE_TOKEN_NAME", "deploy")
# Role a Device must carry to be answerable (team convention: "NFV" — the
# role for the servers; "Hypervisor" was judged not specific enough).
NFV_ROLE = os.environ.get("NFV_ROLE", "NFV")

# ---- media forge (admin surface; decision #44) ----
# DISABLED BY DEFAULT: field-deployed instances serve installs only. Enable
# (plus a bearer token) ONLY on the lab/build instance that prepares
# installer media. While disabled the /admin/* endpoints answer 404 — the
# surface does not exist. Post-Option-D this capability stays containerized
# (the prepare tool is a native binary that cannot live in the Nautobot App).
ADMIN_ENABLED = os.environ.get("ADMIN_ENABLED", "false").lower() == "true"
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
PVE_ISO_BASE_URL = os.environ.get("PVE_ISO_BASE_URL", "https://enterprise.proxmox.com/iso").rstrip("/")
# Publish adapter, "volume" mode: a writable mount of the firmware server's
# storage — prepared artifacts are copied in (and served immediately). Empty:
# artifacts stay under /data and the task result reports their paths.
FIRMWARE_PUBLISH_DIR = os.environ.get("FIRMWARE_PUBLISH_DIR", "")
# Device-facing base URL of the firmware server (plain HTTP for XCC1 mounts);
# used to build download_url at registration. Empty disables auto-register.
FIRMWARE_BASE_URL = os.environ.get("FIRMWARE_BASE_URL", "").rstrip("/")

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
    """The ONE Device carrying this serial. Several matches are refused —
    ambiguity never resolves to a guess (which node's answer, keys, secrets?)."""
    # include=config_context: the rendered context carries host_baseline, the
    # firstboot inputs (packages, serial console, ARC, nag switch) — verified
    # on the list endpoint of Nautobot 2.4.30; 3.x keeps the parameter.
    results = _nb(
        "GET", "/dcim/devices/",
        params={"serial": serial, "depth": 1, "include": "config_context"},
    ).get("results", [])
    if len(results) > 1:
        log.warning("REFUSED: serial %r matches %d Devices (%s) — serials must be unique",
                    serial, len(results), ", ".join(str(d.get("name")) for d in results))
        raise HTTPException(409, "serial matches more than one Device")
    return results[0] if results else None


def default_gateway_for(primary: dict) -> str | None:
    """Contract §3: gateway = the DefaultGW-role IP inside the address's prefix.
    The prefix is the primary IP's OWN parent (IPAddress.parent, a Prefix in
    the IP's namespace) — never a namespace-blind `contains` search, which
    could pick an identically numbered prefix from another namespace.
    NOTE: the ip-addresses `parent` filter takes a Prefix PK (UUID), not a
    CIDR string — Nautobot 2.4 rejects the string form with a 400."""
    detail = _nb("GET", f"/ipam/ip-addresses/{primary['id']}/")
    parent = detail.get("parent")
    parent_id = parent.get("id") if isinstance(parent, dict) else parent
    if not parent_id:
        return None
    gws = _nb(
        "GET", "/ipam/ip-addresses/", params={"parent": parent_id, "role": "DefaultGW"}
    ).get("results", [])
    if not gws:
        return None
    return gws[0]["address"].split("/")[0]


# Interface names the installer accepts for pinning (pve-iface: letter first,
# ASCII alnum/underscore) capped at IFNAMSIZ-1 = 15; the installer's own
# default namespace nic<N> is off limits — a SoT name clashing with an
# enumerated default would fail the whole install ("duplicate interface name").
_IFNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,14}$")
_DEFAULT_PIN_RE = re.compile(r"^nic\d+$")


def pin_name(name: str) -> str:
    """Nautobot interface name -> Linux pin name, deterministically (decision
    #51/#52): lowercase; each run of characters outside [a-z0-9_] -> "_";
    strip leading/trailing "_"; prefix "p_" unless it starts with a letter;
    truncate to 15. "OCP-1" -> "ocp_1", "1GbE-4" -> "p_1gbe_4", "mgmt" ->
    "mgmt". The caller still validates the result (2-15 chars, not nic<N>)."""
    value = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if value and not value[0].isalpha():
        value = "p_" + value
    return value[:15]


def device_interfaces(device: dict) -> list:
    """Every interface of the Device (all pages). Depth 0: `lag` and `bridge`
    are FK references ({"id": ...}) — present on 2.4 and 3.x alike (only M2M
    fields need exclude_m2m=false)."""
    out, offset = [], 0
    while True:
        page = _nb(
            "GET", "/dcim/interfaces/",
            params={"device_id": device["id"], "limit": 200, "offset": offset},
        )
        results = page.get("results", []) or []
        out.extend(results)
        offset += len(results)
        if not page.get("next") or not results:
            return out


def build_pin_mapping(interfaces: list, nics: list, device_name: str = "") -> dict:
    """SoT interface names by MAC for [network.interface-name-pinning.mapping].

    Only MACs the installer reported in its identity POST are mapped — the
    answer file names what is really there; every other physical NIC keeps
    the installer's default nic<N> (enumeration order). The `xcc` interface
    holds the BMC address, not a host NIC. Names the Linux rule rejects are
    transliterated by pin_name() (logged); entries still unusable — too
    short, the nic<N> namespace, a duplicate (first wins) — are skipped with
    a log line, never guessed."""
    seen = {str(n.get("mac") or "").lower() for n in nics or [] if n.get("mac")}
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for iface in interfaces or []:
        mac = str(iface.get("mac_address") or "").lower()
        name = str(iface.get("name") or "")
        if not mac or name == "xcc" or _iface_type(iface) in ("lag", "bridge"):
            continue
        if mac not in seen:
            log.info("%s: interface %s (%s) not reported by the installer — not pinned",
                     device_name, name, mac)
            continue
        linux = pin_name(name)
        if not _IFNAME_RE.match(linux):
            log.warning("%s: interface name %r -> %r is not a valid Linux/pve-iface name "
                        "(letter first, alnum/underscore, 2-15 chars) — %s keeps nic<N>",
                        device_name, name, linux, mac)
            continue
        if _DEFAULT_PIN_RE.match(linux):
            log.warning("%s: interface name %r squats the installer's default nic<N> "
                        "namespace — %s keeps its enumerated name", device_name, name, mac)
            continue
        if linux in used:
            log.warning("%s: pin name %r (from %r) is already used by %s — %s not pinned",
                        device_name, linux, name, used[linux], mac)
            continue
        if linux != name:
            log.info("%s: interface name %r -> %r (Linux pin name)", device_name, name, linux)
        used[linux] = mac
        mapping[mac] = linux
    return mapping


def _ref_id(value):
    """FK reference (dict at depth 0/1, or a bare id) -> id string or None."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


def _iface_type(iface: dict) -> str:
    value = iface.get("type")
    return str((value.get("value") if isinstance(value, dict) else value) or "")


def _iface_record(iface: dict) -> dict:
    """REST interface -> the plain record derive_install_interface works on
    (the same shape jobs/lib/host_baseline.interface_record builds)."""
    return {
        "id": str(iface.get("id")),
        "name": str(iface.get("name") or ""),
        "type": _iface_type(iface),
        "lag": _ref_id(iface.get("lag")),
        "bridge": _ref_id(iface.get("bridge")),
        "mac": str(iface.get("mac_address") or "").lower() or None,
        "primary_member": (iface.get("custom_fields") or {}).get("primary_member") is True,
    }


class InstallNicError(ValueError):
    """The SoT does not name exactly one install port for the node."""


def _pick_member(device_name, kind, parent, members):
    names = sorted(m["name"] for m in members)
    if not members:
        field = "Bridge" if kind == "bridge" else "LAG"
        raise InstallNicError(
            f"{kind} {parent['name']} on {device_name} has no member interfaces — set the "
            f"{field} field of its port(s) to {parent['name']}"
        )
    flagged = [m for m in members if m.get("primary_member") is True]
    if len(flagged) > 1:
        raise InstallNicError(
            f"{kind} {parent['name']} on {device_name}: primary_member is set on several members "
            f"({', '.join(sorted(m['name'] for m in flagged))}) — flag exactly one"
        )
    if len(members) == 1:
        return members[0]
    if flagged:
        return flagged[0]
    raise InstallNicError(
        f"{kind} {parent['name']} on {device_name} has several members ({', '.join(names)}) and "
        f"none is flagged primary_member — flag the port that carries the install"
    )


def derive_install_interface(device_name, primary_address, primary_ids, interfaces):
    """The install NIC through the SoT model (decision #55): the interface
    carrying primary_ip4; a bridge resolves to its single port (or the port
    flagged primary_member), a LAG to its single member (or the flagged one).
    -> (interface, chain of names). Raises InstallNicError on any ambiguity.
    Keep in step with jobs/lib/host_baseline.py derive_install_interface —
    tests/test_answer_service.py runs both over the same cases."""
    by_id = {i["id"]: i for i in interfaces}
    mine = []
    for iface_id in primary_ids or ():
        iface = by_id.get(str(iface_id))
        if iface is not None and iface not in mine:
            mine.append(iface)
    if not mine:
        raise InstallNicError(
            f"primary_ip4 {primary_address} is not assigned to any interface of {device_name} — "
            "assign it to the management bridge (or the mgmt port)"
        )
    if len(mine) > 1:
        raise InstallNicError(
            f"primary_ip4 {primary_address} is assigned to several interfaces of {device_name} "
            f"({', '.join(sorted(i['name'] for i in mine))}) — ambiguous install NIC; keep it on one"
        )
    iface = mine[0]
    chain = [iface["name"]]
    if iface["type"] == "bridge":
        iface = _pick_member(device_name, "bridge", iface,
                             [i for i in interfaces if i.get("bridge") == iface["id"]])
        chain.append(iface["name"])
    if iface["type"] == "lag":
        iface = _pick_member(device_name, "LAG", iface,
                             [i for i in interfaces if i.get("lag") == iface["id"]])
        chain.append(iface["name"])
    if iface["type"] in ("bridge", "lag"):
        raise InstallNicError(
            f"install NIC derivation for {device_name} reached {iface['name']} (type {iface['type']}) "
            f"via {' -> '.join(chain)} — only bridge -> LAG -> port nesting is supported"
        )
    return iface, chain


def install_interface(device: dict, interfaces: list | None = None):
    """(record, chain) of the SoT install NIC, or (None, []) without
    primary_ip4. An ambiguous model is a 409 refusal (logged REFUSED) — the
    install never lands on a guessed port."""
    primary = device.get("primary_ip4")
    if not primary:
        return None, []
    # exclude_m2m=false: Nautobot 3.x omits many-to-many fields (the
    # `interfaces` list included) from REST responses by default; 2.4 accepts
    # the parameter and returns them either way. Without it a pinned mgmt MAC
    # is invisible on 3.x and every static install would be refused.
    detail = _nb(
        "GET",
        f"/ipam/ip-addresses/{primary['id']}/",
        params={"depth": 1, "exclude_m2m": "false"},
    )
    ids = [_ref_id(a) for a in detail.get("interfaces", []) or [] if _ref_id(a)]
    records = [_iface_record(i) for i in (interfaces if interfaces is not None else device_interfaces(device))]
    try:
        return derive_install_interface(device["name"], primary.get("address"), ids, records)
    except InstallNicError as exc:
        log.warning("REFUSED: %s", exc)
        raise HTTPException(409, f"install NIC: {exc}")


def mgmt_interface_mac(device: dict, interfaces: list | None = None) -> str | None:
    """MAC of the SoT install NIC (derived through bridge/LAG), if recorded."""
    iface, _ = install_interface(device, interfaces)
    return (iface or {}).get("mac") or None


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


# PVE storage ids, LVM VG/LV names and ZFS pool names: a letter, then letters,
# digits, "_", "." or "-" (upper case allowed — the fleet storage is `DataDrive`).
_POOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


def filter_match_for(install: dict) -> str:
    """install.filter_match -> answer [disk-setup] filter-match ("" = installer default)."""
    value = str(install.get("filter_match") or "").strip().lower()
    if value not in ("", "any", "all"):
        raise HTTPException(500, f"profile install.filter_match must be any|all (got {value!r})")
    return value


def data_pool_spec(install: dict) -> dict | None:
    """Validate install.data_pool for the firstboot hook (SE455 V3-style
    JBOD boxes: the installer mirrors the boot pair, firstboot mirrors the
    data pair). Every value lands in a shell script, so only plain
    identifiers and integers pass — anything else is a 500, never rendered."""
    spec = install.get("data_pool")
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise HTTPException(500, "profile install.data_pool must be a mapping")
    name = str(spec.get("name") or "")
    storage = str(spec.get("pve_storage") or name)
    raid = str(spec.get("raid") or "mirror")
    select = str(spec.get("select") or "unused-largest")
    if not (_POOL_NAME_RE.match(name) and _POOL_NAME_RE.match(storage)):
        raise HTTPException(500, "profile install.data_pool name/pve_storage must be a plain identifier (letter first; letters, digits, _ . -)")
    if raid != "mirror":
        raise HTTPException(500, f"profile install.data_pool.raid: only 'mirror' is supported (got {raid!r})")
    if select != "unused-largest":
        raise HTTPException(500, f"profile install.data_pool.select: only 'unused-largest' is supported (got {select!r})")
    try:
        count = int(spec.get("count", 2))
        min_size_gib = int(spec.get("min_size_gib", 0))
    except (TypeError, ValueError):
        raise HTTPException(500, "profile install.data_pool count/min_size_gib must be integers")
    if count < 2 or min_size_gib < 0:
        raise HTTPException(500, "profile install.data_pool: count >= 2 and min_size_gib >= 0 required")
    return {
        "name": name,
        "pve_storage": storage,
        "raid": raid,
        "select": select,
        "count": count,
        "min_size_gib": min_size_gib,
    }


def data_volume_spec(install: dict) -> dict | None:
    """Validate install.data_volume for the firstboot hook (RAID-adapter boxes
    such as the SE455 V3: the adapter presents a data virtual drive; firstboot
    turns it into an LVM-thin PVE storage). Shell-literal rules as above."""
    spec = install.get("data_volume")
    if not spec:
        return None
    if install.get("data_pool"):
        raise HTTPException(500, "profile install: data_pool and data_volume are mutually exclusive")
    if not isinstance(spec, dict):
        raise HTTPException(500, "profile install.data_volume must be a mapping")
    vg = str(spec.get("vg") or "datastore")
    thinpool = str(spec.get("thinpool") or "data")
    storage = str(spec.get("pve_storage") or vg)
    select = str(spec.get("select") or "unused-largest")
    for value in (vg, thinpool, storage):
        if not _POOL_NAME_RE.match(value):
            raise HTTPException(500, "profile install.data_volume vg/thinpool/pve_storage must be plain identifiers (letter first; letters, digits, _ . -)")
    if select != "unused-largest":
        raise HTTPException(500, f"profile install.data_volume.select: only 'unused-largest' is supported (got {select!r})")
    try:
        min_size_gib = int(spec.get("min_size_gib", 0))
    except (TypeError, ValueError):
        raise HTTPException(500, "profile install.data_volume.min_size_gib must be an integer")
    if min_size_gib < 0:
        raise HTTPException(500, "profile install.data_volume.min_size_gib must be >= 0")
    return {
        "vg": vg,
        "thinpool": thinpool,
        "pve_storage": storage,
        "select": select,
        "min_size_gib": min_size_gib,
    }


def serial_console_spec(install: dict) -> dict | None:
    """Validate install.serial_console — the DeviceType declares that its
    hardware HAS a serial port and which unit (ttyS<unit>); the line
    parameters (speed, ...) are site facts from the config context."""
    spec = install.get("serial_console")
    if spec is None or spec is False:
        return None
    if not isinstance(spec, dict):
        raise HTTPException(500, "profile install.serial_console must be a mapping like {unit: 0}")
    extra = sorted(set(spec) - {"unit"})
    if extra:
        raise HTTPException(500, f"profile install.serial_console accepts only `unit` (got {extra}) — "
                                 "speed and framing come from the config context host_baseline.serial_console")
    unit = spec.get("unit")
    if isinstance(unit, bool) or not isinstance(unit, int) or not 0 <= unit <= 7:
        raise HTTPException(500, "profile install.serial_console.unit must be an integer 0-7 (ttyS<unit>)")
    return {"unit": unit, "tty": f"ttyS{unit}"}


# ---- host_baseline firstboot inputs (config context, decision #55) ----
# Packages the baseline itself depends on (the Host Baseline job configures
# snmpd; lldpd is the fleet's neighbour discovery). Keep in step with
# jobs/lib/host_baseline.py REQUIRED_PACKAGES.
REQUIRED_PACKAGES = ("lldpd", "snmpd")
_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$")
SERIAL_SPEEDS = (9600, 19200, 38400, 57600, 115200)
ARC_MIN_BYTES = 64 * 1024 * 1024  # ZFS refuses a smaller zfs_arc_max


class FirstbootInputError(ValueError):
    pass


def _serial_console(port: dict | None, sc) -> tuple[dict | None, str]:
    if port is None and sc is None:
        return None, "the DeviceType profile declares no serial port (install.serial_console) — skipped"
    if port is None:
        return None, ("the SoT sets host_baseline.serial_console but the DeviceType profile declares "
                      "no serial port (install.serial_console) — skipped")
    if sc is None:
        return None, (f"the DeviceType profile declares {port['tty']} but the SoT has no "
                      "host_baseline.serial_console (speed) — serial console NOT configured")
    if not isinstance(sc, dict):
        raise FirstbootInputError("host_baseline.serial_console must be a mapping like {speed: 115200}")
    extra = sorted(set(sc) - {"speed", "word", "parity", "stop"})
    if extra:
        raise FirstbootInputError(f"host_baseline.serial_console has unknown key(s) {extra}")
    speed, word = sc.get("speed"), sc.get("word", 8)
    parity, stop = sc.get("parity", "no"), sc.get("stop", 1)
    if isinstance(speed, bool) or speed not in SERIAL_SPEEDS:
        raise FirstbootInputError(f"host_baseline.serial_console.speed must be one of {list(SERIAL_SPEEDS)} "
                                  f"(got {speed!r})")
    if isinstance(word, bool) or word not in (5, 6, 7, 8):
        raise FirstbootInputError(f"host_baseline.serial_console.word must be 5-8 (got {word!r})")
    if parity not in ("no", "odd", "even"):
        raise FirstbootInputError(f"host_baseline.serial_console.parity must be no, odd or even (got {parity!r})")
    if isinstance(stop, bool) or stop not in (1, 2):
        raise FirstbootInputError(f"host_baseline.serial_console.stop must be 1 or 2 (got {stop!r})")
    tty, unit = port["tty"], port["unit"]
    return {
        "tty": tty,
        "unit": unit,
        "speed": speed,
        "console_arg": f"{tty},{speed}{parity[0]}{word}",
        "grub_command": f"serial --speed={speed} --unit={unit} --word={word} --parity={parity} --stop={stop}",
    }, f"{tty} at {speed} {word}{parity[0].upper()}{stop}"


def host_baseline_firstboot(device: dict, install: dict) -> dict:
    """The firstboot inputs from the Device's rendered config context
    (host_baseline) + the profile: packages, serial console, ZFS ARC limit,
    subscription-nag hook. Absent keys mean "not configured" (logged by
    firstboot); a malformed value is a 409 refusal at answer time — before
    the installer runs, like a broken profile key."""
    name = device.get("name")
    ctx = device.get("config_context")
    hb = ctx.get("host_baseline") if isinstance(ctx, dict) else None
    try:
        if hb is None:
            hb = {}
        if not isinstance(hb, dict):
            raise FirstbootInputError("config context host_baseline must be a mapping")
        packages = list(REQUIRED_PACKAGES)
        extra = hb.get("packages")
        if extra is not None:
            if not isinstance(extra, list):
                raise FirstbootInputError("host_baseline.packages must be a list of Debian package names")
            for pkg in extra:
                if not isinstance(pkg, str) or not _PKG_RE.match(pkg):
                    raise FirstbootInputError(f"host_baseline.packages: {pkg!r} is not a valid Debian package name")
                if pkg not in packages:
                    packages.append(pkg)
        serial, serial_note = _serial_console(serial_console_spec(install), hb.get("serial_console"))
        arc = hb.get("zfs_arc_max_bytes")
        if arc is not None and (isinstance(arc, bool) or not isinstance(arc, int) or arc < ARC_MIN_BYTES):
            raise FirstbootInputError(f"host_baseline.zfs_arc_max_bytes must be an integer >= {ARC_MIN_BYTES} "
                                      f"(64 MiB; got {arc!r})")
        nag = hb.get("remove_subscription_nag", False)
        if not isinstance(nag, bool):
            raise FirstbootInputError(f"host_baseline.remove_subscription_nag must be true or false (got {nag!r})")
    except FirstbootInputError as exc:
        log.warning("REFUSED: %s: %s (config context)", name, exc)
        raise HTTPException(409, f"config context: {exc}")
    return {
        "packages": packages,
        "serial": serial,
        "serial_note": serial_note,
        "zfs_arc_max_bytes": arc,
        "remove_nag": nag,
    }


def firstboot_summary(fb: dict) -> str:
    return (f"packages={','.join(fb['packages'])} serial={fb['serial_note'] if fb['serial'] else 'off'} "
            f"arc={fb['zfs_arc_max_bytes'] or 'default'} nag={'remove' if fb['remove_nag'] else 'keep'}")


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
    if not HOSTNAME_LABEL_RE.match(str(device.get("name") or "")):
        log.warning(
            "REFUSED: Device name %r (serial %s) is not a valid hostname label — "
            "letters, digits and hyphens only, 1-63 chars, no leading/trailing hyphen, "
            "not all digits; rename the Device", device.get("name"), serial,
        )
        raise HTTPException(409, "device name is not a valid hostname label (contract §4)")

    profile = load_profile((device.get("device_type") or {}).get("model", ""))
    install = profile.get("install", {})
    # Validate every profile key the firstboot step will need NOW: a broken
    # profile must refuse before the installer runs, not one boot later.
    filter_match = filter_match_for(install)
    data_pool_spec(install)
    data_volume_spec(install)
    firstboot_inputs = host_baseline_firstboot(device, install)
    pinning = bool(install.get("interface_name_pinning", False))
    interfaces = None

    # Network: static from the SoT when primary_ip4 exists, else DHCP.
    network_source = "from-dhcp"
    cidr = gateway = dns = ""
    net_filter: dict[str, str] = {}
    primary = device.get("primary_ip4")
    if primary and install.get("network_source", "from-answer") == "from-answer":
        cidr = primary["address"]
        gateway = default_gateway_for(primary) or ""
        if not gateway:
            log.warning(
                "REFUSED: %s has primary_ip4 %s but no DefaultGW-role IP in its prefix",
                device["name"], cidr,
            )
            raise HTTPException(409, "no DefaultGW-role IP in the management prefix (contract §3)")
        dns = DNS_SERVER or gateway
        network_source = "from-answer"
        # Static installs need the exact mgmt NIC: never guess one from the
        # installer's list (its "link" field is the interface NAME, not a
        # carrier state, so nothing in the POST says which port is cabled).
        # Derived through the SoT model (decision #55): primary_ip4's
        # interface; a bridge -> its port, a LAG -> its primary member.
        interfaces = device_interfaces(device)
        nic, chain = install_interface(device, interfaces)
        mac = ((nic or {}).get("mac") or "").lower()
        if not mac and len(chain) > 1:
            log.warning(
                "REFUSED: %s installs static (%s) but its install NIC %s (via %s) has no MAC — "
                "record the port's MAC on its Nautobot interface", device["name"], cidr,
                nic["name"], " -> ".join(chain),
            )
            raise HTTPException(409, "static install needs the install port's MAC (derived through the "
                                     "management bridge/LAG) recorded in Nautobot (contract §4)")
        if not mac:
            log.warning(
                "REFUSED: %s installs static (%s) but the interface carrying primary_ip4 "
                "has no MAC — pin the mgmt interface MAC in Nautobot", device["name"], cidr,
            )
            raise HTTPException(409, "static install needs the mgmt interface MAC pinned (contract §4)")
        reported = sorted({str(n.get("mac") or "").lower() for n in nics if n.get("mac")})
        if mac not in reported:
            log.warning(
                "REFUSED: %s pinned mgmt MAC %s is not among the NICs the installer reported "
                "(%s) — fix the MAC on the mgmt interface", device["name"], mac,
                ", ".join(reported) or "none",
            )
            raise HTTPException(409, "pinned mgmt MAC is not present on this machine")
        net_filter["ID_NET_NAME_MAC"] = f"*{mac.replace(':', '')}"

    # Interface name pinning (PVE >= 9.1 answer format, decision #51): every
    # physical NIC gets a MAC-pinned name at install time; SoT interface names
    # apply where the Device records the MAC, the rest default to nic<N>.
    if pinning and interfaces is None:
        interfaces = device_interfaces(device)
    pin_mapping = build_pin_mapping(interfaces, nics, device["name"]) if pinning else {}

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
        interface_name_pinning=pinning,
        pin_mapping=pin_mapping,
        filesystem=filesystem,
        disk_filter=install.get("disk_filter", {}),
        filter_match=filter_match,
        fs_family=fs_family,
        fs_options=install.get(fs_family, {}),
        firstboot_url=f"{PUBLIC_URL}/firstboot?serial={serial}&key={firstboot_key}",
        cert_fingerprint=CERT_FINGERPRINT,
        webhook_url=f"{PUBLIC_URL}/webhook?serial={serial}&key={webhook_key}",
    )
    log.info(
        "ANSWERED: %s (serial %s) source=%s fs=%s pinning=%s%s firstboot: %s",
        device["name"], serial, network_source, filesystem, pinning,
        f" names={pin_mapping}" if pin_mapping else "", firstboot_summary(firstboot_inputs),
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
    # Storage-layout policy rides the same profile the answer came from
    # (install.data_pool -> the ZFS data-mirror step in the script).
    profile = load_profile((device.get("device_type") or {}).get("model", ""))
    data_pool = data_pool_spec(profile.get("install", {}))
    data_volume = data_volume_spec(profile.get("install", {}))
    firstboot_inputs = host_baseline_firstboot(device, profile.get("install", {}))
    cred_key = issue_key(serial, "credentials")
    rendered = TEMPLATES.get_template("firstboot.sh.j2").render(
        node_name=device["name"],
        data_pool=data_pool,
        data_volume=data_volume,
        serial=serial,
        service_url=PUBLIC_URL,
        cert_fingerprint=CERT_FINGERPRINT,
        credentials_key=cred_key,
        pve_role=PVE_ROLE_NAME,
        pve_privs=PVE_ROLE_PRIVS,
        pve_user=PVE_SERVICE_USER,
        pve_token=PVE_TOKEN_NAME,
        packages=firstboot_inputs["packages"],
        serial_console=firstboot_inputs["serial"],
        serial_console_note=firstboot_inputs["serial_note"],
        zfs_arc_max_bytes=firstboot_inputs["zfs_arc_max_bytes"],
        remove_nag=firstboot_inputs["remove_nag"],
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


def node_token_names(device_name: str, account: str = "proxmox") -> dict:
    """Names of a node's API-token Secrets: SecretsGroup <node>-<account>,
    text-file Secrets <slug>-<account>-token-username / -secret over the files
    <slug>_<account>_token_id / _secret. The Host Baseline job stores its
    service-account tokens with the same scheme (jobs/lib/host_baseline.py
    node_token_names — tests/test_answer_service.py keeps the two equal)."""
    slug = slugify(device_name)
    return {
        "group": f"{device_name}-{account}",
        "secret_username": f"{slug}-{account}-token-username",
        "secret_secret": f"{slug}-{account}-token-secret",
        "file_id": f"{slug}_{account}_token_id",
        "file_secret": f"{slug}_{account}_token_secret",
    }


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
    names = node_token_names(name)
    if (device.get("custom_fields") or {}).get("secrets_group"):
        # Reinstall path (designed): a fresh install re-runs the bootstrap and
        # the old token died with the old OS — overwriting is correct, but say so.
        log.warning("OVERWRITING stored credentials for %s (reinstall)", name)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    id_file = SECRETS_DIR / names["file_id"]
    secret_file = SECRETS_DIR / names["file_secret"]
    _write_secret_file(id_file, token_id + "\n", 0o640)
    _write_secret_file(secret_file, token_secret + "\n", 0o640)

    group_name = names["group"]
    secret_ids = {}
    for kind, path in (("username", id_file), ("secret", secret_file)):
        secret_name = names[f"secret_{kind}"]
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
    # The installed node's final NIC names (pinned or not) — the record an
    # operator needs when a Nautobot interface has to be matched to a port.
    names = [
        (n.get("name"), n.get("mac"), "mgmt" if n.get("is-management") else "")
        for n in (body.get("network-interfaces") or []) if isinstance(n, dict)
    ]
    if names:
        log.info("INSTALLED %s: interfaces %s", device["name"],
                 ", ".join(f"{n}={m}{' (' + tag + ')' if tag else ''}" for n, m, tag in names))
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


# ---- media forge: prepare installer media against THIS service's identity ----
# (admin surface — see the config block; everything here is inert unless
# ADMIN_ENABLED. The point of preparing media HERE: the URL and cert
# fingerprint are injected from this process's own runtime identity, so
# mismatched media is structurally impossible.)

import shutil
import subprocess
import uuid

_prepare_tasks: dict[str, dict] = {}
_prepare_lock = threading.Lock()
PREPARE_TOOL = "proxmox-auto-install-assistant"


def _check_admin(authorization: str | None) -> None:
    if not ADMIN_ENABLED:
        raise HTTPException(404, "not found")  # surface hidden when disabled
    if not ADMIN_TOKEN or authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401, "bad or missing admin token")


@app.get("/info")
def info() -> dict:
    """Read-only identity — media MUST be prepared against these values, which
    is exactly what /admin/prepare guarantees by injecting them itself."""
    return {
        "public_url": PUBLIC_URL,
        "cert_fingerprint": CERT_FINGERPRINT,
        "nfv_role": NFV_ROLE,
        "admin_enabled": ADMIN_ENABLED,
        # Version handshake (decision #56): what this build is, and the oldest
        # jobs it accepts. The jobs compare both against their own constants
        # (jobs/lib/version.py) and refuse before any BMC/forge action.
        "version": SERVICE_VERSION,
        "min_jobs_version": MIN_JOBS_VERSION,
        # Baked-in install profiles and the profile keys this build understands.
        # The install job's preflight reads these before it touches a BMC:
        # profiles bake in at build time while the jobs arrive through the Git
        # sync, so the two drift after every profile merge (seen live on the
        # first SE455 V3 run — 403 "no install profile" after a full boot).
        "profiles": sorted(p.stem for p in PROFILE_DIR.glob("*.yaml")) if PROFILE_DIR.is_dir() else [],
        "profile_features": ["filter_match", "data_pool", "data_volume", "interface_name_pinning",
                             "serial_console"],
        # Config-context (host_baseline) inputs firstboot renders — a build
        # without this key silently ignores them.
        "firstboot_features": ["packages", "serial_console", "zfs_arc_max_bytes",
                               "remove_subscription_nag"],
    }


def _tlog(tid: str, message: str) -> None:
    log.info("[prepare %s] %s", tid[:8], message)
    with _prepare_lock:
        task = _prepare_tasks.get(tid)
        if task is not None:
            task["progress"].append(message)


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_iso(tid: str, url: str, expected_sha: str | None, dest: Path) -> None:
    if dest.exists() and expected_sha and _sha256_file(dest) == expected_sha:
        _tlog(tid, f"stock ISO already cached and checksum-verified: {dest.name}")
        return
    _tlog(tid, f"downloading {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        done = 0
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                fh.write(chunk)
                done += len(chunk)
                if done % (200 * 1024 * 1024) < 1024 * 1024:
                    _tlog(tid, f"  ... {done // (1024 * 1024)} MiB")
    if expected_sha:
        seen = _sha256_file(tmp)
        if seen != expected_sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"stock ISO checksum mismatch: {seen} != {expected_sha}")
        _tlog(tid, "stock ISO checksum verified")
    os.replace(tmp, dest)


def _prepare_task(tid: str, release: str | None, iso_url: str | None,
                  iso_sha256: str | None, pxe: bool, version: str | None) -> None:
    try:
        iso_name = (iso_url.rsplit("/", 1)[-1] if iso_url else f"proxmox-ve_{release}.iso")
        src_url = iso_url or f"{PVE_ISO_BASE_URL}/{iso_name}"
        expected = iso_sha256
        if not expected and not iso_url:
            # Official base URL: the published SHA256SUMS file is authoritative.
            sums = requests.get(f"{PVE_ISO_BASE_URL}/SHA256SUMS", timeout=30)
            sums.raise_for_status()
            for line in sums.text.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].lstrip("*") == iso_name:
                    expected = parts[0]
                    break
            if not expected:
                raise RuntimeError(f"{iso_name} not found in SHA256SUMS — bad release string?")
        if not expected:
            raise RuntimeError("custom iso_url requires iso_sha256 (fail-closed on integrity)")

        # Version string decides the ARTIFACT NAME: one file per version, so a
        # new prepare can never overwrite the artifact an existing (possibly
        # Active) version's checksum points at. Fail fast on a collision
        # BEFORE the heavy download/prepare work when auto-registration is on.
        version_str = version or (f"{release}-auto" if release else f"{iso_name[:-4]}-auto")
        if FIRMWARE_BASE_URL and NAUTOBOT_URL and NAUTOBOT_TOKEN:
            plats = _nb("GET", "/dcim/platforms/", params={"name": "proxmox-ve"}).get("results", [])
            if plats and _nb("GET", "/dcim/software-versions/",
                             params={"version": version_str, "platform": plats[0]["id"]}
                             ).get("results", []):
                raise RuntimeError(
                    f"SoftwareVersion {version_str!r} already exists — refusing to "
                    "re-point it; re-run with an explicit new version")

        cached = DATA_DIR / "iso-cache" / iso_name
        _download_iso(tid, src_url, expected, cached)

        outdir = DATA_DIR / "prepared" / tid
        outdir.mkdir(parents=True, exist_ok=True)
        out_iso = outdir / f"proxmox-ve_{version_str}.iso"
        cmd = [PREPARE_TOOL, "prepare-iso", str(cached),
               "--fetch-from", "http", "--url", f"{PUBLIC_URL}/answer",
               "--output", str(out_iso)]
        if CERT_FINGERPRINT:
            cmd += ["--cert-fingerprint", CERT_FINGERPRINT]
        if ANSWER_AUTH_TOKEN:
            cmd += ["--answer-auth-token", ANSWER_AUTH_TOKEN]
        _tlog(tid, f"preparing ISO against {PUBLIC_URL}/answer "
                   f"(fingerprint {'pinned' if CERT_FINGERPRINT else 'NOT pinned'})")
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        # The tool can print an error yet exit 0 (seen live: missing xorriso)
        # — the artifact existing is the real success signal.
        if run.returncode != 0 or not out_iso.exists():
            raise RuntimeError(
                f"prepare-iso failed (rc={run.returncode}, artifact "
                f"{'missing' if not out_iso.exists() else 'present'}): "
                f"{(run.stderr or run.stdout)[-400:]}"
            )
        artifacts = [out_iso]

        if pxe:
            pxe_dir = outdir / "pxe"
            pxe_dir.mkdir(exist_ok=True)
            _tlog(tid, "preparing PXE/iPXE artifact set")
            run = subprocess.run(
                [PREPARE_TOOL, "prepare-iso", str(cached),
                 "--fetch-from", "http", "--url", f"{PUBLIC_URL}/answer",
                 *(["--cert-fingerprint", CERT_FINGERPRINT] if CERT_FINGERPRINT else []),
                 *(["--answer-auth-token", ANSWER_AUTH_TOKEN] if ANSWER_AUTH_TOKEN else []),
                 "--pxe", "--pxe-loader", "ipxe", "--output", str(pxe_dir)],
                capture_output=True, text=True, timeout=1200)
            pxe_files = sorted(p for p in pxe_dir.iterdir() if p.is_file())
            if run.returncode != 0 or not pxe_files:
                raise RuntimeError(
                    f"prepare-iso --pxe failed (rc={run.returncode}, "
                    f"{len(pxe_files)} artifacts): {(run.stderr or run.stdout)[-400:]}"
                )
            artifacts += pxe_files

        files = []
        for path in artifacts:
            sha = _sha256_file(path)
            files.append({"name": path.name, "sha256": sha, "size": path.stat().st_size,
                          "pxe": path.parent.name == "pxe", "local_path": str(path)})
            _tlog(tid, f"artifact {path.name}: sha256={sha}")

        published = False
        if FIRMWARE_PUBLISH_DIR:
            pub_root = Path(FIRMWARE_PUBLISH_DIR)
            for entry, path in zip(files, artifacts):
                # PXE artifacts keep tool-given names -> per-version subdir so
                # versions never collide there either.
                rel = f"pxe/{version_str}/{path.name}" if entry["pxe"] else path.name
                target = pub_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                (target.parent / f"{path.name}.sha256").write_text(
                    f"{entry['sha256']}  {path.name}\n")
                if FIRMWARE_BASE_URL:
                    entry["download_url"] = f"{FIRMWARE_BASE_URL}/{rel}"
            published = True
            _tlog(tid, f"published {len(files)} artifact(s) to {FIRMWARE_PUBLISH_DIR}")

        registered, register_note = False, ""
        if published and FIRMWARE_BASE_URL and NAUTOBOT_URL and NAUTOBOT_TOKEN:
            try:
                registered, register_note = _register_prepared(
                    tid, version_str, files[0])
            except Exception as exc:  # registration failure must not lose the prepare
                register_note = f"registration failed: {exc}"
                _tlog(tid, register_note)
        elif not published:
            register_note = "not published (FIRMWARE_PUBLISH_DIR unset) — artifacts left in /data"
        else:
            register_note = "auto-registration disabled (FIRMWARE_BASE_URL or Nautobot creds unset)"

        with _prepare_lock:
            _prepare_tasks[tid].update(state="success", result={
                "files": [{k: v for k, v in f.items() if k != "local_path"} or f for f in files],
                "published": published,
                "registered": registered,
                "register_note": register_note,
                "answer_url": f"{PUBLIC_URL}/answer",
                "cert_fingerprint": CERT_FINGERPRINT,
            })
        _tlog(tid, "prepare complete")
    except Exception as exc:
        log.error("[prepare %s] FAILED: %s", tid[:8], exc)
        with _prepare_lock:
            _prepare_tasks[tid].update(state="error", error=str(exc))


def _register_prepared(tid: str, version_str: str, iso_entry: dict) -> tuple[bool, str]:
    """SoftwareVersion (Staged) + ImageFile for the prepared ISO. Fail-closed
    on version collision: an existing version is NEVER silently re-pointed at
    a new artifact (it may be Active and in devices' intent)."""
    plats = _nb("GET", "/dcim/platforms/", params={"name": "proxmox-ve"}).get("results", [])
    if not plats:
        return False, "platform proxmox-ve missing — run Bootstrap NFV Data Model first"
    existing = _nb("GET", "/dcim/software-versions/",
                   params={"version": version_str, "platform": plats[0]["id"]}).get("results", [])
    if existing:
        return False, (f"SoftwareVersion {version_str!r} already exists — refusing to "
                       "re-point it; re-run with an explicit new version")
    staged = _nb("GET", "/extras/statuses/", params={"name": "Staged"})["results"][0]
    active = _nb("GET", "/extras/statuses/", params={"name": "Active"})["results"][0]
    sv = _nb("POST", "/dcim/software-versions/", json={
        "platform": plats[0]["id"], "version": version_str, "status": staged["id"]})
    _nb("POST", "/dcim/software-image-files/", json={
        "software_version": sv["id"], "image_file_name": iso_entry["name"],
        "image_file_checksum": iso_entry["sha256"], "hashing_algorithm": "sha256",
        "image_file_size": iso_entry["size"], "download_url": iso_entry["download_url"],
        "default_image": True, "status": active["id"]})
    _tlog(tid, f"registered SoftwareVersion {version_str} (Staged) + ImageFile "
               f"{iso_entry['name']} — promote to Active in the lab, then validate one install")
    return True, f"SoftwareVersion {version_str} registered as Staged"


@app.post("/admin/prepare")
async def admin_prepare(request: Request, authorization: str | None = Header(default=None)) -> dict:
    _check_admin(authorization)
    body = await request.json()
    release = (body.get("release") or "").strip() or None
    iso_url = (body.get("iso_url") or "").strip() or None
    if release and not re.fullmatch(r"[0-9]+\.[0-9]+-[0-9]+", release):
        raise HTTPException(400, "release must look like 9.2-1")
    version = (body.get("version") or "").strip()
    if version and not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", version):
        raise HTTPException(400, "version may only contain letters, digits, . _ -")
    if iso_url and not iso_url.startswith(("http://", "https://")):
        raise HTTPException(400, "iso_url must be http(s)")
    if not (release or iso_url):
        raise HTTPException(400, "provide release or iso_url")
    tid = uuid.uuid4().hex
    with _prepare_lock:
        _prepare_tasks[tid] = {"state": "running", "progress": [], "result": None, "error": None}
    threading.Thread(
        target=_prepare_task,
        args=(tid, release, iso_url, (body.get("iso_sha256") or "").strip() or None,
              bool(body.get("pxe")), version or None),
        daemon=True,
    ).start()
    log.info("prepare task %s started (release=%s iso_url=%s pxe=%s)",
             tid[:8], release, iso_url, bool(body.get("pxe")))
    return {"task": tid}


@app.get("/admin/prepare/{task_id}")
def admin_prepare_status(task_id: str, authorization: str | None = Header(default=None)) -> dict:
    _check_admin(authorization)
    with _prepare_lock:
        task = _prepare_tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "unknown task (tasks do not survive restarts)")
        return {"state": task["state"], "progress": list(task["progress"]),
                "result": task["result"], "error": task["error"]}
