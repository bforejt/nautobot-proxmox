#!/usr/bin/env python3
"""
Unit tests for jobs/lib/bmc_identity.py — the "is the BMC at the Device's
xcc IP really this Device?" guard the install and storage-layout jobs run
before any BMC write. Stdlib-only; the module is loaded from its file path.

Run:  python3 tests/test_bmc_identity.py
"""

import importlib.util
import pathlib
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "bmc_identity.py"
spec = importlib.util.spec_from_file_location("bmc_identity", MODULE)
bi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bi)

IP = "10.0.0.50"


class FakeRedfish:
    def __init__(self, info):
        self.info = info
        self.calls = []

    def system_info(self):
        self.calls.append("system_info")
        return self.info


class Identity(unittest.TestCase):
    def test_match_is_trimmed_and_case_insensitive(self):
        rf = FakeRedfish({"serial_number": " j101yceb "})
        self.assertEqual(bi.verify_bmc_identity(rf, "node1", "J101YCEB", IP), "j101yceb")
        self.assertEqual(rf.calls, ["system_info"])

    def test_mismatch_refuses_naming_both_serials(self):
        with self.assertRaises(bi.BmcIdentityError) as ctx:
            bi.verify_bmc_identity(FakeRedfish({"serial_number": "J9OTHER1"}), "node1", "J101YCEB", IP)
        msg = str(ctx.exception)
        self.assertIn("J9OTHER1", msg)
        self.assertIn("J101YCEB", msg)
        self.assertIn(IP, msg)

    def test_bmc_without_serial_refuses(self):
        for info in ({"serial_number": None}, {"serial_number": "  "}, {}):
            with self.assertRaises(bi.BmcIdentityError, msg=info) as ctx:
                bi.check_bmc_identity(info, "node1", "J101YCEB", IP)
            self.assertIn("reports no system serial", str(ctx.exception))

    def test_unreadable_bmc_refuses(self):
        with self.assertRaises(bi.BmcIdentityError) as ctx:
            bi.check_bmc_identity({"error": "ConnectionError: timed out"}, "node1", "J101YCEB", IP)
        self.assertIn("could not read", str(ctx.exception))

    def test_device_without_serial_refuses(self):
        with self.assertRaises(bi.BmcIdentityError):
            bi.check_bmc_identity({"serial_number": "J101YCEB"}, "node1", "", IP)


if __name__ == "__main__":
    unittest.main(verbosity=1)
