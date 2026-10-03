"""
Answer-service client bits shared by the install-side jobs (Nautobot-free).

The service's install profiles bake into its image at build time, while the
jobs arrive through the Git sync — after every profile merge the two can
drift, and the symptom is a wasted boot cycle: the node boots the installer,
POSTs its identity, and gets `403 no install profile for DeviceType ...`.
The install job therefore asks the service first (`GET /info` lists the
baked-in profiles and the profile keys the build understands) and refuses
before touching a BMC when the answer is definitive. An unreachable service
is only a warning — the installing node, not the worker, is who must reach it.

Importable by file path for tests; `requests` is imported lazily.
"""

import re

INTEGRATION_NAME = "nfv-answer-service"

# The Device name becomes the installed node's hostname (<name>.<DOMAIN> in
# answer.toml): an RFC 1123 label — letters, digits, hyphen, 1-63 chars, no
# leading/trailing hyphen — and not all digits (the PVE installer rejects a
# numeric host). The answer service refuses anything else; keep in step with
# bmc/answer_service/app.py HOSTNAME_LABEL_RE.
HOSTNAME_LABEL_RE = re.compile(r"^(?![0-9]+\Z)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def is_hostname_label(name):
    """True when `name` can be the installed node's hostname label."""
    return isinstance(name, str) and bool(HOSTNAME_LABEL_RE.match(name))


# The Device role the bare-metal jobs act on (team convention, decision #43).
# The ObjectVar `query_params` only filter the UI dropdown — a job submitted
# through the REST API can name ANY Device — so the jobs re-check it in run()
# before touching a BMC. Keep in step with the answer service's NFV_ROLE
# (bmc/answer_service/app.py), which refuses any other role at identity time.
NFV_ROLE = "NFV"


def nfv_role_refusal(device, action):
    """None when `device` carries the NFV role; otherwise the refusal message
    (`action` says what was refused, e.g. "boot an installer")."""
    role = getattr(device, "role", None)
    role_name = getattr(role, "name", None) if role is not None else None
    if role_name == NFV_ROLE:
        return None
    return (
        f"{getattr(device, 'name', device)} has role {role_name!r}, not {NFV_ROLE!r} — "
        f"refusing to {action}; only {NFV_ROLE}-role Devices are bare-metal install "
        "targets (the job form filters on the role, but an API-submitted job can name "
        "any Device)"
    )

# Profile keys under `install` that older service builds silently ignore —
# a stale image would answer, but with a degraded layout (no data storage,
# no pinning). Keep in step with app.py's /info "profile_features".
PROFILE_FEATURE_KEYS = ("filter_match", "data_pool", "data_volume", "interface_name_pinning",
                        "serial_console")

# Config-context (host_baseline) keys firstboot renders (decision #55); keep
# in step with app.py's /info "firstboot_features".
FIRSTBOOT_FEATURE_KEYS = ("packages", "serial_console", "zfs_arc_max_bytes", "remove_subscription_nag")


def fetch_info(base_url, verify=False, timeout=10):
    """GET <base_url>/info -> dict, or None when unreachable/invalid."""
    import requests  # lazy: the pure helpers below must load without it

    try:
        response = requests.get(f"{base_url.rstrip('/')}/info", timeout=timeout, verify=verify)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def profile_feature_keys(profile):
    """The feature keys a profile actually uses (sorted)."""
    install = (profile or {}).get("install") or {}
    return sorted(key for key in PROFILE_FEATURE_KEYS if install.get(key))


def evaluate_profile_preflight(info, slug, features=(), base_url=""):
    """-> (verdict, message); verdict is 'ok', 'warn' (continue) or 'refuse'."""
    where = f"answer service at {base_url}" if base_url else "answer service"
    if info is None:
        return "warn", (
            f"{where} did not answer GET /info from this worker — profile preflight "
            "skipped (the installing node, not the worker, must reach it)"
        )
    profiles = info.get("profiles")
    if not isinstance(profiles, list):
        return "warn", (
            f"{where} predates the profile list in /info — cannot verify it carries "
            f"{slug!r}; rebuild it from the current main before relying on new profiles"
        )
    if slug not in profiles:
        return "refuse", (
            f"{where} has no install profile {slug!r} (it carries {sorted(profiles)}) — "
            "rebuild the answer-service image from the current main "
            "(docker compose --profile answer-service up -d --build answer-service) and re-run"
        )
    known = info.get("profile_features")
    if isinstance(known, list):
        missing = [feature for feature in features if feature not in known]
        if missing:
            return "refuse", (
                f"{where} does not support profile feature(s) {missing} used by {slug!r} — "
                "it would install a degraded layout; rebuild the service image from the "
                "current main and re-run"
            )
    detail = f" with features {list(features)}" if features else ""
    return "ok", f"{where} carries profile {slug!r}{detail}"


def firstboot_inputs_warning(info, context, base_url=""):
    """A warning (or None) when the Device's config context sets host_baseline
    firstboot inputs the answer service build does not render — a service
    image older than decision #55 answers, but silently skips them."""
    hb = (context or {}).get("host_baseline") if isinstance(context, dict) else None
    used = sorted(k for k in FIRSTBOOT_FEATURE_KEYS if isinstance(hb, dict) and hb.get(k) is not None)
    if not used or info is None:
        return None
    known = info.get("firstboot_features")
    missing = used if not isinstance(known, list) else [k for k in used if k not in known]
    if not missing:
        return None
    where = f"answer service at {base_url}" if base_url else "answer service"
    return (
        f"{where} does not render the host_baseline firstboot input(s) {missing} set in this "
        "Device's config context — the node would install without them (the Host Baseline job "
        "still ensures the packages); rebuild the service image from the current main"
    )
