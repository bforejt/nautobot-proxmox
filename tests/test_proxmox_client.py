#!/usr/bin/env python3
"""
Unit tests for jobs/lib/proxmox_client.py task-completion handling: a PVE
task that finished with "WARNINGS: <n>" succeeded (exit code 0) and must not
be treated as a failure (deploy would roll back a VM that started fine).
Also: transport errors / 5xx / non-JSON bodies surface as ProxmoxError
(ProxmoxUnreachableError) so the jobs' best-effort guards catch them, and
wait_task tolerates a few consecutive failed status polls. Image pulls refuse
(fail closed, before any node call) when the image record has no checksum.
A failed deploy's rollback destroys only a VM that carries the device's name
(rollback_vm_decision), never another deploy's VM at a colliding vmid.
Stdlib-only; `requests` is stubbed (the client only needs it at construction)
and the API is a canned fake -- no network.

Run:  python3 tests/test_proxmox_client.py
"""

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock

if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        stub = types.ModuleType("requests")

        class _Session:
            def __init__(self):
                self.headers = {}
                self.verify = True

        class _RequestException(IOError):
            pass

        class _ConnectionError(_RequestException):
            pass

        stub.Session = _Session
        stub.RequestException = _RequestException
        stub.ConnectionError = _ConnectionError
        stub.packages = types.SimpleNamespace(
            urllib3=types.SimpleNamespace(disable_warnings=lambda *a, **k: None))
        sys.modules["requests"] = stub

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "proxmox_client.py"
spec = importlib.util.spec_from_file_location("proxmox_client", MODULE)
pc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pc  # dataclasses resolve string annotations via sys.modules
spec.loader.exec_module(pc)

UPID = "UPID:pve1:0001:0002:0003:qmstart:100:svc@pve!deploy:"


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args)


