#!/usr/bin/env python3
"""
Unit tests for the install job's state watch (F33): the per-delivery-method
watch window (jobs/lib/install_delivery.watch_timeout_seconds) and the vmedia
eject as soon as the webhook lands (jobs/baremetal/install_node.py).
Stdlib-only: requests and the nautobot modules are stubbed, and the job
package is loaded from its file paths.

Run:  python3 tests/test_install_watch.py
"""

import importlib
import pathlib
import re
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Anything:
    """Stand-in for any nautobot class/function the job module touches."""

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Anything()


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    mod.__getattr__ = lambda attr: _Anything
    sys.modules[name] = mod
    return mod


if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        _stub("requests", RequestException=IOError, ConnectionError=IOError)
        _stub("requests.auth")
for _name in ("nautobot", "nautobot.apps", "nautobot.dcim", "nautobot.dcim.models",
              "nautobot.extras", "nautobot.extras.models", "nautobot.extras.choices"):
    _stub(_name)
_stub("nautobot.apps.jobs", Job=object, register_jobs=lambda *a: None)

# Load jobs/lib and jobs/baremetal as a synthetic package (skipping
# jobs/__init__.py, which imports every job).
_pkg = types.ModuleType("jobpkg")
_pkg.__path__ = [str(ROOT / "jobs")]
sys.modules["jobpkg"] = _pkg
for _sub in ("lib", "baremetal"):
    _m = types.ModuleType(f"jobpkg.{_sub}")
    _m.__path__ = [str(ROOT / "jobs" / _sub)]
    sys.modules[f"jobpkg.{_sub}"] = _m
idl = importlib.import_module("jobpkg.lib.install_delivery")
inode = importlib.import_module("jobpkg.baremetal.install_node")


def profile(method, **delivery):
    return {"delivery": {"method": method, **delivery}}


class WatchTimeout(unittest.TestCase):
    def test_defaults_per_method(self):
        self.assertEqual(idl.watch_timeout_seconds(profile("redfish-vmedia")), 4500)
        self.assertEqual(idl.watch_timeout_seconds(profile("pve-nested")), 1800)

    def test_vmedia_default_outlasts_the_documented_install(self):
        self.assertGreater(idl.watch_timeout_seconds(profile("redfish-vmedia")), 40 * 60)

    def test_override_within_bounds(self):
        p = profile("redfish-vmedia", watch_timeout_seconds=5400)
        self.assertEqual(idl.watch_timeout_seconds(p), 5400)

    def test_override_out_of_bounds_or_wrong_type_refuses(self):
        for bad in (0, 299, 6001, "3600", 3600.5, True, -5):
            with self.subTest(bad=bad), self.assertRaises(idl.DeliveryError) as ctx:
                idl.watch_timeout_seconds(profile("redfish-vmedia", watch_timeout_seconds=bad))
            self.assertIn("watch_timeout_seconds must be an integer between 300 and 6000",
                          str(ctx.exception))
        with self.assertRaises(idl.DeliveryError):
            idl.watch_timeout_seconds(profile("pve-nested", watch_timeout_seconds=3601))

    def test_method_without_a_job_watch_refuses(self):
        for method in ("pxe", None, "bogus"):
            with self.subTest(method=method), self.assertRaises(idl.DeliveryError):
                idl.watch_timeout_seconds(profile(method))

    def test_caps_fit_the_job_time_limit(self):
        src = (ROOT / "jobs" / "baremetal" / "install_node.py").read_text()
        soft = int(re.search(r"soft_time_limit = (\d+)", src).group(1))
        for method in idl.WATCH_TIMEOUT_DEFAULTS:
            self.assertLessEqual(idl.WATCH_TIMEOUT_DEFAULTS[method], idl.WATCH_TIMEOUT_MAX[method])
        # nested: ISO pull + install to power-off + state-flip grace + watch
        self.assertLess(1800 + 2700 + 120 + idl.WATCH_TIMEOUT_MAX["pve-nested"], soft)
        # vmedia: power-on wait + 2 volume creates + media insert + watch
        self.assertLess(300 + 2 * 180 + 120 + idl.WATCH_TIMEOUT_MAX["redfish-vmedia"], soft)

    def test_shipped_profiles_resolve(self):
        if idl.yaml is None:
            self.skipTest("PyYAML not installed")
        for path in sorted((ROOT / "bmc" / "profiles").glob("*.yaml")):
            p = idl.yaml.safe_load(path.read_text())
            if p["delivery"].get("method") in idl.WATCH_TIMEOUT_DEFAULTS:
                with self.subTest(profile=path.name):
                    idl.watch_timeout_seconds(p)


