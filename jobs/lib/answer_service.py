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

INTEGRATION_NAME = "nfv-answer-service"

# Profile keys under `install` that older service builds silently ignore —
# a stale image would answer, but with a degraded layout (no data storage,
# no pinning). Keep in step with app.py's /info "profile_features".
PROFILE_FEATURE_KEYS = ("filter_match", "data_pool", "data_volume", "interface_name_pinning")


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
