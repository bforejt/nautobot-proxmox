"""
Host baseline (L1/L2) — the pure logic of the `Host Baseline (SoT-driven)`
job (decisions #54/#55; design: docs/host-baseline.md; contract:
docs/sot-data-contract.md §4c).

The SoT is the repository of facts. Site and fleet settings come from the
device's rendered config context (top-level key ``host_baseline``), the bond
and bridge topology from its Nautobot interfaces (native ``type`` lag/bridge,
``Interface.lag`` / ``Interface.bridge`` membership, ``mtu``, ``mode``,
``mac_address``) plus three interface custom fields, and every credential
from a Nautobot Secret. This module carries mechanics only — validation (a
missing or ambiguous fact is a named refusal before anything touches the
node), rendering, planning and parsing — on plain dicts, so it imports by
file path and is unit-tested without Nautobot or SSH
(tests/test_host_baseline.py). The job converts ORM objects into these dicts
and ships the results to the on-node applier (host_baseline_applier.sh, next
to this file) over the SSH session's stdin.

Secrets never enter a string this module returns for logging: pveum/pvesh
argv lists carry none (the AD bind password goes to PVE's realm credential
file, SNMPv3 passphrases to snmpd's persistent file — both written on the
node by bash builtins from the payload's locals), and `scrub()` is the job's
last line of defence on everything it logs.
"""

import difflib
import hashlib
import ipaddress
import json
import re
import shlex
from pathlib import Path

CONTEXT_KEY = "host_baseline"

# Interface custom fields (bootstrap-created on dcim.interface, contract §4c).
CF_LAG_MODE = "lag_mode"
CF_LAG_XMIT_HASH = "lag_xmit_hash"
CF_PRIMARY_MEMBER = "primary_member"

# Linux bonding modes / transmit hash policies — the bootstrap seeds the two
# select custom fields with exactly these choices (code<->data handshake).
LAG_MODES = ("balance-rr", "active-backup", "balance-xor", "broadcast", "802.3ad",
             "balance-tlb", "balance-alb")
XMIT_HASH_POLICIES = ("layer2", "layer2+3", "layer3+4", "encap2+3", "encap3+4", "vlan+srcmac")
_XMIT_HASH_MODES = {"balance-xor", "802.3ad", "balance-tlb", "balance-alb"}
_XMIT_HASH_REQUIRED = {"balance-xor", "802.3ad"}
_PRIMARY_MODES = {"active-backup", "balance-tlb", "balance-alb"}
LACP_RATES = ("slow", "fast")

# Packages the baseline itself depends on (the SNMP step configures snmpd;
# lldpd is the fleet's neighbour discovery). Firstboot installs them and the
# job ensures them; host_baseline.packages adds to them. Keep in step with
# bmc/answer_service/app.py REQUIRED_PACKAGES.
REQUIRED_PACKAGES = ("lldpd", "snmpd")

# provisioning_state gates (contract §4c state table).
APPLY_STATES = ("bm_installed", "baseline_done")
DRY_RUN_STATES = ("bm_installed", "baseline_done", "fabric_done", "vms_deployed", "handed_off")
DONE_STATE = "baseline_done"

# Secret record names (conventions; the config context may name others).
DEFAULT_AD_BIND_SECRET = "ad_bind_password"
DEFAULT_SNMP_COMMUNITY_SECRET = "snmp_community"
HOST_SSH_USERNAME_SECRET = "host_ssh_username"
HOST_SSH_PASSWORD_SECRET = "host_ssh_password"

# snmpd.conf mechanics (the tester's field-verified layout): the views the
# rendered file defines, and the SNMPv3 protocols net-snmp's createUser takes.
SNMP_VIEWS = ("systemonly",)
SNMP_AUTH_PROTOCOLS = ("SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512")
SNMP_PRIV_PROTOCOLS = ("AES", "AES-192", "AES-256")
DEFAULT_SNMP_AUTH = "SHA"
DEFAULT_SNMP_PRIV = "AES"

# Network apply: the rollback timer restores the previous file unless the
# job reconnects on the management IP and cancels it first.
DEFAULT_ROLLBACK_SECONDS = 180
ROLLBACK_MIN, ROLLBACK_MAX = 60, 900

# Identities the job never manages: root, and the firstboot-created deploy
# account whose token lives in <node>-proxmox (answer-service phone-home).
RESERVED_ACCOUNT_USERS = ("root@pam", "svc-nfv@pve")
RESERVED_ACCOUNT_NAMES = ("proxmox",)

APPLIER_PATH = Path(__file__).resolve().with_name("host_baseline_applier.sh")
EVENT_PREFIX = "@@NFV@@ "
MANAGED_COMMENT = "managed by the Nautobot Host Baseline job"


class BaselineRefusal(RuntimeError):
    """A required SoT fact is missing/ambiguous, or the node is not the one
    the SoT describes — refused before anything is written (fail closed)."""

    def __init__(self, problems):
        self.problems = [str(p) for p in problems]
        super().__init__(format_refusal(self.problems))


def format_refusal(problems):
    problems = list(problems)
    if len(problems) == 1:
        return f"Host baseline refused: {problems[0]}"
    lines = "\n".join(f"  ({n}) {p}" for n, p in enumerate(problems, 1))
    return f"Host baseline refused — {len(problems)} problems:\n{lines}"


# ---------------------------------------------------------------- validators

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REALM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,31}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]{1,253}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_PVE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")          # roles, groups
_TOKEN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,63}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,63}$")
_SCHEDULE_RE = re.compile(r"^[A-Za-z0-9 :*,./~-]{1,64}$")
_ACL_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)?$")
_USERID_RE = re.compile(r"^([^\s@:!/]{1,60})@(pam|pve)$")
_ACCOUNT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_SNMP_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_SYNC_ATTR_RE = re.compile(r"^\w+=[^,]+(,\s*\w+=[^,]+)*$")
_SYNC_OPTS_RE = re.compile(r"^[A-Za-z0-9=;,_-]{1,200}$")
_REMOVE_VANISHED_RE = re.compile(r"^(none|(acl|entry|properties)(;(acl|entry|properties))*)$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")


def _text(value, path, problems, required=True, max_len=255):
    if value is None or value == "":
        if required:
            problems.append(f"{path} is missing")
        return None
    if not isinstance(value, str):
        problems.append(f"{path} must be a string (got {type(value).__name__})")
        return None
    if _CONTROL_RE.search(value):
        problems.append(f"{path} contains a control character (newline, tab, ...) — not allowed")
        return None
    if len(value) > max_len:
        problems.append(f"{path} is longer than {max_len} characters")
        return None
    return value


def _match(value, path, problems, regex, what, required=True):
    value = _text(value, path, problems, required=required)
    if value is not None and not regex.match(value):
        problems.append(f"{path} {value!r} is not {what}")
        return None
    return value


def _bool(value, path, problems, default=None):
    if value is None:
        return default
    if not isinstance(value, bool):
        problems.append(f"{path} must be true or false (got {value!r})")
        return default
    return value


def _int(value, path, problems, lo, hi, required=False):
    if value is None:
        if required:
            problems.append(f"{path} is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{path} must be an integer (got {value!r})")
        return None
    if not lo <= value <= hi:
        problems.append(f"{path} must be between {lo} and {hi} (got {value})")
        return None
    return value


def _section(hb, key, problems):
    """A step's section: None = explicitly disabled ({enabled: false});
    a dict = enabled; missing = refusal (fail closed — no silent skip)."""
    raw = hb.get(key)
    path = f"{CONTEXT_KEY}.{key}"
    if raw is None:
        problems.append(
            f"{path} is missing — set it (contract §4c) or set `{key}: {{enabled: false}}` "
            "to skip that step on purpose"
        )
        return None, False
    if not isinstance(raw, dict):
        problems.append(f"{path} must be a mapping")
        return None, False
    enabled = _bool(raw.get("enabled"), f"{path}.enabled", problems, default=True)
    return (raw if enabled else None), True


def validate_packages(value, problems, path=f"{CONTEXT_KEY}.packages"):
    """REQUIRED_PACKAGES + the SoT's extras (deduplicated, order kept)."""
    out = list(REQUIRED_PACKAGES)
    if value is None:
        return out
    if not isinstance(value, list):
        problems.append(f"{path} must be a list of Debian package names")
        return out
    for item in value:
        if not isinstance(item, str) or not _PKG_RE.match(item):
            problems.append(f"{path}: {item!r} is not a valid Debian package name")
            continue
        if item not in out:
            out.append(item)
    return out


