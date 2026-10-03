#!/usr/bin/env python3
"""
Unit tests for jobs/lib/answer_service.py — the install job's answer-service
profile preflight — and for the answer service itself
(bmc/answer_service/app.py): the install-NIC derivation through the SoT's
bridge/LAG model (decision #55, kept equal to jobs/lib/host_baseline.py's),
the REST calls behind it, the pin-mapping guard, the install.serial_console
profile key and the host_baseline config-context firstboot inputs.

Stdlib-only: fastapi/jinja2/requests are stubbed when absent; the parts that
render templates (or run proxmox-auto-install-assistant) need the real ones
and run in the answer-service image.

Run:  python3 tests/test_answer_service.py
"""

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "answer_service.py"
spec = importlib.util.spec_from_file_location("answer_service", MODULE)
asvc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asvc)

SLUG = "thinkedge-se455-v3"
FEATURES = ["data_volume", "interface_name_pinning"]
URL = "https://answer-service:8800"


class Preflight(unittest.TestCase):
    def test_unreachable_is_a_warning(self):
        verdict, msg = asvc.evaluate_profile_preflight(None, SLUG, FEATURES, URL)
        self.assertEqual(verdict, "warn")
        self.assertIn("did not answer", msg)

    def test_old_service_without_profile_list_warns(self):
        verdict, msg = asvc.evaluate_profile_preflight({"public_url": URL}, SLUG, FEATURES, URL)
        self.assertEqual(verdict, "warn")
        self.assertIn("predates", msg)

    def test_missing_profile_refuses_with_the_rebuild_hint(self):
        info = {"profiles": ["nested-lab-node", "nuc", "thinksystem-se350"]}
        verdict, msg = asvc.evaluate_profile_preflight(info, SLUG, FEATURES, URL)
        self.assertEqual(verdict, "refuse")
        self.assertIn("no install profile 'thinkedge-se455-v3'", msg)
        self.assertIn("--build answer-service", msg)

    def test_missing_feature_refuses(self):
        info = {"profiles": [SLUG], "profile_features": ["filter_match", "data_pool"]}
        verdict, msg = asvc.evaluate_profile_preflight(info, SLUG, FEATURES, URL)
        self.assertEqual(verdict, "refuse")
        self.assertIn("data_volume", msg)

    def test_current_service_is_ok(self):
        info = {"profiles": [SLUG, "nuc"], "profile_features": list(asvc.PROFILE_FEATURE_KEYS)}
        verdict, msg = asvc.evaluate_profile_preflight(info, SLUG, FEATURES, URL)
        self.assertEqual(verdict, "ok")
        self.assertIn(SLUG, msg)

    def test_profile_without_features_needs_no_feature_list(self):
        info = {"profiles": ["nuc"]}
        self.assertEqual(asvc.evaluate_profile_preflight(info, "nuc", [], URL)[0], "ok")


class FeatureKeys(unittest.TestCase):
    def test_detects_used_keys(self):
        profile = {"install": {"filesystem": "ext4", "data_volume": {"vg": "datastore"},
                               "interface_name_pinning": True, "filter_match": ""}}
        self.assertEqual(asvc.profile_feature_keys(profile), ["data_volume", "interface_name_pinning"])

    def test_empty_profile(self):
        self.assertEqual(asvc.profile_feature_keys({}), [])


class HostnameLabel(unittest.TestCase):
    """The Device name becomes the node's hostname (<name>.<DOMAIN>)."""

    def test_valid_labels(self):
        for name in ("n1", "NUC-01", "pve-se455-01", "x", "1n", "a" * 63):
            self.assertTrue(asvc.is_hostname_label(name), name)

    def test_invalid_labels_are_refused(self):
        for name in (
            'n1.x"\nroot-ssh-keys = ["ssh-ed25519 attacker"]\n#',  # TOML injection
            "NFV Lab 1", "n1.nfv.lab", "n_1", "-n1", "n1-", "123",
            "a" * 64, "", None, "n1\n", "nüc",
        ):
            self.assertFalse(asvc.is_hostname_label(name), repr(name))

    def test_regex_matches_the_answer_service_copy(self):
        app = (MODULE.parents[2] / "bmc" / "answer_service" / "app.py").read_text()
        self.assertIn(f"HOSTNAME_LABEL_RE = re.compile(r\"{asvc.HOSTNAME_LABEL_RE.pattern}\")", app)