# ---- the watch loop ------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeDevice:
    """provisioning_state flips at `installed_at`, secrets_group at `creds_at`."""

    def __init__(self, clock, installed_at=None, creds_at=None):
        self.clock, self.installed_at, self.creds_at = clock, installed_at, creds_at
        self.name = "edge-01"
        self.cf = {}

    def refresh_from_db(self):
        t = self.clock.now
        installed = self.installed_at is not None and t >= self.installed_at
        self.cf = {
            "provisioning_state": "bm_installed" if installed else "awaiting_install",
            "secrets_group": "edge-01-pve" if self.creds_at is not None and t >= self.creds_at
            else None,
        }


class FakeRedfish:
    def __init__(self, clock, fail=False):
        self.clock, self.fail, self.ejects = clock, fail, []

    def eject_iso(self, member_path, mode):
        self.ejects.append((self.clock.now, member_path, mode))
        if self.fail:
            raise RuntimeError("409 busy")


class Logger:
    def __init__(self):
        self.records = []

    def info(self, msg, *args):
        self.records.append(("info", msg % args))

    def warning(self, msg, *args):
        self.records.append(("warning", msg % args))


class WatchLoop(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self._orig_time = inode.time
        inode.time = types.SimpleNamespace(time=self.clock.time, sleep=self.clock.sleep)
        self.job = inode.InstallProxmoxNode()
        self.job.logger = Logger()
        self.mount = {"member_path": "/redfish/v1/Managers/1/VirtualMedia/EXT1", "mode": "patch-ext"}

    def tearDown(self):
        inode.time = self._orig_time

    def test_35_min_vmedia_install_completes_inside_the_vmedia_window(self):
        device = FakeDevice(self.clock, installed_at=35 * 60, creds_at=38 * 60)
        redfish = FakeRedfish(self.clock)
        self.job._vmedia_mount = (redfish, self.mount)
        timeout = idl.watch_timeout_seconds(profile("redfish-vmedia"))
        self.assertEqual(self.job._watch_state_machine(device, timeout), (True, True))
        # ejected on the flip (first poll at/after 35 min), not after the watch
        self.assertEqual(len(redfish.ejects), 1)
        self.assertLess(redfish.ejects[0][0], 38 * 60)
        self.assertIsNone(self.job._vmedia_mount)

    def test_eject_happens_even_if_credentials_never_arrive(self):
        device = FakeDevice(self.clock, installed_at=20 * 60)
        redfish = FakeRedfish(self.clock)
        self.job._vmedia_mount = (redfish, self.mount)
        self.assertEqual(self.job._watch_state_machine(device, 4500), (True, False))
        self.assertEqual(len(redfish.ejects), 1)
        self.assertLess(redfish.ejects[0][0], 21 * 60)

    def test_no_eject_without_webhook(self):
        device = FakeDevice(self.clock)
        redfish = FakeRedfish(self.clock)
        self.job._vmedia_mount = (redfish, self.mount)
        self.assertEqual(self.job._watch_state_machine(device, 600), (False, False))
        self.assertEqual(redfish.ejects, [])
        self.assertIsNotNone(self.job._vmedia_mount)
        self.assertGreaterEqual(self.clock.now, 600)

    def test_failed_eject_is_a_warning_not_a_crash(self):
        device = FakeDevice(self.clock, installed_at=0, creds_at=0)
        redfish = FakeRedfish(self.clock, fail=True)
        self.job._vmedia_mount = (redfish, self.mount)
        self.assertEqual(self.job._watch_state_machine(device, 600), (True, True))
        self.assertTrue(any(level == "warning" and "Could not eject" in msg
                            for level, msg in self.job.logger.records))

    def test_nested_has_nothing_to_eject(self):
        device = FakeDevice(self.clock, installed_at=0, creds_at=60)
        self.job._vmedia_mount = None
        self.assertEqual(self.job._watch_state_machine(device, 1800), (True, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