def _validate_snmp(raw, problems):
    path = f"{CONTEXT_KEY}.snmp"
    cfg = {
        "contact": _text(raw.get("contact"), f"{path}.contact", problems),
        "community_secret": _match(raw.get("community_secret"), f"{path}.community_secret",
                                   problems, _SECRET_NAME_RE, "a Secret name", required=False),
        "community_source": None,
        "community_view": None,
        "v3_users": [],
    }
    source = raw.get("community_source")
    if source is not None:
        try:
            cfg["community_source"] = str(ipaddress.ip_network(str(source), strict=False))
        except ValueError:
            problems.append(f"{path}.community_source {source!r} is not an IP network (CIDR)")
    view = raw.get("community_view")
    if view is not None:
        if view not in SNMP_VIEWS:
            problems.append(f"{path}.community_view {view!r} is not a view the rendered snmpd.conf "
                            f"defines ({', '.join(SNMP_VIEWS)})")
        else:
            cfg["community_view"] = view
    users = raw.get("v3_users")
    if users is not None and not isinstance(users, list):
        problems.append(f"{path}.v3_users must be a list")
        users = []
    seen = set()
    for n, entry in enumerate(users or []):
        upath = f"{path}.v3_users[{n}]"
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            problems.append(f"{upath} must be a user name or a mapping")
            continue
        name = _match(entry.get("name"), f"{upath}.name", problems, _SNMP_USER_RE,
                      "an SNMPv3 user name (letters, digits, _ . -, max 32)")
        if name is None:
            continue
        if name in seen:
            problems.append(f"{upath}.name {name!r} is listed twice")
            continue
        seen.add(name)
        auth = entry.get("auth_protocol", DEFAULT_SNMP_AUTH)
        priv = entry.get("priv_protocol", DEFAULT_SNMP_PRIV)
        if auth not in SNMP_AUTH_PROTOCOLS:
            problems.append(f"{upath}.auth_protocol {auth!r} is not one of {', '.join(SNMP_AUTH_PROTOCOLS)}")
        if priv not in SNMP_PRIV_PROTOCOLS:
            problems.append(f"{upath}.priv_protocol {priv!r} is not one of {', '.join(SNMP_PRIV_PROTOCOLS)}")
        uview = entry.get("view")
        if uview is not None and uview not in SNMP_VIEWS:
            problems.append(f"{upath}.view {uview!r} is not a view the rendered snmpd.conf defines "
                            f"({', '.join(SNMP_VIEWS)})")
        cfg["v3_users"].append({
            "name": name,
            "auth_protocol": auth,
            "priv_protocol": priv,
            "view": uview,
            "auth_secret": _match(entry.get("auth_secret", f"snmpv3_{name}_auth"), f"{upath}.auth_secret",
                                  problems, _SECRET_NAME_RE, "a Secret name"),
            "priv_secret": _match(entry.get("priv_secret", f"snmpv3_{name}_priv"), f"{upath}.priv_secret",
                                  problems, _SECRET_NAME_RE, "a Secret name"),
        })
    if not cfg["community_secret"] and not cfg["v3_users"]:
        problems.append(f"{path} grants no access — set community_secret and/or v3_users "
                        "(or `snmp: {enabled: false}`)")
    return cfg


def _validate_ad(raw, problems):
    path = f"{CONTEXT_KEY}.ad"
    cfg = {
        "realm": _match(raw.get("realm"), f"{path}.realm", problems, _REALM_RE,
                        "a PVE realm id (letter first; letters, digits, . _ -; 2-32 chars)"),
        "domain": _match(raw.get("domain"), f"{path}.domain", problems, _DOMAIN_RE, "a DNS domain"),
        "mode": raw.get("mode"),
        "port": _int(raw.get("port"), f"{path}.port", problems, 1, 65535),
        "verify": _bool(raw.get("verify"), f"{path}.verify", problems),
        "base_dn": _text(raw.get("base_dn"), f"{path}.base_dn", problems, max_len=1024),
        "bind_dn": _text(raw.get("bind_dn"), f"{path}.bind_dn", problems, max_len=1024),
        "bind_password_secret": _match(raw.get("bind_password_secret", DEFAULT_AD_BIND_SECRET),
                                       f"{path}.bind_password_secret", problems, _SECRET_NAME_RE,
                                       "a Secret name"),
        "user_filter": _text(raw.get("user_filter"), f"{path}.user_filter", problems,
                             required=False, max_len=2048),
        "group_filter": _text(raw.get("group_filter"), f"{path}.group_filter", problems,
                              required=False, max_len=2048),
        "sync_attributes": _match(raw.get("sync_attributes"), f"{path}.sync_attributes", problems,
                                  _SYNC_ATTR_RE, "a PVE sync_attributes list (e.g. email=mail)",
                                  required=False),
        "sync_defaults_options": _match(raw.get("sync_defaults_options"),
                                        f"{path}.sync_defaults_options", problems, _SYNC_OPTS_RE,
                                        "a PVE sync-defaults-options string", required=False),
        "case_sensitive": _bool(raw.get("case_sensitive"), f"{path}.case_sensitive", problems),
        "comment": _text(raw.get("comment"), f"{path}.comment", problems, required=False),
        # Decision #8/#54: AD is the fleet's default login realm.
        "default_realm": _bool(raw.get("default_realm"), f"{path}.default_realm", problems,
                               default=True),
        "admin_group": _match(raw.get("admin_group"), f"{path}.admin_group", problems, _PVE_ID_RE,
                              "a PVE group id"),
        "admin_role": _match(raw.get("admin_role"), f"{path}.admin_role", problems, _PVE_ID_RE,
                             "a PVE role id"),
        "admin_path": _match(raw.get("admin_path", "/"), f"{path}.admin_path", problems,
                             _ACL_PATH_RE, "an ACL path"),
        "sync_job": None,
    }
    if cfg["mode"] not in ("ldap", "ldaps", "ldap+starttls"):
        problems.append(f"{path}.mode must be ldap, ldaps or ldap+starttls (got {cfg['mode']!r})")
        cfg["mode"] = None
    if cfg["realm"] and cfg["realm"].lower() in ("pam", "pve"):
        problems.append(f"{path}.realm {cfg['realm']!r} is a built-in PVE realm")
    servers = raw.get("servers")
    if not isinstance(servers, list) or not 1 <= len(servers) <= 2:
        problems.append(f"{path}.servers must list one or two AD servers")
        cfg["servers"] = []
    else:
        cfg["servers"] = [s for n, s in enumerate(servers)
                          if _match(s, f"{path}.servers[{n}]", problems, _HOST_RE,
                                    "a host name or IP address")]
    job = raw.get("sync_job")
    if not isinstance(job, dict):
        problems.append(f"{path}.sync_job is missing — name + schedule of the realm-sync job")
    else:
        jpath = f"{path}.sync_job"
        scope = job.get("scope", "both")
        if scope not in ("users", "groups", "both"):
            problems.append(f"{jpath}.scope must be users, groups or both (got {scope!r})")
        cfg["sync_job"] = {
            "name": _match(job.get("name"), f"{jpath}.name", problems, _JOB_ID_RE,
                           "a job id (letter first; letters, digits, _ -)"),
            "schedule": _match(job.get("schedule"), f"{jpath}.schedule", problems, _SCHEDULE_RE,
                               "a systemd calendar event (e.g. '*-*-* 06:00:00')"),
            "scope": scope,
            "enable_new": _bool(job.get("enable_new"), f"{jpath}.enable_new", problems),
            "remove_vanished": _match(job.get("remove_vanished"), f"{jpath}.remove_vanished",
                                      problems, _REMOVE_VANISHED_RE,
                                      "none or a ;-list of acl, entry, properties", required=False),
        }
    return cfg


def _validate_accounts(raw, problems):
    path = f"{CONTEXT_KEY}.service_accounts"
    if raw is None:
        problems.append(f"{path} is missing — list the service accounts (user, token, role, path, "
                        "privsep), or [] for none")
        return []
    if not isinstance(raw, list):
        problems.append(f"{path} must be a list")
        return []
    out, users, names = [], set(), set()
    for n, entry in enumerate(raw):
        apath = f"{path}[{n}]"
        if not isinstance(entry, dict):
            problems.append(f"{apath} must be a mapping")
            continue
        user = _match(entry.get("user"), f"{apath}.user", problems, _USERID_RE,
                      "a PVE user id in the pam or pve realm (e.g. datadog@pam)")
        token = _match(entry.get("token"), f"{apath}.token", problems, _TOKEN_ID_RE,
                       "a PVE token id (letter first; letters, digits, . _ -)")
        role = _match(entry.get("role"), f"{apath}.role", problems, _PVE_ID_RE, "a PVE role id")
        acl_path = _match(entry.get("path", "/"), f"{apath}.path", problems, _ACL_PATH_RE, "an ACL path")
        privsep = entry.get("privsep")
        if not isinstance(privsep, bool):
            problems.append(f"{apath}.privsep must be true or false (privilege separation of the token)")
        if user is None or token is None:
            continue
        if user in RESERVED_ACCOUNT_USERS:
            problems.append(f"{apath}.user {user!r} is not managed by the Host Baseline (root, and the "
                            "firstboot deploy account whose token lives in <node>-proxmox)")
            continue
        name = entry.get("name") or _USERID_RE.match(user).group(1).lower()
        if not isinstance(name, str) or not _ACCOUNT_NAME_RE.match(name):
            problems.append(f"{apath}.name {name!r} is not a SecretsGroup suffix (lowercase letter "
                            "first; lowercase letters, digits, -; max 31) — set `name` explicitly")
            continue
        if name in RESERVED_ACCOUNT_NAMES:
            problems.append(f"{apath}.name {name!r} is reserved (<node>-proxmox holds the deploy token)")
            continue
        if user in users:
            problems.append(f"{apath}.user {user!r} is listed twice")
            continue
        if name in names:
            problems.append(f"{apath}.name {name!r} is listed twice — the SecretsGroup <node>-{name} "
                            "would collide")
            continue
        users.add(user)
        names.add(name)
        out.append({"user": user, "token": token, "role": role, "path": acl_path,
                    "privsep": privsep if isinstance(privsep, bool) else None, "name": name})
    return out