class NfvRoleGate(unittest.TestCase):
    """F38: the bare-metal jobs re-check the NFV role server-side — the
    ObjectVar query_params only filter the UI dropdown."""

    @staticmethod
    def _device(role_name, name="n1"):
        from types import SimpleNamespace

        role = None if role_name is None else SimpleNamespace(name=role_name)
        return SimpleNamespace(name=name, role=role)

    def test_nfv_role_passes(self):
        self.assertIsNone(asvc.nfv_role_refusal(self._device("NFV"), "boot an installer"))

    def test_other_roles_are_refused(self):
        for role_name in ("Hypervisor", "nfv", "NFV ", "Firewall", "", None):
            message = asvc.nfv_role_refusal(self._device(role_name, "prod-hv-1"), "boot an installer")
            self.assertIsNotNone(message, repr(role_name))
            self.assertIn("prod-hv-1", message)
            self.assertIn(repr(role_name), message)
            self.assertIn("refusing to boot an installer", message)

    def test_missing_role_attribute_is_refused(self):
        from types import SimpleNamespace

        self.assertIsNotNone(asvc.nfv_role_refusal(SimpleNamespace(name="n1"), "x"))

    def test_role_matches_the_answer_service_default(self):
        app = (MODULE.parents[2] / "bmc" / "answer_service" / "app.py").read_text()
        self.assertIn(f'NFV_ROLE = os.environ.get("NFV_ROLE", "{asvc.NFV_ROLE}")', app)

    def test_jobs_gate_on_the_role_first(self):
        """Each job's run() checks the role before any other SoT/BMC work."""
        import re

        jobs = MODULE.parents[1] / "baremetal"
        for job, first_other in (
            ("install_node.py", "provisioning_state"),
            ("apply_storage_layout.py", "load_profile("),
        ):
            src = (jobs / job).read_text()
            run = src[src.index("    def run(self"):]
            self.assertIn("nfv_role_refusal(device", run, job)
            self.assertLess(run.index("nfv_role_refusal(device"), run.index(first_other), job)
            self.assertNotRegex(src, r'query_params=\{"role": "', f"{job}: hard-coded role in the filter")


class AnswerTemplateEscaping(unittest.TestCase):
    """Every value/key interpolated into answer.toml goes through the TOML
    filters, so no SoT/profile/env string can close its quotes."""

    def test_every_interpolation_is_filtered(self):
        import re

        template = (MODULE.parents[2] / "bmc" / "answer_service" / "templates" / "answer.toml.j2").read_text()
        exprs = re.findall(r"\{\{(.*?)\}\}", template)
        self.assertTrue(exprs)
        for expr in exprs:
            expr = expr.strip()
            if expr.startswith('", "'):  # the list separator literal
                continue
            self.assertRegex(expr, r"\|\s*toml(key)?$", f"unfiltered: {{{{ {expr} }}}}")
        self.assertNotRegex(template, r'"\{\{', "a value is wrapped in hand-written quotes")


# ======================================================== the service itself

REPO = MODULE.parents[2]
APP = REPO / "bmc" / "answer_service" / "app.py"
HB_SPEC = importlib.util.spec_from_file_location("host_baseline_lib", REPO / "jobs" / "lib" / "host_baseline.py")
hblib = importlib.util.module_from_spec(HB_SPEC)
HB_SPEC.loader.exec_module(hblib)
TMP = tempfile.mkdtemp(prefix="answer-test-")
STUBBED = set()  # third-party modules replaced by stubs for this run


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


