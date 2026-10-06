"""
Pure rules for the Secret RECORDS the bootstrap job creates (decision #56):
which provider they use and where they point. Stdlib-only and Nautobot-free
so the defaults, the path join and the variable-name mapping are
unit-testable without a Nautobot install (tests/test_bootstrap_schema.py).

Nautobot's built-in providers and their parameters (verified in 2.4.30):
  text-file             {"path": "<absolute path>"}
  environment-variable  {"variable": "<NAME>"}

The defaults reproduce the composer layout (./secrets mounted at
/opt/nautobot/secrets in every Nautobot container); another deployment
picks another prefix or the environment-variable provider. The per-node
token records (answer service, Host Baseline job) are NOT built here: they
stay text-file under <prefix>/nodes, and the platform contract
(docs/platform-contract.md) requires that path, NAUTOBOT_SECRETS_PATH and
NFV_NODE_SECRETS_DIR to agree.
"""

import posixpath
import re

TEXT_FILE = "text-file"
ENVIRONMENT_VARIABLE = "environment-variable"
PROVIDERS = (TEXT_FILE, ENVIRONMENT_VARIABLE)

DEFAULT_PROVIDER = TEXT_FILE
DEFAULT_PATH_PREFIX = "/opt/nautobot/secrets"
DEFAULT_ENV_PREFIX = ""

# A prefix is the head of a variable name, so it obeys the same rule as a
# whole name (POSIX portable: upper-case letters, digits, '_', no leading
# digit). Always applied with fullmatch(): '$' alone would let a trailing
# newline through, and a config-context secret name can carry one.
ENV_PREFIX_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class SecretRecordError(ValueError):
    """The provider/prefix inputs, or a record name under them, are unusable."""


def _has_control_char(value):
    """A newline, tab, NUL or DEL in a path or a record name: nothing below
    could carry it (the UI inputs are single-line; the API and a config
    context are not)."""
    return any(c < " " or c == "\x7f" for c in value)


def normalize_secret_record_inputs(provider=None, path_prefix=None, env_prefix=None):
    """Apply the defaults to empty inputs and refuse anything else that is
    not usable. The job calls this once, before it writes anything: the API
    run (`setup.sh --with-nfv-jobs` posts `{"data": {}}`) delivers every
    input empty, so the defaults are applied here, not by the form.

    Both prefixes are checked whichever provider is chosen — one rule, no
    surprise later when the operator switches provider.

    Returns (provider, path_prefix, env_prefix) normalized: the path prefix
    without its trailing slash (so <prefix>/<name> never doubles it).
    """
    provider = (provider or "").strip() or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        raise SecretRecordError(
            f"Secrets provider {provider!r} is not supported — choose {TEXT_FILE} or {ENVIRONMENT_VARIABLE}"
        )
    path_prefix = (path_prefix or "").strip() or DEFAULT_PATH_PREFIX
    if not path_prefix.startswith("/"):
        raise SecretRecordError(
            f"text-file path prefix {path_prefix!r} must be an absolute path (start with /)"
        )
    # Nautobot's own text-file provider form refuses '..' anywhere in the
    # path; get_or_create(defaults=...) bypasses that form, so refuse it here.
    if ".." in path_prefix:
        raise SecretRecordError(
            f"text-file path prefix {path_prefix!r} must not contain '..' — Nautobot's text-file "
            "provider refuses such a path"
        )
    if _has_control_char(path_prefix):
        raise SecretRecordError(
            f"text-file path prefix {path_prefix!r} contains a control character (newline, tab, ...) — not allowed"
        )
    path_prefix = path_prefix.rstrip("/") or "/"
    env_prefix = (env_prefix or "").strip() or DEFAULT_ENV_PREFIX
    if env_prefix and not ENV_PREFIX_RE.fullmatch(env_prefix):
        raise SecretRecordError(
            f"environment-variable name prefix {env_prefix!r} must match {ENV_PREFIX_RE.pattern} "
            "(upper-case letters, digits and '_', not starting with a digit) or be empty"
        )
    return provider, path_prefix, env_prefix