def _validate_network(raw, problems):
    path = f"{CONTEXT_KEY}.network"
    rollback = _int(raw.get("rollback_seconds"), f"{path}.rollback_seconds", problems,
                    ROLLBACK_MIN, ROLLBACK_MAX)
    lacp = raw.get("lacp_rate")
    if lacp is not None and lacp not in LACP_RATES:
        problems.append(f"{path}.lacp_rate must be slow or fast (got {lacp!r})")
        lacp = None
    return {
        "bond_miimon": _int(raw.get("bond_miimon"), f"{path}.bond_miimon", problems, 0, 10000),
        "lacp_rate": lacp,
        "rollback_seconds": rollback or DEFAULT_ROLLBACK_SECONDS,
    }


def validate_context(context):
    """The device's rendered config context -> (config, problems).

    config keys: packages, root_email, snmp, ad, service_accounts, network —
    snmp/ad/network are None when their section says `enabled: false`.
    Every problem is a complete sentence naming the config-context path."""
    problems = []
    hb = (context or {}).get(CONTEXT_KEY) if isinstance(context, dict) else None
    if hb is None:
        return None, [
            f"config context has no '{CONTEXT_KEY}' block — the Host Baseline reads every site and "
            "fleet setting from it (contract §4c; example in docs/sot-data-contract.md)"
        ]
    if not isinstance(hb, dict):
        return None, [f"config context '{CONTEXT_KEY}' must be a mapping"]
    cfg = {
        "packages": validate_packages(hb.get("packages"), problems),
        "root_email": _match(hb.get("root_email"), f"{CONTEXT_KEY}.root_email", problems,
                             _EMAIL_RE, "an e-mail address"),
    }
    raw, _ = _section(hb, "snmp", problems)
    cfg["snmp"] = _validate_snmp(raw, problems) if raw is not None else None
    raw, _ = _section(hb, "ad", problems)
    cfg["ad"] = _validate_ad(raw, problems) if raw is not None else None
    cfg["service_accounts"] = _validate_accounts(hb.get("service_accounts"), problems)
    raw, _ = _section(hb, "network", problems)
    cfg["network"] = _validate_network(raw, problems) if raw is not None else None
    return cfg, problems


def secret_names(cfg):
    """[(purpose, secret name)] the job resolves for this config — every one
    must exist with a value before the node is touched."""
    out = []
    if cfg.get("ad"):
        out.append(("AD bind password", cfg["ad"]["bind_password_secret"]))
    snmp = cfg.get("snmp")
    if snmp:
        if snmp.get("community_secret"):
            out.append(("SNMP community", snmp["community_secret"]))
        for user in snmp.get("v3_users") or []:
            out.append((f"SNMPv3 {user['name']} auth passphrase", user["auth_secret"]))
            out.append((f"SNMPv3 {user['name']} priv passphrase", user["priv_secret"]))
    return out


def secret_value_problem(purpose, name, value):
    """Shape checks on a resolved secret VALUE — the message never contains it."""
    if not value:
        return f"Secret {name!r} ({purpose}) is empty"
    if purpose == "SNMP community":
        if not re.fullmatch(r"[\x21-\x7e]{1,64}", value) or any(c in value for c in "\"'#\\"):
            return (f"Secret {name!r} ({purpose}) must be 1-64 printable ASCII characters without "
                    "spaces, quotes, '#' or backslashes (snmpd.conf token)")
    elif purpose.startswith("SNMPv3"):
        if not re.fullmatch(r"[\x20-\x7e]{8,128}", value) or any(c in value for c in "\"\\"):
            return (f"Secret {name!r} ({purpose}) must be 8-128 printable ASCII characters without "
                    "double quotes or backslashes (net-snmp createUser)")
    elif purpose == "AD bind password":
        if "\n" in value or "\r" in value or "\x00" in value:
            return f"Secret {name!r} ({purpose}) contains a line break or NUL — PVE reads only its first line"
    return None


# ----------------------------------------------------- the interface model

BRIDGE, LAG = "bridge", "lag"
_NON_PORT_TYPES = {BRIDGE, LAG, "virtual"}
_BOND_NAME_RE = re.compile(r"^bond\d{1,3}$")
_BRIDGE_NAME_RE = re.compile(r"^vmbr\d{1,4}$")


def normalize_mac(value):
    """'94:C6:91:AA:F8:52' / '94-c6-91-aa-f8-52' / EUI -> '94:c6:91:aa:f8:52'
    (None when empty or not a 48-bit MAC)."""
    hexdigits = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    if len(hexdigits) != 12:
        return None
    return ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2)).lower()


def interface_record(*, id, name, type, lag=None, bridge=None, mac=None, mtu=None, mode=None,
                     tagged_vids=(), description="", custom_fields=None, ips=()):
    """The plain-dict interface shape every function here works on (the job
    builds it from the ORM, tests by hand)."""
    cf = custom_fields or {}
    return {
        "id": str(id),
        "name": str(name or ""),
        "type": str(type or ""),
        "lag": str(lag) if lag else None,
        "bridge": str(bridge) if bridge else None,
        "mac": normalize_mac(mac),
        "mtu": mtu,
        "mode": str(mode or ""),
        "tagged_vids": sorted(int(v) for v in tagged_vids or ()),
        "description": str(description or ""),
        "lag_mode": cf.get(CF_LAG_MODE) or None,
        "lag_xmit_hash": cf.get(CF_LAG_XMIT_HASH) or None,
        "primary_member": cf.get(CF_PRIMARY_MEMBER) is True,
        "ips": [str(ip) for ip in ips or ()],
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
            f"{kind} {parent['name']} on {device_name}: {CF_PRIMARY_MEMBER} is set on several members "
            f"({', '.join(sorted(m['name'] for m in flagged))}) — flag exactly one"
        )
    if len(members) == 1:
        return members[0]
    if flagged:
        return flagged[0]
    raise InstallNicError(
        f"{kind} {parent['name']} on {device_name} has several members ({', '.join(names)}) and "
        f"none is flagged {CF_PRIMARY_MEMBER} — flag the port that carries the install"
    )


