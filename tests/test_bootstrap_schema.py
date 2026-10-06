#!/usr/bin/env python3
"""
Tests for the bootstrap job's Secret RECORDS (decision #56): the pure rules
in jobs/lib/secret_records.py — loaded straight from its file path, no
Nautobot — and source-level gates on jobs/design/bootstrap_schema.py: the
three inputs exist and are optional, the defaults are the composer layout,
validation runs before any write (every record name planned up front, so a
name the provider cannot spell or two names that resolve from one variable
or file refuse before the first write), and all three Secret sites create
through the one helper (get_or_create defaults only — never an update of an
existing record). DocsCoverMessages pins every refusal text to
docs/getting-started.md §1 the way tests/test_host_baseline.py pins the
baseline's to the runbook.

Run:  python3 tests/test_bootstrap_schema.py
"""

import ast
import importlib.util
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = ROOT / "jobs" / "lib" / "secret_records.py"
JOB = ROOT / "jobs" / "design" / "bootstrap_schema.py"
DOCS = ROOT / "docs" / "getting-started.md"
RUNBOOK = ROOT / "docs" / "baremetal-install.md"
spec = importlib.util.spec_from_file_location("secret_records", MODULE)
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

TEXT_FILE = "text-file"
ENV_VAR = "environment-variable"
STANDARD_SECRET_NAMES = (
    "jumphost_console_password",
    "xcc_username",
    "xcc_password",
    "host_ssh_username",
    "host_ssh_password",
    "proxmox_token_id",
    "proxmox_token_secret",
    "pa_admin_password",
    "pa_authcode",
    "scm_registration_pin_id",
    "scm_registration_pin_value",
)


class Defaults(unittest.TestCase):
    def test_defaults_are_the_composer_layout(self):
        self.assertEqual(sr.DEFAULT_PROVIDER, TEXT_FILE)
        self.assertEqual(sr.DEFAULT_PATH_PREFIX, "/opt/nautobot/secrets")
        self.assertEqual(sr.DEFAULT_ENV_PREFIX, "")
        self.assertEqual(sr.PROVIDERS, (TEXT_FILE, ENV_VAR))

    def test_empty_inputs_take_the_defaults(self):
        expected = (TEXT_FILE, "/opt/nautobot/secrets", "")
        self.assertEqual(sr.normalize_secret_record_inputs(), expected)
        self.assertEqual(sr.normalize_secret_record_inputs(None, None, None), expected)
        self.assertEqual(sr.normalize_secret_record_inputs("", "", ""), expected)  # the API run
        self.assertEqual(sr.normalize_secret_record_inputs("  ", " ", "\t"), expected)

    def test_path_prefix_is_normalized(self):
        self.assertEqual(sr.normalize_secret_record_inputs(None, "/srv/secrets/", None)[1], "/srv/secrets")
        self.assertEqual(sr.normalize_secret_record_inputs(None, " /srv/secrets// ", None)[1], "/srv/secrets")
        self.assertEqual(sr.normalize_secret_record_inputs(None, "/", None)[1], "/")

    def test_env_prefix_accepts_portable_names(self):
        for prefix in ("NFV_", "_X9", "A", "NFV_SITE_"):
            self.assertEqual(sr.normalize_secret_record_inputs(ENV_VAR, None, prefix)[2], prefix)
        self.assertEqual(sr.normalize_secret_record_inputs(ENV_VAR, None, " NFV_ ")[2], "NFV_")


