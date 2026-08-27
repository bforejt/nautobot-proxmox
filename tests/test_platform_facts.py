#!/usr/bin/env python3
"""
Unit tests for jobs/lib/platform_facts.py — the pattern-based NIC ordering
behind the PA-VM deploy path. Stdlib-only; module loaded from its file path.

Run:  python3 tests/test_platform_facts.py
"""

import importlib.util
import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "platform_facts.py"
spec = importlib.util.spec_from_file_location("platform_facts", MODULE)
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)

PA = pf.PLATFORM_FACTS["paloalto-panos"]
UBUNTU = pf.PLATFORM_FACTS["ubuntu-jumphost"]


class FixedList(unittest.TestCase):
    def test_ubuntu_unchanged(self):
        self.assertEqual(pf.resolve_nic_order("ubuntu-jumphost", UBUNTU, ["eth0"]), ["eth0"])

    def test_extra_interfaces_ignored_legacy_semantics(self):
        self.assertEqual(
            pf.resolve_nic_order("ubuntu-jumphost", UBUNTU, ["eth0", "mgmt0"]), ["eth0"]
        )


class Pattern(unittest.TestCase):
    def test_mgmt_plus_two_dataplane(self):
        self.assertEqual(
            pf.resolve_nic_order("paloalto-panos", PA, ["ethernet1/2", "mgmt", "ethernet1/1"]),
            ["mgmt", "ethernet1/1", "ethernet1/2"],
        )

    def test_mgmt_only(self):
        self.assertEqual(pf.resolve_nic_order("paloalto-panos", PA, ["mgmt"]), ["mgmt"])

    def test_missing_mgmt_refused(self):
        with self.assertRaisesRegex(ValueError, "mgmt"):
            pf.resolve_nic_order("paloalto-panos", PA, ["ethernet1/1"])

    def test_gap_refused(self):
        with self.assertRaisesRegex(ValueError, "contiguous"):
            pf.resolve_nic_order("paloalto-panos", PA, ["mgmt", "ethernet1/1", "ethernet1/3"])

    def test_not_starting_at_one_refused(self):
        with self.assertRaisesRegex(ValueError, "contiguous"):
            pf.resolve_nic_order("paloalto-panos", PA, ["mgmt", "ethernet1/2"])

    def test_unmatched_name_refused(self):
        with self.assertRaisesRegex(ValueError, "match neither"):
            pf.resolve_nic_order("paloalto-panos", PA, ["mgmt", "ethernet1/1", "ha1"])

    def test_typo_refused(self):
        with self.assertRaisesRegex(ValueError, "match neither"):
            pf.resolve_nic_order("paloalto-panos", PA, ["mgmt", "ethernet1/01x"])

    def test_leading_zero_index_is_unmatched(self):
        # ethernet1/01 parses as digits but yields index 1 formatted back as
        # ethernet1/1 — guard: 01 must NOT silently alias 1.
        with self.assertRaises(ValueError):
            pf.resolve_nic_order("paloalto-panos", PA, ["mgmt", "ethernet1/01"])


class Facts(unittest.TestCase):
    def test_pa_facts_shape(self):
        self.assertFalse(PA["guest_agent"])
        self.assertEqual(PA["readiness"], "tcp-mgmt")
        self.assertTrue(PA["pin_smbios_uuid"])
        self.assertEqual(PA["scsihw"], "virtio-scsi-single")

    def test_unknown_platform_refused(self):
        with self.assertRaises(ValueError):
            pf.get_platform_facts("nonexistent")


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=1))