def derive_install_interface(device_name, primary_address, primary_ids, interfaces):
    """The install NIC through the SoT model (decision #55): the interface
    carrying primary_ip4; a bridge resolves to its single port (or the port
    flagged primary_member), a LAG to its single member (or the flagged one).
    -> (interface, chain of names). Raises InstallNicError on any ambiguity.
    The caller checks the result's MAC (its own message for a plain port).
    Keep in step with bmc/answer_service/app.py derive_install_interface —
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
    if iface["type"] == BRIDGE:
        iface = _pick_member(device_name, "bridge", iface,
                             [i for i in interfaces if i.get("bridge") == iface["id"]])
        chain.append(iface["name"])
    if iface["type"] == LAG:
        iface = _pick_member(device_name, "LAG", iface,
                             [i for i in interfaces if i.get("lag") == iface["id"]])
        chain.append(iface["name"])
    if iface["type"] in (BRIDGE, LAG):
        raise InstallNicError(
            f"install NIC derivation for {device_name} reached {iface['name']} (type {iface['type']}) "
            f"via {' -> '.join(chain)} — only bridge -> LAG -> port nesting is supported"
        )
    return iface, chain


def install_nic_mac_problem(device_name, iface, chain):
    """None when the derived install NIC records a MAC; otherwise the refusal."""
    if iface.get("mac"):
        return None
    if len(chain) == 1:
        return (f"{device_name}: the interface carrying primary_ip4 ({iface['name']}) has no MAC "
                "address — pin the mgmt interface MAC")
    return (f"{device_name}: install NIC {iface['name']} (via {' -> '.join(chain)}) has no MAC "
            "address — record the port's MAC on its Nautobot interface")


def _bridge_vids(vids):
    """[10, 11, 12, 20] -> '10-12 20' (ifupdown2 bridge-vids syntax)."""
    out, start, prev = [], None, None
    for vid in sorted(set(vids)):
        if start is None:
            start = prev = vid
        elif vid == prev + 1:
            prev = vid
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = vid
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else str(start))
    return " ".join(out)


def build_network_model(device_name, interfaces, primary_address, primary_ids, gateway, net_cfg):
    """Validate the device's bond/bridge model -> (model, problems).

    model = {"ports": [...], "bonds": [...], "bridges": [...], "mgmt_bridge": name}
    Ports are matched to the node's NICs by MAC later (match_ports); names in
    the model are Nautobot names."""
    problems = []
    net_cfg = net_cfg or {}
    by_id = {i["id"]: i for i in interfaces}
    name_of = {i["id"]: i["name"] for i in interfaces}
    bridges = [i for i in interfaces if i["type"] == BRIDGE]
    bonds = [i for i in interfaces if i["type"] == LAG]
    where = f"{device_name} network model"

    # the management bridge: the interface carrying primary_ip4
    mine = [by_id[str(x)] for x in primary_ids or () if str(x) in by_id]
    mgmt = None
    if len(mine) != 1:
        problems.append(
            f"{where}: primary_ip4 {primary_address} must sit on exactly one interface of the device "
            f"(found {len(mine)})"
        )
    elif mine[0]["type"] != BRIDGE:
        problems.append(
            f"{where}: primary_ip4 {primary_address} is on {mine[0]['name']} (type "
            f"{mine[0]['type'] or 'unset'}), not on a bridge — model the management bridge (vmbr0, "
            "type bridge) with the bond or port as its member and move the IP onto it"
        )
    else:
        mgmt = mine[0]
    try:
        primary_net = ipaddress.ip_interface(str(primary_address))
    except ValueError:
        primary_net = None
        problems.append(f"{where}: primary_ip4 {primary_address!r} is not an IPv4 address with prefix")
    if gateway is None:
        problems.append(f"{where}: no DefaultGW-role IP in primary_ip4's parent prefix (contract §3)")
    elif primary_net is not None:
        try:
            outside = ipaddress.ip_address(gateway) not in primary_net.network
        except ValueError:
            outside = True
        if outside:
            problems.append(f"{where}: DefaultGW {gateway} is outside primary_ip4's network {primary_net.network}")

    def mtu_of(iface):
        mtu = iface.get("mtu")
        if mtu is None:
            return None
        if isinstance(mtu, bool) or not isinstance(mtu, int) or not 576 <= mtu <= 9216:
            problems.append(f"{where}: {iface['name']} mtu {mtu!r} is outside 576-9216")
            return None
        return mtu

    model_ports, model_bonds, model_bridges, port_ids = {}, [], [], set()

    def add_port(iface, parent_name, mtu):
        if iface["type"] in _NON_PORT_TYPES:
            problems.append(
                f"{where}: {iface['name']} (type {iface['type']}) is a member of {parent_name} but is "
                "not a physical port"
            )
            return
        if iface["lag"] and iface["bridge"]:
            problems.append(f"{where}: {iface['name']} is both a LAG member and a bridge port")
            return
        if not iface.get("mac"):
            problems.append(
                f"{where}: member {iface['name']} of {parent_name} has no MAC address — the baseline "
                "matches ports to the node's NICs by MAC, never by name"
            )
            return
        port_ids.add(iface["id"])
        model_ports[iface["id"]] = {"name": iface["name"], "mac": iface["mac"], "mtu": mtu,
                                    "description": iface["description"]}

    miimon = net_cfg.get("bond_miimon")
    lacp_rate = net_cfg.get("lacp_rate")
    for bond in sorted(bonds, key=lambda i: i["name"]):
        name = bond["name"]
        if not _BOND_NAME_RE.match(name):
            problems.append(f"{where}: LAG {name!r} must be named bond<N> (PVE types bonds by name)")
        members = sorted((i for i in interfaces if i.get("lag") == bond["id"]), key=lambda i: i["name"])
        mode = bond.get("lag_mode")
        xmit = bond.get("lag_xmit_hash")
        if not members:
            problems.append(f"{where}: LAG {name} has no members (set the LAG field of its ports)")
        if mode not in LAG_MODES:
            problems.append(f"{where}: LAG {name} has no valid {CF_LAG_MODE} custom field "
                            f"(got {mode!r}; one of {', '.join(LAG_MODES)})")
            mode = None
        if xmit is not None and xmit not in XMIT_HASH_POLICIES:
            problems.append(f"{where}: LAG {name} {CF_LAG_XMIT_HASH} {xmit!r} is not one of "
                            f"{', '.join(XMIT_HASH_POLICIES)}")
            xmit = None
        if mode in _XMIT_HASH_REQUIRED and not xmit:
            problems.append(f"{where}: LAG {name} is {mode} but has no {CF_LAG_XMIT_HASH} — set the "
                            "transmit hash policy in the SoT (it must match the switch side)")
        if mode and mode not in _XMIT_HASH_MODES and xmit:
            problems.append(f"{where}: LAG {name} sets {CF_LAG_XMIT_HASH} {xmit!r} but mode {mode} does "
                            "not use it — clear the field or fix the mode")
        if miimon is None:
            problems.append(f"{where}: LAG {name} needs {CONTEXT_KEY}.network.bond_miimon in the config "
                            "context")
        if mode == "802.3ad" and lacp_rate is None:
            problems.append(f"{where}: LAG {name} is 802.3ad but {CONTEXT_KEY}.network.lacp_rate is not "
                            "set (slow or fast — must match the switch side)")
        flagged = [m for m in members if m.get("primary_member")]
        primary = None
        if len(flagged) > 1:
            problems.append(f"{where}: LAG {name}: {CF_PRIMARY_MEMBER} is set on several members "
                            f"({', '.join(m['name'] for m in flagged)}) — flag exactly one")
        elif flagged:
            primary = flagged[0]
        if mode == "active-backup" and len(members) > 1 and primary is None:
            problems.append(f"{where}: LAG {name} is active-backup with {len(members)} members but none is "
                            f"flagged {CF_PRIMARY_MEMBER} — flag the port that should carry traffic")
        bond_mtu = mtu_of(bond)
        for member in members:
            member_mtu = mtu_of(member)
            if bond_mtu and member_mtu and bond_mtu != member_mtu:
                problems.append(f"{where}: {member['name']} mtu {member_mtu} differs from its LAG {name} "
                                f"mtu {bond_mtu} (the kernel forces the bond's MTU onto members)")
            add_port(member, f"LAG {name}", member_mtu or bond_mtu)
        model_bonds.append({
            "name": name, "id": bond["id"], "members": [m["id"] for m in members], "mode": mode,
            "xmit_hash": xmit if mode in _XMIT_HASH_MODES else None,
            "lacp_rate": lacp_rate if mode == "802.3ad" else None, "miimon": miimon,
            "primary": primary["id"] if primary is not None and mode in _PRIMARY_MODES else None,
            "mtu": bond_mtu or max([model_ports[m["id"]]["mtu"] or 0 for m in members
                                    if m["id"] in model_ports] or [0]) or None,
            "description": bond["description"],
        })
    bond_by_id = {b["id"]: b for b in model_bonds}

    for bridge in sorted(bridges, key=lambda i: i["name"]):
        name = bridge["name"]
        if not _BRIDGE_NAME_RE.match(name):
            problems.append(f"{where}: bridge {name!r} must be named vmbr<N> (PVE types bridges by name)")
        ports = sorted((i for i in interfaces if i.get("bridge") == bridge["id"]), key=lambda i: i["name"])
        flagged_ports = [i["name"] for i in ports if i.get("primary_member")]
        if len(flagged_ports) > 1:
            problems.append(f"{where}: bridge {name}: {CF_PRIMARY_MEMBER} is set on several ports "
                            f"({', '.join(flagged_ports)}) — flag exactly one")
        bridge_mtu = mtu_of(bridge)
        port_ids_of_bridge = []
        for port in ports:
            if port["type"] == LAG:
                bond = bond_by_id.get(port["id"])
                port_mtu = bond["mtu"] if bond else None
            elif port["type"] == BRIDGE:
                problems.append(f"{where}: bridge {name} has another bridge ({port['name']}) as a port")
                continue
            else:
                port_mtu = mtu_of(port)
                add_port(port, f"bridge {name}", port_mtu)
            if bridge_mtu and port_mtu and bridge_mtu > port_mtu:
                problems.append(f"{where}: bridge {name} mtu {bridge_mtu} is above its port "
                                f"{port['name']}'s mtu {port_mtu}")
            port_ids_of_bridge.append(port["id"])
        mode = bridge.get("mode") or ""
        vids = None
        if mode == "tagged-all":
            vids = "2-4094"
        elif mode == "tagged":
            if not bridge.get("tagged_vids"):
                problems.append(f"{where}: bridge {name} is mode tagged but carries no tagged VLANs")
            else:
                vids = _bridge_vids(bridge["tagged_vids"])
        elif mode == "access":
            problems.append(f"{where}: bridge {name} has mode access — use tagged-all (VLAN-aware) or "
                            "no mode (plain bridge)")
        is_mgmt = mgmt is not None and bridge["id"] == mgmt["id"]
        extra_ips = [ip for ip in bridge.get("ips") or () if not is_mgmt or ip != str(primary_address)]
        if extra_ips:
            problems.append(f"{where}: bridge {name} carries address(es) {', '.join(extra_ips)} besides "
                            "primary_ip4 — the baseline renders only the management address")
        model_bridges.append({
            "name": name, "id": bridge["id"], "ports": port_ids_of_bridge, "vlan_aware": vids is not None,
            "vids": vids, "mtu": bridge_mtu, "description": bridge["description"],
            "address": str(primary_address) if is_mgmt else None,
            "gateway": gateway if is_mgmt else None,
        })

    for iface in interfaces:
        if iface["type"] in (BRIDGE, LAG) or iface["id"] in port_ids or iface["name"] == "xcc":
            continue
        if iface.get("ips"):
            problems.append(f"{where}: {iface['name']} carries address(es) {', '.join(iface['ips'])} but "
                            "is not part of the bond/bridge topology — the baseline would drop it")
    model = {
        "ports": [dict(p, id=pid) for pid, p in sorted(model_ports.items(), key=lambda kv: kv[1]["name"])],
        "bonds": model_bonds,
        "bridges": model_bridges,
        "mgmt_bridge": mgmt["name"] if mgmt else None,
        "names": name_of,
    }
    return model, problems


# ------------------------------------------------- the node's NICs (by MAC)

_IP_LINK_RE = re.compile(r"^\d+:\s+(?P<name>[^:@\s]+)(?:@[^:\s]+)?:\s+<(?P<flags>[^>]*)>(?P<rest>.*)$")


def parse_ip_link(text):
    """`ip -o link show` -> [{name, mac, permaddr, master, mtu, state, flags}]."""
    out = []
    for line in (text or "").splitlines():
        m = _IP_LINK_RE.match(line.strip())
        if not m:
            continue
        rest = m.group("rest").replace("\\", " ")
        tokens = rest.split()

        def after(key):
            return tokens[tokens.index(key) + 1] if key in tokens and tokens.index(key) + 1 < len(tokens) else None

        out.append({
            "name": m.group("name"),
            "flags": m.group("flags").split(","),
            "mtu": int(after("mtu")) if (after("mtu") or "").isdigit() else None,
            "master": after("master"),
            "state": after("state"),
            "mac": normalize_mac(after("link/ether")),
            "permaddr": normalize_mac(after("permaddr")),
        })
    return out


def match_ports(model, links, physical_names):
    """SoT ports -> node NIC names by MAC (the permanent address when the NIC
    is enslaved — `ip -o link` shows it as permaddr). -> (names, problems)."""
    problems = []
    physical = set(physical_names or ())
    by_mac = {}
    for link in links:
        if link["name"] not in physical:
            continue
        mac = link.get("permaddr") or link.get("mac")
        if mac:
            by_mac.setdefault(mac, []).append(link["name"])
    names, used = {}, {}
    seen_nics = sorted(f"{link['name']}={link.get('permaddr') or link.get('mac')}"
                       for link in links if link["name"] in physical)
    for port in model["ports"]:
        hits = by_mac.get(port["mac"], [])
        if not hits:
            problems.append(
                f"member {port['name']} ({port['mac']}) is not among the node's physical NICs "
                f"({', '.join(seen_nics) or 'none'}) — wrong MAC in Nautobot, or the card is missing"
            )
            continue
        if len(hits) > 1:
            problems.append(f"MAC {port['mac']} of {port['name']} appears on several node NICs "
                            f"({', '.join(sorted(hits))})")
            continue
        if hits[0] in used:
            problems.append(f"{port['name']} and {used[hits[0]]} both resolve to node NIC {hits[0]} "
                            "(duplicate MAC in Nautobot)")
            continue
        used[hits[0]] = port["name"]
        names[port["id"]] = hits[0]
    return names, problems


# --------------------------------------------- /etc/network/interfaces

INTERFACES_HEADER = """\
# network interface settings; autogenerated
# Please do NOT modify this file directly, unless you know what
# you're doing.
#
# If you want to manage parts of the network configuration manually,
# please utilize the 'source' or 'source-directory' directives to do
# so.
# PVE will preserve these directives, but will NOT read its network
# configuration from sourced files, so do not attempt to move any of
# the PVE managed interfaces into external files!
#
# Rendered from Nautobot by the Host Baseline job (SoT: the Device's bond and
# bridge interfaces); a re-run overwrites local edits.
"""


def _comment(text):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return f"#{text}\n" if text else ""


def render_interfaces(model, linux_names):
    """The node's /etc/network/interfaces from the validated model and the
    MAC-matched Linux names (Nautobot interface id -> Linux name)."""
    def lname(iface_id):
        return linux_names.get(iface_id) or model["names"][iface_id]

    out = [INTERFACES_HEADER, "auto lo\niface lo inet loopback\n"]
    for port in sorted(model["ports"], key=lambda p: lname(p["id"])):
        stanza = f"auto {lname(port['id'])}\niface {lname(port['id'])} inet manual\n"
        if port.get("mtu"):
            stanza += f"\tmtu {port['mtu']}\n"
        out.append(stanza + _comment(port.get("description")))
    for bond in model["bonds"]:
        stanza = f"auto {bond['name']}\niface {bond['name']} inet manual\n"
        stanza += f"\tbond-slaves {' '.join(sorted(lname(m) for m in bond['members']))}\n"
        stanza += f"\tbond-miimon {bond['miimon']}\n"
        stanza += f"\tbond-mode {bond['mode']}\n"
        if bond.get("xmit_hash"):
            stanza += f"\tbond-xmit-hash-policy {bond['xmit_hash']}\n"
        if bond.get("lacp_rate"):
            stanza += f"\tbond-lacp-rate {bond['lacp_rate']}\n"
        if bond.get("primary"):
            stanza += f"\tbond-primary {lname(bond['primary'])}\n"
        if bond.get("mtu"):
            stanza += f"\tmtu {bond['mtu']}\n"
        out.append(stanza + _comment(bond.get("description")))
    for bridge in model["bridges"]:
        method = "static" if bridge.get("address") else "manual"
        stanza = f"auto {bridge['name']}\niface {bridge['name']} inet {method}\n"
        if bridge.get("address"):
            stanza += f"\taddress {bridge['address']}\n"
            if bridge.get("gateway"):
                stanza += f"\tgateway {bridge['gateway']}\n"
        ports = [lname(p) for p in bridge["ports"]]
        stanza += f"\tbridge-ports {' '.join(ports) if ports else 'none'}\n"
        stanza += "\tbridge-stp off\n\tbridge-fd 0\n"
        if bridge.get("vlan_aware"):
            stanza += f"\tbridge-vlan-aware yes\n\tbridge-vids {bridge['vids']}\n"
        if bridge.get("mtu"):
            stanza += f"\tmtu {bridge['mtu']}\n"
        out.append(stanza + _comment(bridge.get("description")))
    out.append("source /etc/network/interfaces.d/*\n")
    return "\n".join(out)


def unified_diff(current, rendered, path):
    """A readable unified diff (empty string when equal)."""
    if current == rendered:
        return ""
    return "".join(difflib.unified_diff(
        (current or "").splitlines(keepends=True), rendered.splitlines(keepends=True),
        fromfile=f"{path} (node)", tofile=f"{path} (SoT render)",
    ))


# ------------------------------------------------------ /proc/net/bonding

_BOND_MODE_TEXT = {
    "load balancing (round-robin)": "balance-rr",
    "fault-tolerance (active-backup)": "active-backup",
    "load balancing (xor)": "balance-xor",
    "fault-tolerance (broadcast)": "broadcast",
    "IEEE 802.3ad Dynamic link aggregation": "802.3ad",
    "transmit load balancing": "balance-tlb",
    "adaptive load balancing": "balance-alb",
}


def parse_proc_bonding(text):
    """/proc/net/bonding/<bond> -> dict (mode, xmit_hash, mii_status, miimon,
    primary, active_slave, lacp_rate, aggregator {id, ports, partner_mac},
    slaves [{name, mii_status, aggregator_id, perm_hwaddr}])."""
    info = {"mode": None, "xmit_hash": None, "mii_status": None, "miimon": None, "primary": None,
            "active_slave": None, "lacp_rate": None, "aggregator": {}, "slaves": []}
    section, slave = "bond", None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "Slave Interface":
            slave = {"name": value, "mii_status": None, "aggregator_id": None, "perm_hwaddr": None}
            info["slaves"].append(slave)
            section = "slave"
            continue
        if key == "Active Aggregator Info":
            section = "aggregator"
            continue
        if line.startswith("details "):
            section = "details"
            continue
        if section == "bond" or (section == "aggregator" and key not in (
                "Aggregator ID", "Number of ports", "Actor Key", "Partner Key", "Partner Mac Address")):
            if key == "Bonding Mode":
                info["mode"] = _BOND_MODE_TEXT.get(value, value)
            elif key == "Transmit Hash Policy":
                info["xmit_hash"] = value.split(" (")[0]
            elif key == "MII Status" and info["mii_status"] is None:
                info["mii_status"] = value
            elif key == "MII Polling Interval (ms)":
                info["miimon"] = int(value) if value.isdigit() else None
            elif key == "Primary Slave":
                info["primary"] = value.split(" (")[0]
            elif key == "Currently Active Slave":
                info["active_slave"] = value
            elif key == "LACP rate":
                info["lacp_rate"] = value
        elif section == "aggregator":
            if key == "Aggregator ID":
                info["aggregator"]["id"] = int(value) if value.isdigit() else None
            elif key == "Number of ports":
                info["aggregator"]["ports"] = int(value) if value.isdigit() else None
            elif key == "Partner Mac Address":
                info["aggregator"]["partner_mac"] = normalize_mac(value)
        elif section == "slave" and slave is not None:
            if key == "MII Status":
                slave["mii_status"] = value
            elif key == "Aggregator ID":
                slave["aggregator_id"] = int(value) if value.isdigit() else None
            elif key == "Permanent HW addr":
                slave["perm_hwaddr"] = normalize_mac(value)
    return info


def evaluate_bond(bond, linux_members, linux_primary, text):
    """One bond's running state vs the SoT -> (errors, warnings). Missing
    bond, wrong mode or member set are errors (the intent is not in effect);
    LACP partner/aggregation, link and active-slave state are WARNINGS —
    the switch side is out of scope (decision #54, Brian 2026-10-02)."""
    errors, warnings = [], []
    name = bond["name"]
    if not text:
        return [f"{name}: /proc/net/bonding/{name} is missing — the bond is not up"], warnings
    info = parse_proc_bonding(text)
    if info["mode"] != bond["mode"]:
        errors.append(f"{name}: running mode {info['mode']!r}, SoT says {bond['mode']!r}")
    running = sorted(s["name"] for s in info["slaves"])
    if running != sorted(linux_members):
        errors.append(f"{name}: running members {running}, SoT says {sorted(linux_members)}")
    if bond.get("xmit_hash") and info["xmit_hash"] and info["xmit_hash"] != bond["xmit_hash"]:
        warnings.append(f"{name}: transmit hash policy {info['xmit_hash']}, SoT says {bond['xmit_hash']}")
    if bond.get("miimon") is not None and info["miimon"] is not None and info["miimon"] != bond["miimon"]:
        warnings.append(f"{name}: miimon {info['miimon']} ms, SoT says {bond['miimon']}")
    for slave in info["slaves"]:
        if slave["mii_status"] != "up":
            warnings.append(f"{name}: member {slave['name']} link is {slave['mii_status'] or 'unknown'}")
    if bond["mode"] == "active-backup" and linux_primary:
        if info["active_slave"] and info["active_slave"] != linux_primary:
            warnings.append(f"{name}: active member is {info['active_slave']}, the SoT primary "
                            f"{linux_primary} is not carrying traffic (its link down?)")
    if bond["mode"] == "802.3ad":
        if bond.get("lacp_rate") and info["lacp_rate"] and info["lacp_rate"] != bond["lacp_rate"]:
            warnings.append(f"{name}: LACP rate {info['lacp_rate']}, SoT says {bond['lacp_rate']}")
        agg = info["aggregator"]
        partner = agg.get("partner_mac")
        if not partner or partner == "00:00:00:00:00:00":
            warnings.append(f"{name}: LACP has no partner (partner MAC {partner or 'unknown'}) — the "
                            "switch ports are not running LACP for this bundle yet")
        else:
            members_in = [s["name"] for s in info["slaves"] if s["aggregator_id"] == agg.get("id")]
            if len(members_in) < len(info["slaves"]):
                warnings.append(f"{name}: only {sorted(members_in)} aggregate with partner {partner} "
                                f"(active aggregator {agg.get('id')}, {agg.get('ports')} port(s)) — check "
                                "the switch-side channel")
    return errors, warnings


def split_bonding_dump(text):
    """The applier's '=== <bond>' framed dump of /proc/net/bonding/* -> {bond: text}."""
    out, name, lines = {}, None, []
    for line in (text or "").splitlines():
        if line.startswith("=== "):
            if name:
                out[name] = "\n".join(lines)
            name, lines = line[4:].strip(), []
        elif name:
            lines.append(line)
    if name:
        out[name] = "\n".join(lines)
    return out


# ------------------------------------------------------------------- SNMP

SNMPD_CONF_PATH = "/etc/snmp/snmpd.conf"


def snmp_location(location_name):
    """sysLocation = the Device's Location NAME (decision #5/#54; the name,
    not the path, so a hierarchy reorganisation above the site never churns
    every node's monitoring tags). -> (value, problem)."""
    name = str(location_name or "").strip()
    if not name:
        return None, "the Device has no Location name for SNMP sysLocation"
    if _CONTROL_RE.search(name):
        return None, f"Location name {name!r} contains a control character — cannot be sysLocation"
    return name, None


def render_snmpd_conf(*, location, contact, community=None, community_source=None,
                      community_view=None, v3_users=()):
    """/etc/snmp/snmpd.conf in the tester's field-verified layout. Contains
    the community (the node writes it 0600); never log the result — the
    applier reports a masked diff."""
    lines = [
        "# Rendered from Nautobot by the Host Baseline job: sysLocation is the",
        "# Device's Location name, the rest comes from the config context",
        "# (host_baseline.snmp) and Nautobot Secrets; a re-run overwrites local edits.",
        f"sysLocation    {location}",
        f"sysContact     {contact}",
        "sysServices    72",
        "master  agentx",
        "agentaddress udp:161",
        "view   systemonly  included   .1.3.6.1.2.1.1",
        "view   systemonly  included   .1.3.6.1.2.1.25.1",
    ]
    if community:
        view = f" -V {community_view}" if community_view else ""
        if community_source:
            keyword = "rocommunity6" if ipaddress.ip_network(community_source).version == 6 else "rocommunity"
            lines.append(f"{keyword:<12} {community} {community_source}{view}")
        else:
            source = " default" if view else ""
            lines.append(f"rocommunity  {community}{source}{view}")
            lines.append(f"rocommunity6 {community}{source}{view}")
    for user in v3_users or ():
        view = f" -V {user['view']}" if user.get("view") else ""
        lines.append(f"rouser {user['name']} authpriv{view}")
    lines.append("includeDir /etc/snmp/snmpd.conf.d")
    return "\n".join(lines) + "\n"


def snmpv3_fingerprint(device_id, user, auth_protocol, auth_pass, priv_protocol, priv_pass):
    """Change marker for an SNMPv3 user's credentials, kept root-only on the
    node (/var/lib/nfv-baseline/snmpv3/<user>.fp): a slow salted hash, never
    the passphrases. A rotated Secret changes it -> the user is re-created."""
    material = "\0".join([auth_protocol, auth_pass, priv_protocol, priv_pass]).encode()
    salt = f"nfv-host-baseline:{device_id}:{user}".encode()
    return hashlib.pbkdf2_hmac("sha256", material, salt, 100_000).hex()


# --------------------------------------------------------------- AD realm

# SoT key -> PVE realm option (order = argv order).
_REALM_OPTIONS = (
    ("domain", "domain"), ("server1", "server1"), ("server2", "server2"), ("port", "port"),
    ("mode", "mode"), ("verify", "verify"), ("base_dn", "base_dn"), ("bind_dn", "bind_dn"),
    ("user_filter", "filter"), ("group_filter", "group_filter"),
    ("sync_attributes", "sync_attributes"), ("sync_defaults_options", "sync-defaults-options"),
    ("case_sensitive", "case-sensitive"), ("comment", "comment"), ("default_realm", "default"),
)
# PVE's defaults for boolean realm options when the config omits them.
_REALM_BOOL_DEFAULTS = {"verify": "0", "case-sensitive": "1", "default": "0"}


def _pve_str(value):
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


def realm_options(ad):
    """The PVE realm options the SoT manages -> {option: string}. The bind
    password is NOT among them: it goes to PVE's credential file
    (/etc/pve/priv/realm/<realm>.pw), never onto an argv."""
    values = dict(ad)
    values["server1"] = ad["servers"][0]
    values["server2"] = ad["servers"][1] if len(ad["servers"]) > 1 else None
    out = {}
    for sot_key, pve_key in _REALM_OPTIONS:
        value = values.get(sot_key)
        if value is not None:
            out[pve_key] = _pve_str(value)
    return out


def _current_option(current, key):
    value = current.get(key)
    if value is None:
        return _REALM_BOOL_DEFAULTS.get(key)
    return _pve_str(value)


def plan_realm(ad, current):
    """`current` = the realm's config on the node (pvesh get /access/domains/
    <realm>) or None. -> {"action": add|modify|none|refuse, "argv", "changes",
    "problem"}."""
    realm = ad["realm"]
    want = realm_options(ad)
    order = [pve for _, pve in _REALM_OPTIONS]
    if current is None:
        argv = ["pveum", "realm", "add", realm, "--type", "ad"]
        for key in order:
            if key in want:
                argv += [f"--{key}", want[key]]
        return {"action": "add", "argv": argv, "changes": [k for k in order if k in want], "problem": None}
    if current.get("type") not in (None, "ad"):
        return {"action": "refuse", "argv": None, "changes": [], "problem": (
            f"realm {realm} exists on the node with type {current.get('type')!r} but the SoT describes "
            "an AD realm — the type of a realm cannot change; remove or rename it by hand"
        )}
    changes = [k for k in order if k in want and _current_option(current, k) != want[k]]
    deletes = ["server2"] if "server2" not in want and current.get("server2") else []
    if not changes and not deletes:
        return {"action": "none", "argv": None, "changes": [], "problem": None}
    argv = ["pveum", "realm", "modify", realm]
    for key in changes:
        argv += [f"--{key}", want[key]]
    if deletes:
        argv += ["--delete", ",".join(deletes)]
    return {"action": "modify", "argv": argv, "changes": changes + [f"-{d}" for d in deletes],
            "problem": None}


def plan_sync_job(ad, current_jobs):
    """The realm-sync job (pvesh /cluster/jobs/realm-sync/<name>)."""
    job = ad["sync_job"]
    jid = job["name"]
    path = f"/cluster/jobs/realm-sync/{jid}"
    want = {"schedule": job["schedule"], "scope": job["scope"]}
    if job.get("enable_new") is not None:
        want["enable-new"] = _pve_str(job["enable_new"])
    if job.get("remove_vanished"):
        want["remove-vanished"] = job["remove_vanished"]
    current = next((j for j in current_jobs or () if isinstance(j, dict) and j.get("id") == jid), None)
    if current is None:
        argv = ["pvesh", "create", path, "--realm", ad["realm"]]
        for key, value in want.items():
            argv += [f"--{key}", value]
        return {"action": "create", "argv": argv, "changes": list(want), "problem": None}
    if current.get("realm") != ad["realm"]:
        return {"action": "refuse", "argv": None, "changes": [], "problem": (
            f"realm-sync job {jid} exists for realm {current.get('realm')!r} but the SoT names realm "
            f"{ad['realm']} — a sync job's realm is fixed; delete it by hand (pvesh delete {path}) or "
            "rename host_baseline.ad.sync_job.name"
        )}
    changes = {k: v for k, v in want.items() if _pve_str(current.get(k)) != v}
    if _pve_str(current.get("enabled", 1)) != "1":
        changes["enabled"] = "1"
    if not changes:
        return {"action": "none", "argv": None, "changes": [], "problem": None}
    argv = ["pvesh", "set", path]
    for key, value in changes.items():
        argv += [f"--{key}", value]
    return {"action": "set", "argv": argv, "changes": list(changes), "problem": None}


def realm_sync_argv(ad, dry_run=False):
    """`pveum realm sync` with the sync job's scope/enable-new (dry_run: PVE's
    own --dry-run, which reads AD and writes nothing)."""
    job = ad["sync_job"]
    argv = ["pveum", "realm", "sync", ad["realm"], "--scope", job["scope"]]
    if job.get("enable_new") is not None:
        argv += ["--enable-new", _pve_str(job["enable_new"])]
    if dry_run:
        argv += ["--dry-run", "1"]
    return argv


def plan_root_email(email, users):
    root = next((u for u in users or () if isinstance(u, dict) and u.get("userid") == "root@pam"), None)
    if root is not None and (root.get("email") or "") == email:
        return None
    return ["pveum", "user", "modify", "root@pam", "--email", email]


# ----------------------------------------------------------------- ACLs

_ACL_KIND_FLAG = {"user": "--users", "group": "--groups", "token": "--tokens"}


def acl_set(acls):
    return {
        (a.get("path"), a.get("type"), a.get("ugid"), a.get("roleid"))
        for a in acls or () if isinstance(a, dict)
    }


def plan_acl_changes(desired, current, managed):
    """desired: [(path, kind, ugid, role)]; current: acl_set(); managed:
    {(kind, ugid)} whose ACLs the SoT owns completely (service accounts) —
    their entries not in `desired` are removed (least privilege converges).
    -> (adds, deletes) as pveum argv lists."""
    wanted = set(desired)
    adds = [["pveum", "acl", "modify", p, _ACL_KIND_FLAG[k], u, "--roles", r]
            for (p, k, u, r) in desired if (p, k, u, r) not in current]
    deletes = [["pveum", "acl", "delete", p, _ACL_KIND_FLAG[k], u, "--roles", r]
               for (p, k, u, r) in sorted(current) if (k, u) in managed and (p, k, u, r) not in wanted]
    return adds, deletes


# ------------------------------------------------------- service accounts

def slugify(value):
    """Same rule as the answer service's slugify (file and Secret names)."""
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def node_token_names(device_name, account):
    """Per-node token Secrets, named exactly like the answer service's
    phone-home names the deploy token (account "proxmox" there):
    SecretsGroup <node>-<account> with Generic/username = token id and
    Generic/secret = value, text-file Secrets <slug>-<account>-token-username
    / -secret over the files <slug>_<account>_token_id / _secret."""
    slug = slugify(device_name)
    return {
        "group": f"{device_name}-{account}",
        "secret_username": f"{slug}-{account}-token-username",
        "secret_secret": f"{slug}-{account}-token-secret",
        "file_id": f"{slug}_{account}_token_id",
        "file_secret": f"{slug}_{account}_token_secret",
    }


def token_full_id(account):
    return f"{account['user']}!{account['token']}"


def plan_service_accounts(accounts, users, acls, stored):
    """Per account -> plan dict. `stored[name]` is the Nautobot side:
    "ok" (both Secrets readable, token id matches, value not rejected by the
    node), "missing" (a Secret absent/unreadable or naming another token id)
    or "invalid" (the node rejected the stored value with 401)."""
    by_user = {u.get("userid"): u for u in users or () if isinstance(u, dict)}
    current = acl_set(acls)
    plans = []
    for acc in accounts:
        user = by_user.get(acc["user"])
        tokens = {t.get("tokenid"): t for t in (user or {}).get("tokens") or () if isinstance(t, dict)}
        token = tokens.get(acc["token"])
        state = stored.get(acc["name"], "missing")
        if token is None:
            action, reason = "create", "token absent on the node"
        elif state == "missing":
            action, reason = "rotate", "its Secrets are missing in Nautobot — the value is unrecoverable"
        elif state == "invalid":
            action, reason = "rotate", "the node rejects the stored value (401)"
        else:
            action, reason = "keep", "token present and its Secrets are stored"
        privsep_fix = None
        if action == "keep" and _pve_str(token.get("privsep", 1)) != _pve_str(bool(acc["privsep"])):
            privsep_fix = ["pveum", "user", "token", "modify", acc["user"], acc["token"],
                           "--privsep", _pve_str(bool(acc["privsep"]))]
        full = token_full_id(acc)
        desired = [(acc["path"], "user", acc["user"], acc["role"])]
        if acc["privsep"]:
            desired.append((acc["path"], "token", full, acc["role"]))
        # A created/rotated token starts without ACLs (removal drops them).
        cur = current if action == "keep" else {c for c in current if c[2] != full}
        adds, deletes = plan_acl_changes(desired, cur, {("user", acc["user"]), ("token", full)})
        plans.append({
            "account": acc,
            "user_add": None if user is not None else
            ["pveum", "user", "add", acc["user"], "--comment", MANAGED_COMMENT],
            "token": action,
            "token_reason": reason,
            "privsep_fix": privsep_fix,
            "acl_adds": adds,
            "acl_deletes": deletes,
        })
    return plans


_TOKEN_VALUE_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def valid_token_value(value):
    """PVE API token values are UUIDs."""
    return isinstance(value, str) and bool(_TOKEN_VALUE_RE.match(value))


# ------------------------------------------------------------- identity

def identity_problems(device_name, device_serial, node_hostname, node_serial):
    """Hostname and DMI serial on the node must be the Device's (serial:
    trimmed, case-insensitive — the BMC rule) before anything is written."""
    problems = []
    host = (node_hostname or "").strip().split(".")[0]
    if not host:
        problems.append("could not read the node's hostname")
    elif host.lower() != str(device_name).lower():
        problems.append(
            f"the node at the management IP calls itself {host!r}, not {device_name!r} — wrong "
            "primary_ip4 or wrong Device; refusing to touch it"
        )
    serial = (node_serial or "").strip()
    if not serial:
        problems.append(
            "could not read the node's DMI serial (/sys/class/dmi/id/product_serial needs the ROOT "
            f"login in the {HOST_SSH_USERNAME_SECRET}/{HOST_SSH_PASSWORD_SECRET} Secrets)"
        )
    elif serial.upper() != str(device_serial or "").strip().upper():
        problems.append(
            f"the node's DMI serial is {serial!r}, but {device_name}'s serial is "
            f"{str(device_serial or '').strip()!r} — refusing to touch that machine"
        )
    return problems


def pick_gateway(addresses):
    """The DefaultGW-role addresses inside primary_ip4's parent prefix ->
    (gateway or None, problem or None). Exactly one (contract §3)."""
    hosts = sorted({str(a).split("/")[0] for a in addresses or ()})
    if len(hosts) > 1:
        return None, (f"several DefaultGW-role IPs in primary_ip4's prefix ({', '.join(hosts)}) — "
                      "contract §3 allows exactly one")
    return (hosts[0], None) if hosts else (None, None)


# ------------------------------------------------- the applier transport

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sh_local(name, value):
    """`local NAME=<quoted>` (or an array) for the payload function."""
    if not _IDENT_RE.match(name):
        raise ValueError(f"not a shell identifier: {name!r}")
    if isinstance(value, (list, tuple)):
        return f"local -a {name}=(" + " ".join(shlex.quote(str(v)) for v in value) + ")"
    return f"local {name}={shlex.quote(str(value))}"


def sh_call(argv):
    return " ".join(shlex.quote(str(a)) for a in argv)


def build_payload(applier_source, locals_, calls):
    """The script `bash -s` receives: the applier's function library, then
    ONE payload function holding the step's inputs as locals (secrets
    included, shell-quoted) and its calls, then the call of that function.
    Nothing runs before the last line arrives, so a truncated upload runs
    nothing. Never log the result — log `calls` instead (they carry no
    secret by construction; tests assert it)."""
    body = ["__nfv_payload() {", "  set +x  # before the locals: no trace ever shows a secret"]
    body += [f"  {sh_local(name, value)}" for name, value in locals_]
    body.append("  exec </dev/null")
    body.append("  nfv_init")  # set +x, pipefail, LC_ALL, path defaults, the root check
    body += [f"  {sh_call(argv)}" for argv in calls]
    body.append("  nfv_done")
    body.append("}")
    return applier_source.rstrip("\n") + "\n\n" + "\n".join(body) + "\n__nfv_payload\n"


def parse_events(stdout_text):
    """Applier stdout -> (events, count of unrecognised lines)."""
    events, noise = [], 0
    for line in (stdout_text or "").splitlines():
        if line.startswith(EVENT_PREFIX):
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
            except ValueError:
                noise += 1
                continue
            if isinstance(event, dict):
                events.append(event)
            else:
                noise += 1
        elif line.strip():
            noise += 1
    return events, noise


def decode_observation(events):
    """observe-step events -> {item: {"rc": int, "text": str}}."""
    import base64

    out = {}
    for event in events:
        if event.get("step") != "observe" or "b64" not in event:
            continue
        try:
            text = base64.b64decode(event["b64"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            text = ""
        out[str(event.get("item"))] = {"rc": int(event.get("rc", 1)), "text": text}
    return out


def json_from(observation, item, default=None):
    """A JSON document from an observed command (None/default on rc != 0)."""
    entry = (observation or {}).get(item)
    if not entry or entry["rc"] != 0:
        return default
    try:
        return json.loads(entry["text"])
    except ValueError:
        return default


def scrub(text, secrets):
    """Replace every known secret value (>= 4 chars) in a string about to be
    logged — the last line of defence, not the mechanism."""
    out = str(text)
    for value in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(value, "<redacted>")
    return out


# --------------------------------------------- config-context JSON schema

def _str(pattern=None, max_length=255):
    schema = {"type": "string", "maxLength": max_length}
    if pattern:
        schema["pattern"] = pattern
    return schema


def _enabled():
    return {"type": "boolean"}


# Shape-only (types, enums, patterns — no `required`): config contexts are
# partial and merged (fleet + site + device), so required-ness is enforced by
# validate_context() on the merged result, not here. The bootstrap keeps a
# ConfigContextSchema "nfv-host-baseline" equal to this; attach it to the
# contexts that carry host_baseline to get edit-time validation.
CONTEXT_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        CONTEXT_KEY: {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "packages": {"type": "array", "items": _str(_PKG_RE.pattern, 63)},
                "serial_console": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "speed": {"type": "integer", "enum": [9600, 19200, 38400, 57600, 115200]},
                        "word": {"type": "integer", "minimum": 5, "maximum": 8},
                        "parity": {"type": "string", "enum": ["no", "odd", "even"]},
                        "stop": {"type": "integer", "enum": [1, 2]},
                    },
                },
                "zfs_arc_max_bytes": {"type": "integer", "minimum": 64 * 1024 * 1024},
                "remove_subscription_nag": {"type": "boolean"},
                "root_email": _str(_EMAIL_RE.pattern),
                "snmp": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "enabled": _enabled(),
                        "contact": _str(),
                        "community_secret": _str(_SECRET_NAME_RE.pattern, 100),
                        "community_source": _str(None, 64),
                        "community_view": {"type": "string", "enum": list(SNMP_VIEWS)},
                        "v3_users": {"type": "array", "items": {"anyOf": [
                            _str(_SNMP_USER_RE.pattern, 32),
                            {"type": "object", "additionalProperties": False, "properties": {
                                "name": _str(_SNMP_USER_RE.pattern, 32),
                                "auth_protocol": {"type": "string", "enum": list(SNMP_AUTH_PROTOCOLS)},
                                "priv_protocol": {"type": "string", "enum": list(SNMP_PRIV_PROTOCOLS)},
                                "view": {"type": "string", "enum": list(SNMP_VIEWS)},
                                "auth_secret": _str(_SECRET_NAME_RE.pattern, 100),
                                "priv_secret": _str(_SECRET_NAME_RE.pattern, 100),
                            }},
                        ]}},
                    },
                },
                "ad": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "enabled": _enabled(),
                        "realm": _str(_REALM_RE.pattern, 32),
                        "domain": _str(_DOMAIN_RE.pattern, 253),
                        "servers": {"type": "array", "minItems": 1, "maxItems": 2,
                                    "items": _str(_HOST_RE.pattern, 253)},
                        "mode": {"type": "string", "enum": ["ldap", "ldaps", "ldap+starttls"]},
                        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                        "verify": {"type": "boolean"},
                        "base_dn": _str(None, 1024),
                        "bind_dn": _str(None, 1024),
                        "bind_password_secret": _str(_SECRET_NAME_RE.pattern, 100),
                        "user_filter": _str(None, 2048),
                        "group_filter": _str(None, 2048),
                        "sync_attributes": _str(_SYNC_ATTR_RE.pattern),
                        "sync_defaults_options": _str(_SYNC_OPTS_RE.pattern, 200),
                        "case_sensitive": {"type": "boolean"},
                        "comment": _str(),
                        "default_realm": {"type": "boolean"},
                        "admin_group": _str(_PVE_ID_RE.pattern, 64),
                        "admin_role": _str(_PVE_ID_RE.pattern, 64),
                        "admin_path": _str(_ACL_PATH_RE.pattern),
                        "sync_job": {"type": "object", "additionalProperties": False, "properties": {
                            "name": _str(_JOB_ID_RE.pattern, 64),
                            "schedule": _str(_SCHEDULE_RE.pattern, 64),
                            "scope": {"type": "string", "enum": ["users", "groups", "both"]},
                            "enable_new": {"type": "boolean"},
                            "remove_vanished": _str(_REMOVE_VANISHED_RE.pattern, 64),
                        }},
                    },
                },
                "service_accounts": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "user": _str(_USERID_RE.pattern, 64),
                        "token": _str(_TOKEN_ID_RE.pattern, 64),
                        "role": _str(_PVE_ID_RE.pattern, 64),
                        "path": _str(_ACL_PATH_RE.pattern),
                        "privsep": {"type": "boolean"},
                        "name": _str(_ACCOUNT_NAME_RE.pattern, 31),
                    },
                }},
                "network": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "enabled": _enabled(),
                        "bond_miimon": {"type": "integer", "minimum": 0, "maximum": 10000},
                        "lacp_rate": {"type": "string", "enum": list(LACP_RATES)},
                        "rollback_seconds": {"type": "integer", "minimum": ROLLBACK_MIN,
                                             "maximum": ROLLBACK_MAX},
                    },
                },
            },
        },
    },
}
CONTEXT_SCHEMA_NAME = "nfv-host-baseline"


def referenced_secret_names(context_data):
    """Secret names a config context's host_baseline block references (the
    bootstrap pre-creates their records, create-only)."""
    hb = (context_data or {}).get(CONTEXT_KEY) if isinstance(context_data, dict) else None
    if not isinstance(hb, dict):
        return []
    names = []
    ad = hb.get("ad")
    if isinstance(ad, dict) and ad.get("enabled", True) is not False:
        names.append(ad.get("bind_password_secret") or DEFAULT_AD_BIND_SECRET)
    snmp = hb.get("snmp")
    if isinstance(snmp, dict) and snmp.get("enabled", True) is not False:
        if snmp.get("community_secret"):
            names.append(snmp["community_secret"])
        for user in snmp.get("v3_users") or ():
            entry = {"name": user} if isinstance(user, str) else user
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                names.append(entry.get("auth_secret") or f"snmpv3_{entry['name']}_auth")
                names.append(entry.get("priv_secret") or f"snmpv3_{entry['name']}_priv")
    return [n for n in dict.fromkeys(names) if isinstance(n, str) and _SECRET_NAME_RE.match(n)]