class Refusals(unittest.TestCase):
    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(sr.SecretRecordError, ValueError))

    def test_unknown_provider(self):
        for provider in ("vault", "TEXT-FILE", "text_file", "env"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.normalize_secret_record_inputs(provider)
            self.assertIn(provider, str(ctx.exception))
            self.assertIn("choose text-file or environment-variable", str(ctx.exception))

    def test_relative_path_prefix(self):
        for prefix in ("opt/nautobot/secrets", "secrets", "./secrets", "~/secrets", "C:\\secrets"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.normalize_secret_record_inputs(TEXT_FILE, prefix)
            self.assertIn("must be an absolute path", str(ctx.exception))

    def test_dot_dot_in_path_prefix(self):
        """Nautobot's text-file provider form refuses '..' anywhere in the
        path (substring, not segment); get_or_create bypasses the form."""
        for prefix in ("/opt/../secrets", "/opt/nautobot/..", "/opt/na..utobot"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.normalize_secret_record_inputs(TEXT_FILE, prefix)
            self.assertIn("must not contain '..'", str(ctx.exception))
        self.assertEqual(sr.normalize_secret_record_inputs(TEXT_FILE, "/opt/n.autobot/.secrets")[1],
                         "/opt/n.autobot/.secrets")

    def test_control_character_in_path_prefix(self):
        # strip() takes a trailing newline; an embedded one (API input) must refuse
        for prefix in ("/opt/na\nutobot", "/opt/nautobot\t/secrets", "/opt/\x00"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.normalize_secret_record_inputs(TEXT_FILE, prefix)
            self.assertIn("contains a control character (newline, tab, ...) — not allowed", str(ctx.exception))

    def test_malformed_env_prefix(self):
        for prefix in ("nfv_", "1NFV_", "NFV-", "NF V", "NFV.", "nfv", "NFV_$"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.normalize_secret_record_inputs(ENV_VAR, None, prefix)
            self.assertIn("or be empty", str(ctx.exception))
        with self.assertRaises(sr.SecretRecordError):  # an embedded newline survives strip()
            sr.normalize_secret_record_inputs(ENV_VAR, None, "NFV\n_")

    def test_inputs_are_validated_regardless_of_provider(self):
        """A junk prefix for the provider NOT in use is still refused — one
        rule, no surprise later when the operator switches provider."""
        with self.assertRaises(sr.SecretRecordError):
            sr.normalize_secret_record_inputs(ENV_VAR, "relative", "NFV_")
        with self.assertRaises(sr.SecretRecordError):
            sr.normalize_secret_record_inputs(TEXT_FILE, "/x", "bad prefix")

    def test_record_names(self):
        for name in ("", None, "a/b", "/abs", "a..b", "..", "foo\n", "a\tb"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.secret_record_defaults(name)
            self.assertIn("must be non-empty and contain no '/', '..' or control character", str(ctx.exception))
        with self.assertRaises(sr.SecretRecordError):
            sr.secret_record_defaults("ok", file_name="nodes/x")
        with self.assertRaises(sr.SecretRecordError):
            sr.secret_record_defaults("ok", file_name="x..y")
        # hb's _SECRET_NAME_RE ('$', not fullmatch) lets 'foo\n' arrive from a
        # config context — the env-var mapping must not emit 'FOO\n' either
        with self.assertRaises(sr.SecretRecordError):
            sr.variable_name("foo\n")
        with self.assertRaises(sr.SecretRecordError):
            sr.secret_record_defaults("foo\n", ENV_VAR)

    def test_env_var_refuses_a_name_it_cannot_spell(self):
        # hb's _SECRET_NAME_RE lets a config context name a secret with '.'
        # or ' '; there is no variable spelling for those — refuse, name it.
        for name in ("snmpv3_ops.user_auth", "site a community", "9lives"):
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.secret_record_defaults(name, ENV_VAR)
            self.assertIn(repr(name), str(ctx.exception))
            self.assertIn("use the text-file provider", str(ctx.exception))
            # every character of 9LIVES is in the allowed set — the message
            # must say the leading digit is the problem and that a prefix fixes it
            self.assertIn("must not start with a digit — a name prefix such as NFV_ fixes that case",
                          str(ctx.exception))
        self.assertEqual(sr.secret_record_defaults("9lives", ENV_VAR, None, "NFV_")["parameters"]["variable"],
                         "NFV_9LIVES")
        # ...while text-file carries them exactly as before
        self.assertEqual(sr.secret_record_defaults("snmpv3_ops.user_auth", TEXT_FILE)["parameters"]["path"],
                         "/opt/nautobot/secrets/snmpv3_ops.user_auth")
        self.assertEqual(sr.secret_record_defaults("site a community")["parameters"]["path"],
                         "/opt/nautobot/secrets/site a community")


class TextFileRecords(unittest.TestCase):
    def test_default_path_join(self):
        self.assertEqual(
            sr.secret_record_defaults("xcc_password"),
            {"provider": TEXT_FILE, "parameters": {"path": "/opt/nautobot/secrets/xcc_password"}},
        )
        self.assertEqual(
            sr.secret_record_defaults("xcc_password", TEXT_FILE, "/opt/nautobot/secrets", ""),
            {"provider": TEXT_FILE, "parameters": {"path": "/opt/nautobot/secrets/xcc_password"}},
        )

    def test_other_prefix_and_trailing_slash(self):
        self.assertEqual(sr.secret_record_defaults("xcc_password", TEXT_FILE, "/srv/nb/")["parameters"]["path"],
                         "/srv/nb/xcc_password")
        self.assertEqual(sr.secret_record_defaults("x", TEXT_FILE, "/")["parameters"]["path"], "/x")

    def test_file_name_override(self):
        """The forge token: record answer-service-admin-token, file
        answer_service_admin_token (the composer's ./add-secret.sh name)."""
        rec = sr.secret_record_defaults("answer-service-admin-token", file_name="answer_service_admin_token")
        self.assertEqual(rec["parameters"]["path"], "/opt/nautobot/secrets/answer_service_admin_token")

    def test_no_variable_key(self):
        self.assertEqual(set(sr.secret_record_defaults("x")["parameters"]), {"path"})

    def test_env_prefix_is_ignored(self):
        self.assertEqual(sr.secret_record_defaults("x", TEXT_FILE, None, "NFV_")["parameters"], {"path": "/opt/nautobot/secrets/x"})


class EnvironmentVariableRecords(unittest.TestCase):
    def test_name_mapping_without_prefix(self):
        self.assertEqual(
            sr.secret_record_defaults("answer-service-admin-token", ENV_VAR),
            {"provider": ENV_VAR, "parameters": {"variable": "ANSWER_SERVICE_ADMIN_TOKEN"}},
        )
        self.assertEqual(sr.secret_record_defaults("xcc_password", ENV_VAR, "", "")["parameters"]["variable"],
                         "XCC_PASSWORD")

    def test_name_mapping_with_prefix(self):
        self.assertEqual(sr.secret_record_defaults("answer-service-admin-token", ENV_VAR, None, "NFV_")["parameters"]["variable"],
                         "NFV_ANSWER_SERVICE_ADMIN_TOKEN")
        self.assertEqual(sr.secret_record_defaults("xcc_password", ENV_VAR, None, "NFV_")["parameters"]["variable"],
                         "NFV_XCC_PASSWORD")
        self.assertEqual(sr.variable_name("snmpv3_ops_auth", "SITE1_"), "SITE1_SNMPV3_OPS_AUTH")

    def test_variable_comes_from_the_record_name_not_the_file_name(self):
        rec = sr.secret_record_defaults("answer-service-admin-token", ENV_VAR, file_name="answer_service_admin_token")
        self.assertEqual(rec["parameters"], {"variable": "ANSWER_SERVICE_ADMIN_TOKEN"})

    def test_no_path_key_and_path_prefix_ignored(self):
        rec = sr.secret_record_defaults("xcc_password", ENV_VAR, "/elsewhere", "")
        self.assertEqual(set(rec["parameters"]), {"variable"})

    def test_every_standard_name_maps(self):
        seen = set()
        for name in STANDARD_SECRET_NAMES:
            variable = sr.secret_record_defaults(name, ENV_VAR, None, "NFV_")["parameters"]["variable"]
            self.assertRegex(variable, r"^NFV_[A-Z0-9_]+$")
            seen.add(variable)
        self.assertEqual(len(seen), len(STANDARD_SECRET_NAMES), "variable-name collision")


class Plan(unittest.TestCase):
    FORGE = ("answer-service-admin-token", "answer_service_admin_token")

    def test_plan_is_every_record_s_defaults(self):
        plan = sr.plan_secret_records([self.FORGE, ("xcc_password", None), ("xcc_password", None)])
        self.assertEqual(list(plan), ["answer-service-admin-token", "xcc_password"])
        self.assertEqual(plan["answer-service-admin-token"]["parameters"]["path"],
                         "/opt/nautobot/secrets/answer_service_admin_token")
        self.assertEqual(plan["xcc_password"], sr.secret_record_defaults("xcc_password"))
        plan = sr.plan_secret_records([self.FORGE, ("xcc_password", None)], ENV_VAR, None, "NFV_")
        self.assertEqual([d["parameters"]["variable"] for d in plan.values()],
                         ["NFV_ANSWER_SERVICE_ADMIN_TOKEN", "NFV_XCC_PASSWORD"])

    def test_env_var_refuses_two_names_that_spell_one_variable(self):
        for names in (("snmpv3_ops_auth", "snmpv3_ops-auth"), ("xcc_password", "XCC_PASSWORD"),
                      ("a-b", "a_b")):
            records = [(name, None) for name in names]
            with self.assertRaises(sr.SecretRecordError) as ctx:
                sr.plan_secret_records(records, ENV_VAR)
            message = str(ctx.exception)
            self.assertIn(repr(names[0]), message)
            self.assertIn(repr(names[1]), message)
            self.assertIn("both resolve from the same variable", message)
            self.assertIn("or use the text-file provider", message)
            # ...and text-file carries both (distinct files)
            self.assertEqual(len(sr.plan_secret_records(records, TEXT_FILE)), 2)

    def test_text_file_refuses_a_record_on_the_forge_token_s_file(self):
        with self.assertRaises(sr.SecretRecordError) as ctx:
            sr.plan_secret_records([self.FORGE, ("answer_service_admin_token", None)])
        self.assertIn("both resolve from the same file '/opt/nautobot/secrets/answer_service_admin_token'",
                      str(ctx.exception))
        self.assertNotIn("text-file provider", str(ctx.exception))

    def test_plan_refuses_like_the_normalizer(self):
        with self.assertRaises(sr.SecretRecordError):
            sr.plan_secret_records([("x", None)], "vault")
        with self.assertRaises(sr.SecretRecordError):
            sr.plan_secret_records([("snmpv3_ops.user_auth", None)], ENV_VAR)


class LogLine(unittest.TestCase):
    def test_text_file(self):
        self.assertEqual(sr.describe_secret_records(), "Secret records: provider text-file, path prefix /opt/nautobot/secrets")
        self.assertEqual(sr.describe_secret_records(TEXT_FILE, "/srv/nb/", None),
                         "Secret records: provider text-file, path prefix /srv/nb")

    def test_environment_variable(self):
        self.assertEqual(sr.describe_secret_records(ENV_VAR, None, "NFV_"),
                         "Secret records: provider environment-variable, variable name prefix NFV_")
        self.assertEqual(sr.describe_secret_records(ENV_VAR),
                         "Secret records: provider environment-variable, variable name prefix (none)")

    def test_refuses_like_the_normalizer(self):
        with self.assertRaises(sr.SecretRecordError):
            sr.describe_secret_records("vault")


# ---- source-level gates on the job -----------------------------------------
JOB_SRC = JOB.read_text()
JOB_AST = ast.parse(JOB_SRC)


def _job_class():
    return next(n for n in JOB_AST.body if isinstance(n, ast.ClassDef) and n.name == "BootstrapNfvSchema")


def _class_inputs():
    """{name: Call} for every <name> = XVar(...) at class level."""
    out = {}
    for node in _job_class().body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            out[node.targets[0].id] = node.value
    return out


def _kw(call, name):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _module_constant(name):
    node = next(n for n in JOB_AST.body if isinstance(n, ast.Assign) and n.targets[0].id == name)
    return ast.literal_eval(node.value)


class JobInputs(unittest.TestCase):
    def test_three_optional_inputs_with_the_lib_defaults(self):
        inputs = _class_inputs()
        for name, var, default in (
            ("secrets_provider", "ChoiceVar", "DEFAULT_PROVIDER"),
            ("secrets_path_prefix", "StringVar", "DEFAULT_PATH_PREFIX"),
            ("secrets_env_prefix", "StringVar", "DEFAULT_ENV_PREFIX"),
        ):
            call = inputs[name]
            self.assertEqual(call.func.id, var, name)
            self.assertEqual(ast.literal_eval(_kw(call, "required")), False, f"{name} must be optional (API run posts {{}})")
            self.assertIsInstance(_kw(call, "default"), ast.Name, name)
            self.assertEqual(_kw(call, "default").id, default, name)
            self.assertIsNotNone(_kw(call, "label"), name)
            self.assertIsNotNone(_kw(call, "description"), name)

    def test_provider_choices_are_the_two_providers(self):
        choices = _kw(_class_inputs()["secrets_provider"], "choices")
        slugs = [elt.elts[0].id for elt in choices.elts]
        self.assertEqual(slugs, ["TEXT_FILE", "ENVIRONMENT_VARIABLE"])

    def test_defaults_and_helpers_are_imported_from_the_lib(self):
        imp = next(n for n in JOB_AST.body if isinstance(n, ast.ImportFrom) and n.module == "lib.secret_records")
        self.assertEqual(imp.level, 2, "from ..lib.secret_records import ...")
        names = {alias.name for alias in imp.names}
        self.assertTrue({"DEFAULT_PROVIDER", "DEFAULT_PATH_PREFIX", "DEFAULT_ENV_PREFIX", "TEXT_FILE",
                         "ENVIRONMENT_VARIABLE", "SecretRecordError", "normalize_secret_record_inputs",
                         "plan_secret_records", "secret_record_defaults", "describe_secret_records"} <= names, names)

    def test_prefix_descriptions_do_not_say_ignored(self):
        """Both prefixes are validated whichever provider is chosen, so the
        form must not promise that the unused one is ignored."""
        inputs = _class_inputs()
        for name, expected in (("secrets_path_prefix", "Used only for text-file records (always validated)."),
                               ("secrets_env_prefix", "Used only for environment-variable records (always validated).")):
            description = ast.literal_eval(_kw(inputs[name], "description"))
            self.assertIn(expected, description, name)
            self.assertNotIn("Ignored", description, name)

    def test_run_signature_carries_the_three_inputs(self):
        self.assertIn("    def run(self, secrets_provider=None, secrets_path_prefix=None, secrets_env_prefix=None):",
                      JOB_SRC)

    def test_docstring_and_description_mention_the_inputs(self):
        doc = ast.get_docstring(JOB_AST)
        for name in ("secrets_provider", "secrets_path_prefix", "secrets_env_prefix"):
            self.assertIn(name, doc)
        meta = next(n for n in _job_class().body if isinstance(n, ast.ClassDef) and n.name == "Meta")
        description = ast.literal_eval(next(n.value for n in meta.body if isinstance(n, ast.Assign)
                                            and n.targets[0].id == "description"))
        self.assertIn("Secrets provider / path prefix / variable-name prefix", description)
        self.assertIn("never", description)


class JobSecretSites(unittest.TestCase):
    def test_no_hard_coded_provider_or_path(self):
        self.assertNotIn('"provider": "text-file"', JOB_SRC)
        self.assertNotIn("/opt/nautobot/secrets/", JOB_SRC)
        self.assertNotIn("/opt/nautobot/secrets", JOB_SRC.replace(ast.get_docstring(JOB_AST), ""),
                         "the default lives in jobs/lib/secret_records.py only")

    def test_all_three_sites_create_through_the_helper(self):
        flat = re.sub(r"\s+", " ", JOB_SRC)
        sites = re.findall(r"Secret\.objects\.get_or_create\( name=([^,]+), defaults=self\._secret_defaults\(", flat)
        self.assertEqual(len(sites), 3, sites)
        self.assertEqual(flat.count("Secret.objects.get_or_create("), 3)
        self.assertEqual(sites[0], "FORGE_ADMIN_TOKEN_SECRET")
        self.assertEqual(sites[1:], ["secret_name", "secret_name"])

    def test_create_only(self):
        """get_or_create(defaults=...) only: no update path for Secret records."""
        self.assertNotIn("Secret.objects.update_or_create", JOB_SRC)
        self.assertNotRegex(JOB_SRC, r"Secret\.objects\.(filter|get)\([^\n]*\)\.update\(")
        self.assertNotRegex(JOB_SRC, r"forge_secret\.(provider|parameters)\s*=")
        self.assertNotRegex(JOB_SRC, r"_secret\.save\(|secret\.validated_save\(")

    def test_forge_token_keeps_its_composer_file_name(self):
        self.assertEqual(_module_constant("FORGE_ADMIN_TOKEN_SECRET"), "answer-service-admin-token")
        self.assertEqual(_module_constant("FORGE_ADMIN_TOKEN_FILE"), "answer_service_admin_token")
        flat = re.sub(r"\s+", " ", JOB_SRC)
        self.assertIn("name=FORGE_ADMIN_TOKEN_SECRET, defaults=self._secret_defaults("
                      "FORGE_ADMIN_TOKEN_SECRET, file_name=FORGE_ADMIN_TOKEN_FILE)", flat)
        rec = sr.secret_record_defaults(_module_constant("FORGE_ADMIN_TOKEN_SECRET"),
                                        file_name=_module_constant("FORGE_ADMIN_TOKEN_FILE"))
        self.assertEqual(rec["parameters"]["path"], "/opt/nautobot/secrets/answer_service_admin_token")

    def test_standard_list_unchanged(self):
        self.assertEqual(_module_constant("STANDARD_SECRET_NAMES"), STANDARD_SECRET_NAMES)

    def test_helper_forwards_the_normalized_inputs(self):
        self.assertIn("return secret_record_defaults(name, provider, path_prefix, env_prefix, file_name=file_name)",
                      JOB_SRC)


class JobFailsClosedBeforeAnyWrite(unittest.TestCase):
    def test_validation_precedes_every_write_in_run(self):
        run = JOB_SRC[JOB_SRC.index("    def run(self"):]
        gate = run.index("normalize_secret_record_inputs(")
        for first_write in ("ContentType.objects.get(", ".objects.get_or_create(", "validated_save(", ".add("):
            self.assertLess(gate, run.index(first_write), first_write)
        self.assertLess(run.index("except SecretRecordError"), run.index("ContentType.objects.get("))
        self.assertLess(run.index("describe_secret_records("), run.index("ContentType.objects.get("))

    def test_every_record_name_is_checked_up_front(self):
        """The env-var provider cannot spell some context-referenced names;
        the job finds out before the first write, not mid-run."""
        run = re.sub(r"\s+", " ", JOB_SRC[JOB_SRC.index("    def run(self"):JOB_SRC.index("        device_ct = ContentType")])
        self.assertIn("plan_secret_records( [(FORGE_ADMIN_TOKEN_SECRET, FORGE_ADMIN_TOKEN_FILE), "
                      "*((name, None) for name in (*STANDARD_SECRET_NAMES, *self._baseline_secret_names()))], "
                      "*self._secret_inputs, )", run)
        self.assertEqual(JOB_SRC.count("self._baseline_secret_names()"), 2, "preflight + the host-baseline site")


MESSAGE_FRAGMENTS = [
    # the inputs (normalize_secret_record_inputs)
    "is not supported — choose text-file or environment-variable",
    "must be an absolute path (start with /)",
    "must not contain '..' — Nautobot's text-file provider refuses such a path",
    "contains a control character (newline, tab, ...) — not allowed",
    "(upper-case letters, digits and '_', not starting with a digit) or be empty",
    # the record names (variable_name, secret_record_defaults, plan_secret_records)
    "cannot be an environment-variable record",
    "(only letters, digits, '_' and '-' map, and the variable must not start with a digit "
    "— a name prefix such as NFV_ fixes that case)",
    "rename it where the config context references it, or use the text-file provider",
    "must be non-empty and contain no '/', '..' or control character",
    "both resolve from the same variable",
    "both resolve from the same file",
    "rename one where the config context references it, or use the text-file provider",
]


def _flatten(text):
    """Joined f-string pieces / wrapped lines -> one comparable string."""
    text = re.sub(r'"\s*\n\s*f?"', "", text)  # adjacent string literals across lines
    text = re.sub(r"\s+", " ", text)
    return text


class DocsCoverMessages(unittest.TestCase):
    def test_every_refusal_is_in_the_code_and_getting_started(self):
        """Every refusal text is quoted exactly in docs/getting-started.md §1
        AND in the install runbook's troubleshooting table, so a reworded
        message cannot silently orphan its rows (or the reverse)."""
        code = _flatten(MODULE.read_text())
        code = code.replace("{TEXT_FILE}", "text-file").replace("{ENVIRONMENT_VARIABLE}", "environment-variable")
        code = code.replace("{ENV_PREFIX_RE.pattern}", sr.ENV_PREFIX_RE.pattern)
        code = code.replace("the same {what} ", "the same variable ").replace("it{hint}", "it, or use the text-file provider")
        code += " both resolve from the same file"  # {what} is 'file' under text-file
        docs = _flatten(DOCS.read_text())
        runbook = _flatten(RUNBOOK.read_text())
        missing_code = [m for m in MESSAGE_FRAGMENTS if m not in code]
        missing_docs = [m for m in MESSAGE_FRAGMENTS if m not in docs]
        missing_runbook = [m for m in MESSAGE_FRAGMENTS if m not in runbook]
        self.assertEqual(missing_code, [], "fragments no longer in jobs/lib/secret_records.py")
        self.assertEqual(missing_docs, [], "fragments missing from docs/getting-started.md §1")
        self.assertEqual(missing_runbook, [], "fragments missing from docs/baremetal-install.md troubleshooting")


class DocsCoverTheInputs(unittest.TestCase):
    def test_getting_started_names_the_inputs_and_the_contract(self):
        docs = DOCS.read_text()
        for fragment in (
            "Secrets provider", "text-file path prefix", "environment-variable name prefix",
            "platform-contract.md", "NAUTOBOT_SECRETS_PATH", "NFV_NODE_SECRETS_DIR",
            "NFV_XCC_PASSWORD", "/opt/nautobot/secrets/nodes",
        ):
            self.assertIn(fragment, docs, fragment)


if __name__ == "__main__":
    unittest.main(verbosity=1)
