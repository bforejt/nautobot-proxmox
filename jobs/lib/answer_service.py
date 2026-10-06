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

Decision #56 puts a version handshake in front of that: the service states
its version and the oldest jobs it accepts (`/info` `version`,
`min_jobs_version`); the jobs carry JOBS_VERSION and the oldest service they
accept (jobs/lib/version.py). Out of step = refuse, with the fix — the
composer pulls the service at a tag (ANSWER_SERVICE_VERSION), so every stale
message says "pull or rebuild", never "rebuild from main".

Importable by file path for tests; `requests` is imported lazily.
"""

import re

try:
    from .version import JOBS_VERSION, MIN_ANSWER_SERVICE_VERSION, VERSION_RE, parse_version
except ImportError:  # loaded by file path (tests): no package, so load the sibling the same way
    import importlib.util as _importlib_util
    from pathlib import Path as _Path

    _spec = _importlib_util.spec_from_file_location("nfv_jobs_version", _Path(__file__).with_name("version.py"))
    _version = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_version)
    JOBS_VERSION = _version.JOBS_VERSION
    MIN_ANSWER_SERVICE_VERSION = _version.MIN_ANSWER_SERVICE_VERSION
    VERSION_RE = _version.VERSION_RE
    parse_version = _version.parse_version

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


def composer_fix(version=None):
    """The one sentence every stale-service message ends with: how a deployed
    service moves forward. The composer pulls a published image pinned by
    ANSWER_SERVICE_VERSION (decision #56), so "rebuild from main" is wrong
    advice there; a checkout (ANSWER_SERVICE_BUILD_CONTEXT) still rebuilds.
    `version` names the tag to pin; None = whichever carries the missing
    piece. Keep the fixed fragments verbatim — the docs test pins them."""
    pin = f"to v{version} or newer" if version else "to a tag carrying it"
    return (
        f"on the composer, pull or rebuild the service (set ANSWER_SERVICE_VERSION {pin} in .env, "
        "then docker compose --profile answer-service pull answer-service && docker compose "
        "--profile answer-service up -d answer-service; a checkout rebuilds it with docker compose "
        "--profile answer-service up -d --build answer-service)"
    )


def _where(base_url):
    return f"answer service at {base_url}" if base_url else "answer service"


def version_handshake(info, base_url=""):
    """-> (verdict, message); verdict is 'ok', 'warn' (continue) or 'refuse'.

    The service's `/info` `version` must be >= MIN_ANSWER_SERVICE_VERSION and
    its `min_jobs_version` (when present) must be <= JOBS_VERSION. Anything
    the jobs cannot parse counts as too old (fail closed): a service that
    predates the handshake reports no version at all. None (unreachable from
    the worker) is only a warning — same policy as the profile preflight: the
    installing node, not the worker, is who must reach the service."""
    where = _where(base_url)
    if info is None:
        return "warn", (
            f"{where} did not answer GET /info from this worker — version handshake and profile "
            "preflight skipped (the installing node, not the worker, must reach it)"
        )
    raw = info.get("version") if isinstance(info, dict) else None
    service = parse_version(raw)
    if service is None:
        return "refuse", (
            f"{where} reports no usable version in GET /info ({raw!r}) — it predates the version "
            f"handshake; these jobs (version {JOBS_VERSION}) require answer service "
            f"{MIN_ANSWER_SERVICE_VERSION} or newer — {composer_fix(MIN_ANSWER_SERVICE_VERSION)} and re-run"
        )
    if service < parse_version(MIN_ANSWER_SERVICE_VERSION):
        return "refuse", (
            f"{where} is version {raw}, older than the {MIN_ANSWER_SERVICE_VERSION} these jobs "
            f"(version {JOBS_VERSION}) require — {composer_fix(MIN_ANSWER_SERVICE_VERSION)} and re-run"
        )
    if "min_jobs_version" in info:
        min_jobs_raw = info["min_jobs_version"]
        min_jobs = parse_version(min_jobs_raw)
        if min_jobs is None:
            return "refuse", (
                f"{where} (version {raw}) reports no usable min_jobs_version in GET /info "
                f"({min_jobs_raw!r}) — these jobs (version {JOBS_VERSION}) cannot tell whether it accepts "
                f"them — {composer_fix(MIN_ANSWER_SERVICE_VERSION)} and re-run"
            )
        if min_jobs > parse_version(JOBS_VERSION):
            return "refuse", (
                f"{where} (version {raw}) requires jobs version {min_jobs_raw} or newer, but these jobs "
                f"are version {JOBS_VERSION} — in Nautobot, sync the nautobot-proxmox Git repository "
                "(Extensibility → Git Repositories → Sync) and re-run"
            )
        accepts = f", accepts jobs {min_jobs_raw} or newer"
    else:
        accepts = ""
    return "ok", f"{where} is version {raw}{accepts} (these jobs: {JOBS_VERSION})"


def fetch_info(base_url, verify=False, timeout=10):
    """GET <base_url>/info -> the JSON object, or None ONLY when no response
    arrived (the worker cannot reach the service: the one case that is a
    warning). Any response that is not a usable JSON object — a 404 from a
    build older than /info itself, a proxy's error page, a bare list — comes
    back as a dict without `version`, so version_handshake refuses it: a
    service the worker reached but that cannot state its version is too old,
    not unreachable. (Same line prepare_media.py draws: it .json()s the
    response and hands whatever came back to the handshake.)"""
    import requests  # lazy: the pure helpers below must load without it

    try:
        response = requests.get(f"{base_url.rstrip('/')}/info", timeout=timeout, verify=verify)
    except requests.RequestException:
        return None
    try:
        data = response.json()
    except ValueError:  # requests' JSONDecodeError is one; an HTML error page lands here
        return {}
    return data if isinstance(data, dict) else {}


def profile_feature_keys(profile):
    """The feature keys a profile actually uses (sorted)."""
    install = (profile or {}).get("install") or {}
    return sorted(key for key in PROFILE_FEATURE_KEYS if install.get(key))


def evaluate_profile_preflight(info, slug, features=(), base_url=""):
    """-> (verdict, message); verdict is 'ok', 'warn' (continue) or 'refuse'.

    The version handshake runs FIRST and its verdict comes back unchanged
    when it is not 'ok' — the refusal, or the single unreachable warning —
    so the install job gets both checks through this one call. Only a
    service that passed the handshake has its profile list inspected."""
    verdict, message = version_handshake(info, base_url)
    if verdict != "ok":
        return verdict, message
    where = _where(base_url)
    version = info.get("version")
    profiles = info.get("profiles")
    if not isinstance(profiles, list):
        return "warn", (
            f"{where} predates the profile list in GET /info — cannot verify it carries "
            f"{slug!r}; {composer_fix()} before relying on new profiles"
        )
    if slug not in profiles:
        return "refuse", (
            f"{where} has no install profile {slug!r} (it carries {sorted(profiles)}) — profiles "
            f"bake into the service image at build; {composer_fix()} and re-run"
        )
    known = info.get("profile_features")
    if isinstance(known, list):
        missing = [feature for feature in features if feature not in known]
        if missing:
            return "refuse", (
                f"{where} does not support profile feature(s) {missing} used by {slug!r} — "
                f"it would install a degraded layout; {composer_fix()} and re-run"
            )
    detail = f" with features {list(features)}" if features else ""
    return "ok", f"{where} (version {version}) carries profile {slug!r}{detail}"


def firstboot_inputs_warning(info, context, base_url=""):
    """A warning (or None) when the Device's config context sets host_baseline
    firstboot inputs the answer service build does not render — a service
    image older than decision #55 answers, but silently skips them.

    Only reached after a passing version handshake (the install job calls
    evaluate_profile_preflight first and stops on its refusal), so `info`
    here is from a service these jobs accept; the check stays because a
    build can carry the version and still predate a firstboot input."""
    hb = (context or {}).get("host_baseline") if isinstance(context, dict) else None
    used = sorted(k for k in FIRSTBOOT_FEATURE_KEYS if isinstance(hb, dict) and hb.get(k) is not None)
    if not used or info is None:
        return None
    known = info.get("firstboot_features")
    missing = used if not isinstance(known, list) else [k for k in used if k not in known]
    if not missing:
        return None
    return (
        f"{_where(base_url)} does not render the host_baseline firstboot input(s) {missing} set in this "
        "Device's config context — the node would install without them (the Host Baseline job "
        f"still ensures the packages); {composer_fix()}"
    )