def _load_app():
    """app.py with fastapi/jinja2/requests stubbed when not installed."""
    try:
        import fastapi  # noqa: F401
    except ImportError:
        class HTTPException(Exception):
            def __init__(self, status_code, detail=None):
                super().__init__(detail)
                self.status_code, self.detail = status_code, detail

        class FastAPI:
            def __init__(self, *args, **kwargs):
                pass

            def _route(self, *args, **kwargs):
                return lambda fn: fn
            get = post = _route

        STUBBED.add("fastapi")
        _stub_module("fastapi", FastAPI=FastAPI, HTTPException=HTTPException,
                     Header=lambda default=None, **kw: default, Request=object)
        _stub_module("fastapi.concurrency", run_in_threadpool=lambda fn, *a: fn(*a))
        _stub_module("fastapi.responses", PlainTextResponse=lambda body, media_type=None: body)
    try:
        import jinja2  # noqa: F401
    except ImportError:
        class Environment:
            def __init__(self, *args, **kwargs):
                self.filters = {}

            def get_template(self, name):
                raise unittest.SkipTest("jinja2 not installed (rendering runs in the answer-service image)")

        STUBBED.add("jinja2")
        _stub_module("jinja2", Environment=Environment, FileSystemLoader=lambda *a, **k: None,
                     StrictUndefined=object)
    try:
        import requests  # noqa: F401
    except ImportError:
        STUBBED.add("requests")
        _stub_module("requests", request=None, get=None, RequestException=IOError)
    os.environ.setdefault("DATA_DIR", os.path.join(TMP, "data"))
    os.environ.setdefault("ROOT_PASSWORD_HASH_FILE", os.path.join(TMP, "root_password_hash"))
    os.environ.setdefault("PUBLIC_URL", "https://answer.example.net:8800")
    with open(os.environ["ROOT_PASSWORD_HASH_FILE"], "w") as fh:
        fh.write("$6$salt$hash\n")
    spec = importlib.util.spec_from_file_location("answer_app", APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = _load_app()


def rest_iface(id, name, type="10gbase-x-sfpp", lag=None, bridge=None, mac=None, primary=None):
    return {"id": id, "name": name, "type": {"value": type, "label": type}, "mac_address": mac,
            "lag": {"id": lag, "object_type": "dcim.interface"} if lag else None,
            "bridge": {"id": bridge, "object_type": "dcim.interface"} if bridge else None,
            "custom_fields": {} if primary is None else {"primary_member": primary}}


def rest_fleet(**overrides):
    rows = {
        "xcc": rest_iface("x", "xcc", "1000base-t", mac="AA:AA:AA:AA:AA:01"),
        "mgmt0": rest_iface("m0", "mgmt0", "1000base-t", lag="b1", mac="3C:EC:EF:00:00:01", primary=True),
        "mgmt1": rest_iface("m1", "mgmt1", "1000base-t", lag="b1", mac="3C:EC:EF:00:00:02"),
        "data0": rest_iface("d0", "data0", lag="b0", mac="3C:EC:EF:00:00:03"),
        "data1": rest_iface("d1", "data1", lag="b0", mac="3C:EC:EF:00:00:04"),
        "bond1": rest_iface("b1", "bond1", "lag", bridge="v0", mac="3C:EC:EF:00:00:01"),
        "bond0": rest_iface("b0", "bond0", "lag", bridge="v1"),
        "vmbr0": rest_iface("v0", "vmbr0", "bridge", mac="3C:EC:EF:00:00:01"),
        "vmbr1": rest_iface("v1", "vmbr1", "bridge"),
    }
    for name, row in overrides.items():
        if row is None:
            rows.pop(name)
        else:
            rows[name] = row
    return list(rows.values())


CONTEXT = {"host_baseline": {"packages": ["snmp"], "serial_console": {"speed": 115200},
                             "zfs_arc_max_bytes": 17179869184, "remove_subscription_nag": True}}


def rest_device(**overrides):
    device = {"id": "dev1", "name": "pve-se455-01", "serial": "J101YCEB", "role": {"name": "NFV"},
              "custom_fields": {"provisioning_state": "awaiting_install"},
              "device_type": {"model": "ThinkEdge SE455 V3"},
              "primary_ip4": {"id": "ip1", "address": "10.40.3.10/23"}, "config_context": CONTEXT}
    device.update(overrides)
    return device


class FakeNautobot:
    """Canned REST answers + a record of every call (method, path, params)."""

    def __init__(self, device, interfaces, primary_ifaces=("v0",), page=4):
        self.device, self.interfaces, self.primary_ifaces, self.page = device, interfaces, primary_ifaces, page
        self.calls = []

    def __call__(self, method, path, **kwargs):
        params = kwargs.get("params") or {}
        self.calls.append((method, path, dict(params)))
        if path == "/dcim/devices/":
            return {"results": [self.device]}
        if path == "/ipam/ip-addresses/ip1/":
            return {"id": "ip1", "parent": {"id": "pfx1"},
                    "interfaces": [{"id": i, "device": {"id": "dev1"}} for i in self.primary_ifaces]}
        if path == "/ipam/ip-addresses/":
            return {"results": [{"address": "10.40.2.1/23"}]}
        if path == "/dcim/interfaces/":
            offset = params.get("offset", 0)
            chunk = self.interfaces[offset:offset + self.page]
            more = offset + self.page < len(self.interfaces)
            return {"results": chunk, "next": "https://nautobot/api/dcim/interfaces/?offset=x" if more else None}
        raise AssertionError(f"unexpected call {method} {path} {params}")


DERIVATION_CASES = {
    "bridge->lag->primary": (rest_fleet(), ("v0",), ("mgmt0", ["vmbr0", "bond1", "mgmt0"])),
    "plain port": ([rest_iface("p", "mgmt", "1000base-t", mac="AA:BB:CC:DD:EE:FF")], ("p",), ("mgmt", ["mgmt"])),
    "bridge single port": ([rest_iface("v", "vmbr0", "bridge"),
                            rest_iface("p", "eno1", "1000base-t", bridge="v", mac="AA:BB:CC:DD:EE:01")],
                           ("v",), ("eno1", ["vmbr0", "eno1"])),
    "no flag": (rest_fleet(mgmt0=rest_iface("m0", "mgmt0", lag="b1", mac="3C:EC:EF:00:00:01")), ("v0",),
                "has several members (mgmt0, mgmt1) and none is flagged primary_member"),
    "two flags": (rest_fleet(mgmt1=rest_iface("m1", "mgmt1", lag="b1", mac="3C:EC:EF:00:00:02", primary=True)),
                  ("v0",), "primary_member is set on several members (mgmt0, mgmt1)"),
    "unassigned": (rest_fleet(), (), "is not assigned to any interface"),
    "several": (rest_fleet(), ("v0", "v1"), "is assigned to several interfaces"),
    "empty lag": (rest_fleet(mgmt0=None, mgmt1=None), ("v0",), "LAG bond1 on n1 has no member interfaces"),
    "nested bridge": (rest_fleet(bond1=rest_iface("b1", "vmbr7", "bridge", bridge="v0")), ("v0",),
                      "reached vmbr7 (type bridge)"),
}


class InstallNicParity(unittest.TestCase):
    """The answer service and the jobs derive the install NIC identically."""

    def test_same_answer_on_every_case(self):
        for name, (rows, ids, expected) in DERIVATION_CASES.items():
            with self.subTest(case=name):
                svc_records = [app._iface_record(r) for r in rows]
                lib_records = [hblib.interface_record(
                    id=r["id"], name=r["name"], type=r["type"]["value"], lag=(r["lag"] or {}).get("id"),
                    bridge=(r["bridge"] or {}).get("id"), mac=r["mac_address"], custom_fields=r["custom_fields"])
                    for r in rows]
                results = []
                for derive, records, err in ((app.derive_install_interface, svc_records, app.InstallNicError),
                                             (hblib.derive_install_interface, lib_records, hblib.InstallNicError)):
                    try:
                        nic, chain = derive("n1", "10.40.3.10/23", list(ids), records)
                        results.append((nic["name"], chain, nic["mac"]))
                    except err as exc:
                        results.append(str(exc))
                self.assertEqual(results[0], results[1])
                if isinstance(expected, tuple):
                    self.assertEqual(results[0][:2], expected)
                else:
                    self.assertIn(expected, results[0])

    def test_feature_lists_match(self):
        info = app.info()
        self.assertEqual(sorted(info["profile_features"]), sorted(asvc.PROFILE_FEATURE_KEYS))
        self.assertEqual(sorted(info["firstboot_features"]), sorted(asvc.FIRSTBOOT_FEATURE_KEYS))
        self.assertEqual(app.REQUIRED_PACKAGES, hblib.REQUIRED_PACKAGES)

    def test_token_names_match_the_host_baseline(self):
        for name in ("pve-se455-01", "nfv-lab-node-01"):
            self.assertEqual(app.node_token_names(name), hblib.node_token_names(name, "proxmox"))
            self.assertEqual(app.node_token_names(name, "datadog"), hblib.node_token_names(name, "datadog"))
        self.assertEqual(app.node_token_names("nfv-lab-node-01")["file_id"], "nfv-lab-node-01_proxmox_token_id")


class RestDerivation(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNautobot(rest_device(), rest_fleet())
        self._nb, app._nb = app._nb, self.fake

    def tearDown(self):
        app._nb = self._nb

    def test_derives_through_the_bridge_with_the_m2m_parameter(self):
        self.assertEqual(app.mgmt_interface_mac(rest_device()), "3c:ec:ef:00:00:01")
        ip_calls = [c for c in self.fake.calls if c[1] == "/ipam/ip-addresses/ip1/"]
        self.assertEqual(ip_calls[0][2], {"depth": 1, "exclude_m2m": "false"})

    def test_interfaces_are_paged(self):
        rows = app.device_interfaces(rest_device())
        self.assertEqual(len(rows), 9)
        offsets = [c[2]["offset"] for c in self.fake.calls if c[1] == "/dcim/interfaces/"]
        self.assertEqual(offsets, [0, 4, 8])

    def test_device_lookup_includes_the_config_context(self):
        app.device_by_serial("J101YCEB")
        self.assertEqual(self.fake.calls[0], ("GET", "/dcim/devices/",
                                              {"serial": "J101YCEB", "depth": 1, "include": "config_context"}))

    def test_ambiguity_is_a_409_before_answering(self):
        self.fake.interfaces = rest_fleet(mgmt0=rest_iface("m0", "mgmt0", lag="b1", mac="3C:EC:EF:00:00:01"))
        with self.assertRaises(app.HTTPException) as ctx:
            app.mgmt_interface_mac(rest_device())
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("install NIC: LAG bond1 on pve-se455-01 has several members", ctx.exception.detail)

    def test_pin_mapping_never_pins_a_bond_or_bridge_mac(self):
        nics = [{"mac": m.lower()} for m in ("3C:EC:EF:00:00:01", "3C:EC:EF:00:00:02", "AA:AA:AA:AA:AA:01")]
        mapping = app.build_pin_mapping(rest_fleet(), nics, "n1")
        self.assertEqual(mapping, {"3c:ec:ef:00:00:01": "mgmt0", "3c:ec:ef:00:00:02": "mgmt1"})


class FirstbootInputs(unittest.TestCase):
    SE455 = {"serial_console": {"unit": 0}}

    def test_profile_key_validation(self):
        self.assertEqual(app.serial_console_spec({"serial_console": {"unit": 0}}), {"unit": 0, "tty": "ttyS0"})
        self.assertIsNone(app.serial_console_spec({}))
        for bad, needle in (({"unit": 9}, "must be an integer 0-7"), ({"unit": True}, "must be an integer 0-7"),
                            ({"unit": 0, "speed": 115200}, "accepts only `unit`"), ("ttyS0", "must be a mapping")):
            with self.subTest(bad=bad), self.assertRaises(app.HTTPException) as ctx:
                app.serial_console_spec({"serial_console": bad})
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIn(needle, ctx.exception.detail)

    def test_full_context(self):
        fb = app.host_baseline_firstboot(rest_device(), self.SE455)
        self.assertEqual(fb["packages"], ["lldpd", "snmpd", "snmp"])
        self.assertEqual(fb["serial"]["console_arg"], "ttyS0,115200n8")
        self.assertEqual(fb["serial"]["grub_command"], "serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1")
        self.assertEqual((fb["zfs_arc_max_bytes"], fb["remove_nag"]), (17179869184, True))
        self.assertIn("serial=ttyS0 at 115200 8N1", app.firstboot_summary(fb))

    def test_absent_inputs_skip_with_a_reason(self):
        fb = app.host_baseline_firstboot(rest_device(config_context={}), self.SE455)
        self.assertEqual(fb["packages"], ["lldpd", "snmpd"])
        self.assertIsNone(fb["serial"])
        self.assertIn("declares ttyS0 but the SoT has no host_baseline.serial_console", fb["serial_note"])
        self.assertEqual((fb["zfs_arc_max_bytes"], fb["remove_nag"]), (None, False))
        fb = app.host_baseline_firstboot(rest_device(), {})
        self.assertIn("declares no serial port", fb["serial_note"])

    def test_malformed_inputs_refuse_at_answer_time(self):
        cases = {
            "speed": ({"serial_console": {"speed": 100}}, "serial_console.speed must be one of"),
            "parity": ({"serial_console": {"speed": 9600, "parity": "mark"}}, "parity must be no, odd or even"),
            "unknown": ({"serial_console": {"speed": 9600, "flow": "rts"}}, "unknown key(s) ['flow']"),
            "arc": ({"zfs_arc_max_bytes": 1024}, "zfs_arc_max_bytes must be an integer >= 67108864"),
            "arc bool": ({"zfs_arc_max_bytes": True}, "zfs_arc_max_bytes must be an integer"),
            "nag": ({"remove_subscription_nag": "yes"}, "remove_subscription_nag must be true or false"),
            "pkg": ({"packages": ["ok", "rm -rf"]}, "is not a valid Debian package name"),
            "pkgs": ({"packages": "lldpd"}, "packages must be a list"),
            "block": ("not-a-mapping", "host_baseline must be a mapping"),
        }
        for name, (hb_value, needle) in cases.items():
            with self.subTest(case=name), self.assertRaises(app.HTTPException) as ctx:
                app.host_baseline_firstboot(rest_device(config_context={"host_baseline": hb_value}), self.SE455)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn(needle, ctx.exception.detail)

    def test_stale_service_warning_in_the_install_job(self):
        self.assertIsNone(asvc.firstboot_inputs_warning({"firstboot_features": list(asvc.FIRSTBOOT_FEATURE_KEYS)},
                                                        CONTEXT))
        message = asvc.firstboot_inputs_warning({"profiles": []}, CONTEXT, "https://svc:8800")
        self.assertIn("does not render the host_baseline firstboot input(s)", message)
        self.assertIsNone(asvc.firstboot_inputs_warning({"profiles": []}, {}))
        self.assertIsNone(asvc.firstboot_inputs_warning(None, CONTEXT))


@unittest.skipUnless(not STUBBED and app.yaml is not None,
                     "needs fastapi/jinja2/requests/PyYAML (runs in the answer-service image)")
class EndToEnd(unittest.TestCase):
    """POST /answer and GET /firstboot for a bridge/LAG-modelled SE455 V3."""

    IDENTITY = {"dmi": {"system": {"serial": "J101YCEB"}},
                "network_interfaces": [{"link": f"nic{i}", "mac": m} for i, m in enumerate(
                    ["3c:ec:ef:00:00:01", "3c:ec:ef:00:00:02", "3c:ec:ef:00:00:03", "3c:ec:ef:00:00:04"])]}

    def setUp(self):
        self.fake = FakeNautobot(rest_device(), rest_fleet())
        self._nb, app._nb = app._nb, self.fake

    def tearDown(self):
        app._nb = self._nb

    def answer(self):
        response = app._answer_impl(json.loads(json.dumps(self.IDENTITY)))
        return response.body.decode()

    def test_answer_uses_the_derived_port_and_pins_only_ports(self):
        text = self.answer()
        self.assertIn('filter.ID_NET_NAME_MAC = "*3cecef000001"', text)
        self.assertIn('"3c:ec:ef:00:00:01" = "mgmt0"', text)
        self.assertIn('"3c:ec:ef:00:00:03" = "data0"', text)
        self.assertNotIn("vmbr0", text)
        self.assertNotIn("bond1", text)
        tool = shutil.which("proxmox-auto-install-assistant")
        if tool:
            with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
                fh.write(text)
            run = subprocess.run([tool, "validate-answer", fh.name], capture_output=True, text=True)
            os.unlink(fh.name)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_ambiguous_model_refuses_the_answer(self):
        self.fake.interfaces = rest_fleet(
            mgmt1=rest_iface("m1", "mgmt1", lag="b1", mac="3C:EC:EF:00:00:02", primary=True))
        with self.assertRaises(app.HTTPException) as ctx:
            self.answer()
        self.assertEqual(ctx.exception.status_code, 409)

    def test_derived_port_without_mac_refuses(self):
        self.fake.interfaces = rest_fleet(mgmt0=rest_iface("m0", "mgmt0", lag="b1", primary=True))
        with self.assertRaises(app.HTTPException) as ctx:
            self.answer()
        self.assertIn("derived through the management bridge/LAG", ctx.exception.detail)

    def test_firstboot_renders_every_input_and_parses(self):
        for context, expect, absent in (
            (CONTEXT, ["NFV_PKGS=( lldpd snmpd snmp )", "NFV_SERIAL_CONSOLE=ttyS0,115200n8",
                       "NFV_ARC_MAX=17179869184", "nfv_subscription_nag()"], []),
            ({}, ["NFV_PKGS=( lldpd snmpd )", "the SoT has no host_baseline.serial_console",
                  "zfs arc: no host_baseline.zfs_arc_max_bytes", "subscription nag: not requested"],
             ["NFV_SERIAL_CONSOLE", "NFV_ARC_MAX", "nfv_subscription_nag()"]),
        ):
            with self.subTest(context=bool(context)):
                self.fake.device = rest_device(config_context=context)
                key = app.issue_key("J101YCEB", "firstboot")
                script = app._firstboot_impl("J101YCEB", key).body.decode()
                for needle in expect:
                    self.assertIn(needle, script)
                for needle in absent:
                    self.assertNotIn(needle, script)
                # the phone-home and storage steps are unchanged and come first
                self.assertLess(script.index("phone the credentials home"), script.index("# --- host-baseline inputs"))
                self.assertLess(script.index("data volume (profile install.data_volume)"),
                                script.index("# --- host-baseline inputs"))
                with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
                    fh.write(script)
                run = subprocess.run(["bash", "-n", fh.name], capture_output=True, text=True)
                os.unlink(fh.name)
                self.assertEqual(run.returncode, 0, run.stderr)
                import ast
                for block in re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", script, re.S):
                    ast.parse(block)


if __name__ == "__main__":
    unittest.main(verbosity=1)
