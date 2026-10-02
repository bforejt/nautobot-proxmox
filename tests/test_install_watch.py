#!/usr/bin/env python3
"""
Unit tests for the install job's state watch (F33): the per-delivery-method
watch window (jobs/lib/install_delivery.watch_timeout_seconds) and the vmedia
eject as soon as the webhook lands (jobs/baremetal/install_node.py), and
the nested reinstall reconciliation (F36,
jobs/lib/install_delivery.stale_install_vms).
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


class NestedIsoChecksumGuard(unittest.TestCase):
    """The nested path's carrier pulls the ISO via download-url: an image
    record with no checksum is refused before the carrier is touched."""

    IMAGE = types.SimpleNamespace(
        image_file_name="pve-9.2-auto.iso", download_url="http://fw.example/pve-9.2-auto.iso",
        image_file_checksum="", hashing_algorithm="sha256",
    )

    def test_install_nested_refuses_before_resolving_the_carrier(self):
        job = inode.InstallProxmoxNode()
        job.logger = Logger()
        orig = inode.resolve_hypervisor
        touched = []
        inode.resolve_hypervisor = lambda device: touched.append(device)
        try:
            with self.assertRaises(inode.ContractViolation) as cm:
                job._install_nested(object(), profile("pve-nested"), self.IMAGE)
        finally:
            inode.resolve_hypervisor = orig
        self.assertIn("no checksum", str(cm.exception))
        self.assertEqual(touched, [])

    def test_ensure_iso_refuses_without_node_calls(self):
        client = types.SimpleNamespace(calls=[])
        client.storage_content = lambda *a: client.calls.append(a) or []
        client.download_url = lambda *a, **k: client.calls.append(a)
        delivery = idl.PveNestedDelivery(client, "carrier1", Logger())
        with self.assertRaises(ValueError):
            delivery.ensure_iso("local", "pve-9.2-auto.iso", "http://fw.example/x.iso", None)
        self.assertEqual(client.calls, [])

    def test_ensure_iso_passes_checksum_to_the_pull(self):
        seen = {}
        client = types.SimpleNamespace(
            storage_content=lambda *a: [],
            download_url=lambda *a, **k: seen.update(k) or "local:iso/pve-9.2-auto.iso",
        )
        log = Logger()
        idl.PveNestedDelivery(client, "carrier1", log).ensure_iso(
            "local", "pve-9.2-auto.iso", "http://fw.example/x.iso", "b" * 64, "sha256")
        self.assertEqual(seen["checksum"], "b" * 64)
        self.assertEqual(seen["checksum_algorithm"], "sha256")
        self.assertTrue(any("sha256-verified" in m for _, m in log.records))


class StaleInstallVms(unittest.TestCase):
    """F36: a confirmed nested reinstall destroys only the job's own install
    VM (l0-lab tag or the Device's recorded vmid) — a VNF VM that merely
    shares the Device's name is refused, never purged."""

    def plan(self, vms, vmid=120, recorded=None):
        return idl.stale_install_vms(vms, node="pve-lab", name="fw-01", vmid=vmid,
                                     recorded_vmid=recorded)

    def test_boot_installer_sets_the_marker_the_reconciliation_reads(self):
        self.assertEqual(idl.INSTALL_VM_TAGS, "nfv;l0-lab")
        self.assertIn(idl.INSTALL_VM_TAG, idl.vm_tags({"tags": idl.INSTALL_VM_TAGS}))

    def test_vm_tags_parsing(self):
        self.assertEqual(idl.vm_tags({"tags": "l0-lab;nfv"}), {"l0-lab", "nfv"})
        self.assertEqual(idl.vm_tags({"tags": "nfv, L0-lab"}), {"l0-lab", "nfv"})
        self.assertEqual(idl.vm_tags({}), set())
        self.assertEqual(idl.vm_tags({"tags": None}), set())

    def test_same_name_vnf_vm_is_refused(self):
        vnf = {"vmid": 105, "name": "fw-01", "tags": "nfv;sot-driven", "status": "running"}
        with self.assertRaises(idl.DeliveryError) as cm:
            self.plan([vnf])
        msg = str(cm.exception)
        self.assertIn("VM 105 on pve-lab is named fw-01 but is not this job's install VM", msg)
        self.assertIn("no 'l0-lab' tag", msg)
        self.assertIn("vmid custom field is empty", msg)

    def test_untagged_same_name_vm_at_another_vmid_than_recorded_is_refused(self):
        with self.assertRaises(idl.DeliveryError) as cm:
            self.plan([{"vmid": 105, "name": "fw-01"}], vmid=120, recorded=120)
        self.assertIn("vmid custom field is 120", str(cm.exception))

    def test_tagged_install_vm_is_stale(self):
        vm = {"vmid": 120, "name": "fw-01", "tags": "l0-lab;nfv"}
        self.assertEqual(self.plan([vm], recorded=120), [vm])
        # tag alone suffices (the vmid CF was cleared)
        vm2 = {"vmid": 130, "name": "fw-01", "tags": "nfv;l0-lab"}
        self.assertEqual(self.plan([vm2], vmid=140), [vm2])

    def test_untagged_vm_at_the_recorded_vmid_is_stale(self):
        vm = {"vmid": 120, "name": "fw-01"}  # created before tags, or tags edited away
        self.assertEqual(self.plan([vm], recorded=120), [vm])
        self.assertEqual(self.plan([vm], recorded="120"), [vm])

    def test_foreign_vm_at_our_vmid_is_refused(self):
        with self.assertRaises(idl.DeliveryError) as cm:
            self.plan([{"vmid": 120, "name": "db-01", "tags": "l0-lab"}], recorded=120)
        self.assertIn("VMID 120 on pve-lab belongs to 'db-01', not fw-01", str(cm.exception))

    def test_refusal_comes_before_any_stale_vm_is_returned(self):
        ours = {"vmid": 120, "name": "fw-01", "tags": "nfv;l0-lab"}
        vnf = {"vmid": 105, "name": "fw-01", "tags": "nfv;sot-driven"}
        with self.assertRaises(idl.DeliveryError):
            self.plan([ours, vnf], recorded=120)

    def test_unrelated_vms_are_ignored(self):
        self.assertEqual(self.plan([{"vmid": 101, "name": "other"}, {"name": "x"}]), [])


class NestedReinstallReconcile(unittest.TestCase):
    """_install_nested refuses a same-named VNF VM before stopping or
    destroying anything on the carrier."""

    IMAGE = types.SimpleNamespace(
        image_file_name="pve-9.2-auto.iso", download_url="http://fw.example/pve-9.2-auto.iso",
        image_file_checksum="a" * 64, hashing_algorithm="sha256",
    )

    def run_nested(self, vms, recorded=None):
        self.calls = calls = []

        class Client:
            def __init__(self, **kwargs):
                pass

            def next_vmid(self):
                return 140

            def list_vms(self, node):
                return vms

            def stop_vm(self, node, vmid):
                calls.append(("stop", vmid))

            def destroy_vm(self, node, vmid):
                calls.append(("destroy", vmid))

        class Delivery:
            def __init__(self, *a):
                pass

            def ensure_iso(self, *a):
                return "local:iso/pve-9.2-auto.iso"

            def boot_installer(self, **kw):
                calls.append(("boot", kw["vmid"]))
                raise RuntimeError("stop here")

        carrier = types.SimpleNamespace(
            name="pve-lab", cf={"vm_storage": "local-lvm"},
            primary_ip4=types.SimpleNamespace(address=types.SimpleNamespace(ip="192.0.2.10")),
        )
        device = types.SimpleNamespace(name="fw-01", serial="NESTED-1", cf={"vmid": recorded})
        patches = {
            "resolve_hypervisor": lambda d: carrier,
            "resolve_proxmox_credentials": lambda c: ("t@pve!x", "s"),
            "ProxmoxClient": Client,
            "PveNestedDelivery": Delivery,
        }
        orig = {k: getattr(inode, k) for k in patches}
        for k, v in patches.items():
            setattr(inode, k, v)
        job = inode.InstallProxmoxNode()
        job.logger = Logger()
        job._mgmt_mac = lambda d: None
        try:
            job._install_nested(device, profile("pve-nested"), self.IMAGE)
        finally:
            for k, v in orig.items():
                setattr(inode, k, v)
        return calls

    def test_vnf_vm_with_the_device_name_is_refused_untouched(self):
        vnf = {"vmid": 105, "name": "fw-01", "tags": "nfv;sot-driven", "status": "running"}
        with self.assertRaises(inode.ContractViolation) as cm:
            self.run_nested([vnf])
        self.assertIn("is not this job's install VM", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_own_install_vm_is_destroyed_and_its_vmid_reused(self):
        ours = {"vmid": 120, "name": "fw-01", "tags": "nfv;l0-lab", "status": "running"}
        with self.assertRaisesRegex(RuntimeError, "stop here"):
            self.run_nested([ours], recorded=120)
        self.assertEqual(self.calls, [("stop", 120), ("destroy", 120), ("boot", 120)])

    def test_fresh_install_takes_next_vmid(self):
        with self.assertRaisesRegex(RuntimeError, "stop here"):
            self.run_nested([{"vmid": 101, "name": "other", "tags": "nfv;sot-driven"}])
        self.assertEqual(self.calls, [("boot", 140)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