def variable_name(name, env_prefix=""):
    """The environment variable a record resolves from: <prefix><NAME>, NAME
    = the record name upper-cased with '-' -> '_'
    (answer-service-admin-token -> ANSWER_SERVICE_ADMIN_TOKEN). Refuses a
    name the mapping cannot carry (a '.' or ' ' a config context put in a
    secret name, or a leading digit with no prefix in front of it) instead
    of inventing a second mapping that could collide.
    """
    variable = env_prefix + name.upper().replace("-", "_")
    if not ENV_PREFIX_RE.fullmatch(variable):
        raise SecretRecordError(
            f"Secret {name!r} cannot be an environment-variable record: {variable!r} is not a valid "
            "variable name (only letters, digits, '_' and '-' map, and the variable must not start "
            "with a digit — a name prefix such as NFV_ fixes that case) — rename it where the config "
            f"context references it, or use the {TEXT_FILE} provider"
        )
    return variable


def secret_record_defaults(name, provider=None, path_prefix=None, env_prefix=None, file_name=None):
    """The create-only defaults of ONE Secret record under the chosen provider:
    {"provider": "text-file", "parameters": {"path": "<prefix>/<name>"}} or
    {"provider": "environment-variable", "parameters": {"variable": "<PREFIX><NAME>"}}.
    The job passes the result as get_or_create(defaults=...), so an existing
    record — repointed by an admin or not — is never touched.

    file_name: the text-file name when the composer convention differs from
    the record name (the forge token: record answer-service-admin-token, file
    answer_service_admin_token — ./add-secret.sh takes the file name). The
    variable name always derives from the record name.
    """
    provider, path_prefix, env_prefix = normalize_secret_record_inputs(provider, path_prefix, env_prefix)
    for what, value in (("name", name), ("file name", file_name or name)):
        # '/' would escape the prefix; '..' Nautobot's provider form refuses;
        # a control character nothing can carry (hb's name rule lets one in).
        if not value or "/" in value or ".." in value or _has_control_char(value):
            raise SecretRecordError(
                f"Secret record {what} {value!r} must be non-empty and contain no '/', '..' or control character"
            )
    if provider == TEXT_FILE:
        return {"provider": TEXT_FILE, "parameters": {"path": posixpath.join(path_prefix, file_name or name)}}
    return {"provider": ENVIRONMENT_VARIABLE, "parameters": {"variable": variable_name(name, env_prefix)}}


def plan_secret_records(records, provider=None, path_prefix=None, env_prefix=None):
    """The defaults of EVERY record up front, {name: defaults}, so the job
    finds an unusable name before its first write. records: (name, file_name)
    pairs, file_name None for the convention. Refuses two records that would
    resolve from the same place — environment-variable: 'a-b' and 'a_b' both
    spell A_B (and names differ in case); text-file: the forge token's file
    name next to a context record of that name — because Nautobot would
    accept both and one value would silently serve two records.
    """
    provider, path_prefix, env_prefix = normalize_secret_record_inputs(provider, path_prefix, env_prefix)
    plan, owners = {}, {}
    what = "file" if provider == TEXT_FILE else "variable"
    for name, file_name in records:
        if name in plan:
            continue  # the same record listed twice is one record
        defaults = secret_record_defaults(name, provider, path_prefix, env_prefix, file_name=file_name)
        target = defaults["parameters"]["path" if provider == TEXT_FILE else "variable"]
        if target in owners:
            hint = f", or use the {TEXT_FILE} provider" if provider == ENVIRONMENT_VARIABLE else ""
            raise SecretRecordError(
                f"Secret records {owners[target]!r} and {name!r} both resolve from the same {what} "
                f"{target!r} — rename one where the config context references it{hint}"
            )
        owners[target] = name
        plan[name] = defaults
    return plan


def describe_secret_records(provider=None, path_prefix=None, env_prefix=None):
    """The one log line the job emits for its choice of provider and prefix."""
    provider, path_prefix, env_prefix = normalize_secret_record_inputs(provider, path_prefix, env_prefix)
    if provider == TEXT_FILE:
        return f"Secret records: provider {TEXT_FILE}, path prefix {path_prefix}"
    return f"Secret records: provider {ENVIRONMENT_VARIABLE}, variable name prefix {env_prefix or '(none)'}"
