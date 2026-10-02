#!/usr/bin/env python3
"""
Unit tests for jobs/lib/proxmox_client.py task-completion handling: a PVE
task that finished with "WARNINGS: <n>" succeeded (exit code 0) and must not
be treated as a failure (deploy would roll back a VM that started fine).
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

        stub.Session = _Session
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