class TaskExitOutcome(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(pc.task_exit_outcome("OK"), (True, 0))

    def test_warnings_with_count(self):
        self.assertEqual(pc.task_exit_outcome("WARNINGS: 3"), (True, 3))

    def test_warnings_unparseable_count_still_success_and_counted(self):
        self.assertEqual(pc.task_exit_outcome("WARNINGS"), (True, 1))
        self.assertEqual(pc.task_exit_outcome("WARNINGS: many"), (True, 1))

    def test_failures(self):
        for status in ("unable to start VM 100 - timeout", "unexpected status",
                       "job errors", "", None, "ok", "WARNING: 1 not quite"):
            with self.subTest(status=status):
                ok, _ = pc.task_exit_outcome(status)
                self.assertFalse(ok)


def client_with_status(exitstatus, logger=None):
    c = pc.ProxmoxClient(host="192.0.2.10", token_id="svc@pve!t", token_secret="x", logger=logger)
    c.get = mock.Mock(return_value={"status": "stopped", "exitstatus": exitstatus})
    return c


class WaitTask(unittest.TestCase):
    def test_ok_returns_without_warning(self):
        log = RecordingLogger()
        client_with_status("OK", log).wait_task("pve1", UPID, poll=0)
        self.assertEqual(log.warnings, [])

    def test_warnings_succeeds_and_logs_count(self):
        log = RecordingLogger()
        client_with_status("WARNINGS: 2", log).wait_task("pve1", UPID, poll=0)
        self.assertEqual(len(log.warnings), 1)
        self.assertIn("2 warning(s)", log.warnings[0])
        self.assertIn(UPID, log.warnings[0])

    def test_warnings_without_job_logger_uses_module_logger(self):
        with self.assertLogs(pc._log, level="WARNING"):
            client_with_status("WARNINGS: 1").wait_task("pve1", UPID, poll=0)

    def test_error_raises(self):
        c = client_with_status("unable to start VM 100 - timeout")
        with self.assertRaises(pc.ProxmoxTaskError) as cm:
            c.wait_task("pve1", UPID, poll=0)
        self.assertIn("failed", str(cm.exception))

    def test_still_running_times_out(self):
        c = pc.ProxmoxClient(host="192.0.2.10", token_id="svc@pve!t", token_secret="x")
        c.get = mock.Mock(return_value={"status": "running"})
        with mock.patch.object(pc.time, "sleep"):
            with self.assertRaises(pc.ProxmoxTaskError):
                c.wait_task("pve1", UPID, timeout=3, poll=1)


SECRET = "s3cr3t-token-value"


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None, bad_json=False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json
        self.text = text if text is not None else ("<html>proxy error</html>" if bad_json else "")

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeSession:
    """Stands in for requests.Session: replays canned responses/exceptions."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.headers = {}
        self.calls = []

    def _next(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    request = _next
    post = _next


def client_with_session(*outcomes, logger=None):
    c = pc.ProxmoxClient(host="192.0.2.10", token_id="svc@pve!t", token_secret=SECRET, logger=logger)
    c.session = FakeSession(*outcomes)
    return c


class RequestErrorMapping(unittest.TestCase):
    def test_ok_unwraps_data(self):
        c = client_with_session(FakeResponse(body={"data": {"release": "9.2"}}))
        self.assertEqual(c.version(), {"release": "9.2"})

    def test_transport_error_is_proxmox_error(self):
        c = client_with_session(pc.requests.ConnectionError("Connection refused"))
        with self.assertRaises(pc.ProxmoxError) as cm:
            c.list_vms("pve1")
        self.assertIsInstance(cm.exception, pc.ProxmoxUnreachableError)
        msg = str(cm.exception)
        self.assertIn("GET /nodes/pve1/qemu", msg)
        self.assertIn("transport error", msg)
        self.assertNotIn(SECRET, msg)

    def test_5xx_is_unreachable(self):
        for code in (500, 502, 596):
            with self.subTest(code=code):
                c = client_with_session(FakeResponse(code, text="Connection refused (596)"))
                with self.assertRaises(pc.ProxmoxUnreachableError):
                    c.get("/version")

    def test_4xx_is_plain_proxmox_error(self):
        c = client_with_session(FakeResponse(403, text="Permission check failed"))
        with self.assertRaises(pc.ProxmoxError) as cm:
            c.get("/version")
        self.assertNotIsInstance(cm.exception, pc.ProxmoxUnreachableError)
        self.assertIn("403", str(cm.exception))

    def test_non_json_body_is_proxmox_error(self):
        c = client_with_session(FakeResponse(200, bad_json=True))
        with self.assertRaises(pc.ProxmoxUnreachableError) as cm:
            c.get("/version")
        self.assertIn("not JSON", str(cm.exception))

    def test_json_without_envelope_is_proxmox_error(self):
        c = client_with_session(FakeResponse(200, body=["not", "an", "envelope"]))
        with self.assertRaises(pc.ProxmoxUnreachableError):
            c.get("/version")

    def test_upload_transport_error_is_proxmox_error(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".iso") as fh:
            c = client_with_session(pc.requests.ConnectionError("reset by peer"))
            with self.assertRaises(pc.ProxmoxUnreachableError) as cm:
                c.upload_file("pve1", "local", fh.name, filename="x.iso")
        self.assertIn("upload x.iso", str(cm.exception))

    def test_upload_non_json_is_proxmox_error(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".iso") as fh:
            c = client_with_session(FakeResponse(200, bad_json=True))
            with self.assertRaises(pc.ProxmoxUnreachableError):
                c.upload_file("pve1", "local", fh.name, filename="x.iso")


def unreachable(n=1):
    return [pc.ProxmoxUnreachableError("GET /nodes/pve1/tasks/... -> 596: proxy restart")] * n


class WaitTaskTransientPolls(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = mock.patch.object(pc.time, "sleep", side_effect=self.sleeps.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, side_effect, logger=None):
        c = pc.ProxmoxClient(host="192.0.2.10", token_id="svc@pve!t", token_secret="x",
                             logger=logger or RecordingLogger())
        c.get = mock.Mock(side_effect=side_effect)
        return c

    def test_tolerates_four_failures_then_succeeds(self):
        log = RecordingLogger()
        c = self.client(unreachable(4) + [{"status": "stopped", "exitstatus": "OK"}], log)
        c.wait_task("pve1", UPID, timeout=900, poll=5)
        self.assertEqual(c.get.call_count, 5)
        self.assertEqual(len(log.warnings), 4)
        self.assertIn("status poll failed (1/5 consecutive)", log.warnings[0])
        self.assertIn("(4/5 consecutive)", log.warnings[3])

    def test_fifth_consecutive_failure_raises_lost_contact(self):
        c = self.client(unreachable(5) + [{"status": "stopped", "exitstatus": "OK"}])
        with self.assertRaises(pc.ProxmoxUnreachableError) as cm:
            c.wait_task("pve1", UPID, timeout=900, poll=5)
        self.assertNotIsInstance(cm.exception, pc.ProxmoxTaskError)
        self.assertIsInstance(cm.exception, pc.ProxmoxError)
        self.assertIn("Lost contact with task", str(cm.exception))
        self.assertIn("may still be running", str(cm.exception))
        self.assertEqual(c.get.call_count, 5)

    def test_successful_poll_resets_counter(self):
        running = {"status": "running"}
        c = self.client(unreachable(4) + [running] + unreachable(4)
                        + [{"status": "stopped", "exitstatus": "OK"}])
        c.wait_task("pve1", UPID, timeout=900, poll=5)
        self.assertEqual(c.get.call_count, 10)

    def test_backoff_grows_and_is_capped(self):
        c = self.client(unreachable(4) + [{"status": "stopped", "exitstatus": "OK"}])
        c.wait_task("pve1", UPID, timeout=900, poll=10)
        self.assertEqual(self.sleeps, [10, 20, 30, 30])

    def test_4xx_poll_error_raises_at_once(self):
        c = self.client([pc.ProxmoxError("GET ... -> 403: Permission check failed")])
        with self.assertRaises(pc.ProxmoxError):
            c.wait_task("pve1", UPID, timeout=900, poll=5)
        self.assertEqual(c.get.call_count, 1)

    def test_non_object_status_counts_as_failed_poll(self):
        c = self.client([None, {"status": "stopped", "exitstatus": "OK"}])
        c.wait_task("pve1", UPID, timeout=900, poll=5)
        self.assertEqual(c.get.call_count, 2)

    def test_timeout_still_bounds_failing_polls(self):
        c = self.client(unreachable(4))
        with self.assertRaises(pc.ProxmoxTaskError) as cm:
            c.wait_task("pve1", UPID, timeout=3, poll=1)
        self.assertIn("did not finish within 3s", str(cm.exception))

    def test_task_failure_after_transient_poll_is_task_error(self):
        c = self.client(unreachable(1) + [{"status": "stopped", "exitstatus": "storage full"}])
        with self.assertRaises(pc.ProxmoxTaskError) as cm:
            c.wait_task("pve1", UPID, timeout=900, poll=5)
        self.assertIn("failed: storage full", str(cm.exception))


AGENT_IFACES = {"data": {"result": [
    {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": "127.0.0.1"}]},
    {"name": "eth0", "ip-addresses": [
        {"ip-address-type": "ipv6", "ip-address": "fe80::1"},
        {"ip-address-type": "ipv4", "ip-address": "192.0.2.50"}]},
]}}


class AgentProbe(unittest.TestCase):
    """F78: a refused agent probe is a permissions problem, not 'not ready'."""

    def test_reports_first_non_loopback_ipv4(self):
        c = client_with_session(FakeResponse(200, body=AGENT_IFACES))
        self.assertEqual(c.agent_ipv4("pve1", 100), "192.0.2.50")

    def test_agent_not_running_is_not_ready(self):
        c = client_with_session(FakeResponse(500, text="QEMU guest agent is not running"))
        self.assertIsNone(c.agent_ipv4("pve1", 100))

    def test_other_4xx_is_not_ready(self):
        c = client_with_session(FakeResponse(400, text="VM 100 not running"))
        self.assertIsNone(c.agent_ipv4("pve1", 100))

    def test_403_raises_naming_the_privilege(self):
        c = client_with_session(FakeResponse(403, text="Permission check failed (/vms/100, VM.GuestAgent.Audit|VM.GuestAgent.Unrestricted)"))
        with self.assertRaises(pc.ProxmoxAgentPermissionError) as cm:
            c.agent_ipv4("pve1", 100)
        self.assertIsInstance(cm.exception, pc.ProxmoxError)
        self.assertEqual(cm.exception.status_code, 403)
        msg = str(cm.exception)
        self.assertIn("VM.GuestAgent.Audit", msg)
        self.assertIn("NFVAutomation", msg)
        self.assertNotIn(SECRET, msg)

    def test_401_raises(self):
        c = client_with_session(FakeResponse(401, text="authentication failure"))
        with self.assertRaises(pc.ProxmoxAgentPermissionError):
            c.agent_ipv4("pve1", 100)

    def test_wait_stops_at_once_on_403(self):
        c = client_with_session(FakeResponse(403, text="Permission check failed"))
        with mock.patch.object(pc.time, "sleep") as slept:
            with self.assertRaises(pc.ProxmoxAgentPermissionError):
                c.wait_agent_ipv4("pve1", 100, timeout=900, poll=10)
        slept.assert_not_called()
        self.assertEqual(len(c.session.calls), 1)

    def test_wait_polls_through_not_running(self):
        c = client_with_session(
            FakeResponse(500, text="QEMU guest agent is not running"),
            FakeResponse(200, body=AGENT_IFACES),
        )
        with mock.patch.object(pc.time, "sleep"):
            self.assertEqual(c.wait_agent_ipv4("pve1", 100, timeout=60, poll=10), "192.0.2.50")

    def test_http_errors_carry_status_code(self):
        for code, cls in ((404, pc.ProxmoxError), (502, pc.ProxmoxUnreachableError)):
            with self.subTest(code=code):
                c = client_with_session(FakeResponse(code, text="x"))
                with self.assertRaises(cls) as cm:
                    c.get("/version")
                self.assertEqual(cm.exception.status_code, code)


class InfoLogger:
    def __init__(self):
        self.infos = []

    def info(self, msg, *args):
        self.infos.append(msg % args)


IMG = "ubuntu-jumphost-24.04-v2.qcow2"
URL = "http://firmware.example/images/" + IMG
SHA = "a" * 64


class ImageChecksumGuard(unittest.TestCase):
    def test_missing_checksum_refuses(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                with self.assertRaises(pc.ImageIntegrityError) as cm:
                    pc.require_image_checksum(IMG, value, "sha256")
                self.assertIn("REFUSED", str(cm.exception))
                self.assertIn("no checksum", str(cm.exception))
                self.assertIn(IMG, str(cm.exception))

    def test_refusal_is_not_a_swallowable_proxmox_error(self):
        self.assertTrue(issubclass(pc.ImageIntegrityError, ValueError))
        self.assertFalse(issubclass(pc.ImageIntegrityError, pc.ProxmoxError))

    def test_returns_stripped_checksum_and_defaults_algorithm(self):
        self.assertEqual(pc.require_image_checksum(IMG, f" {SHA}\n", None), (SHA, "sha256"))
        self.assertEqual(pc.require_image_checksum(IMG, SHA, "sha512"), (SHA, "sha512"))

    def test_ensure_image_without_checksum_makes_no_node_call(self):
        c = client_with_session()  # any request would pop from an empty list
        with self.assertRaises(pc.ImageIntegrityError):
            c.ensure_image("pve1", "local", IMG, url=URL, checksum="")
        self.assertEqual(c.session.calls, [])

    def test_ensure_image_refuses_even_when_already_cached(self):
        # A filename-keyed cache hit must not launder an unverifiable record.
        c = client_with_session(FakeResponse(200, {"data": [{"volid": f"local:import/{IMG}"}]}))
        with self.assertRaises(pc.ImageIntegrityError):
            c.ensure_image("pve1", "local", IMG, url=URL, checksum=None)
        self.assertEqual(c.session.calls, [])

    def test_ensure_image_pull_passes_checksum_and_logs_truthfully(self):
        log = InfoLogger()
        c = client_with_session(
            FakeResponse(200, {"data": []}),          # storage content: absent
            FakeResponse(200, {"data": UPID}),        # download-url
            FakeResponse(200, {"data": {"status": "stopped", "exitstatus": "OK"}}),
        )
        volid = c.ensure_image("pve1", "local", IMG, url=URL, checksum=SHA,
                               checksum_algorithm="sha256", logger=log)
        self.assertEqual(volid, f"local:import/{IMG}")
        post = c.session.calls[1][1]["data"]
        self.assertEqual(post["checksum"], SHA)
        self.assertEqual(post["checksum-algorithm"], "sha256")
        self.assertTrue(any("sha256-verified" in m for m in log.infos))

    def test_cache_hit_log_does_not_claim_verification(self):
        log = InfoLogger()
        c = client_with_session(FakeResponse(200, {"data": [{"volid": f"local:import/{IMG}"}]}))
        c.ensure_image("pve1", "local", IMG, url=URL, checksum=SHA, logger=log)
        self.assertEqual(len(log.infos), 1)
        self.assertNotIn("verified", log.infos[0])
        self.assertIn("matched by filename", log.infos[0])


class RollbackDecision(unittest.TestCase):
    """Deploy rollback destroys only the VM that carries the device's name."""

    VMS = [
        {"vmid": 104, "name": "jump-01"},
        {"vmid": 105, "name": "pa-fw-01"},
        {"vmid": 106},
    ]

    def test_own_vm_is_destroyed(self):
        self.assertEqual(pc.rollback_vm_decision(self.VMS, 105, "pa-fw-01"), ("destroy", "pa-fw-01"))

    def test_nextid_collision_leaves_other_deploys_vm(self):
        # Job B got vmid 105 too; job A's VM sits there -- B must not destroy it.
        self.assertEqual(pc.rollback_vm_decision(self.VMS, 105, "jump-02"), ("foreign", "pa-fw-01"))

    def test_absent_vmid_is_nothing_to_roll_back(self):
        self.assertEqual(pc.rollback_vm_decision(self.VMS, 199, "pa-fw-01"), ("absent", None))

    def test_unnamed_vm_is_not_assumed_ours(self):
        self.assertEqual(pc.rollback_vm_decision(self.VMS, 106, "pa-fw-01"), ("foreign", None))

    def test_vmid_compared_across_int_and_str(self):
        vms = [{"vmid": "105", "name": "pa-fw-01"}]
        self.assertEqual(pc.rollback_vm_decision(vms, 105, "pa-fw-01")[0], "destroy")

    def test_empty_expected_name_never_destroys(self):
        vms = [{"vmid": 105, "name": ""}]
        self.assertEqual(pc.rollback_vm_decision(vms, 105, "")[0], "foreign")

    def test_empty_or_missing_list(self):
        self.assertEqual(pc.rollback_vm_decision([], 105, "x"), ("absent", None))
        self.assertEqual(pc.rollback_vm_decision(None, 105, "x"), ("absent", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
