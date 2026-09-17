#!/usr/bin/env python3
"""
Unit tests for jobs/lib/answer_service.py — the install job's answer-service
profile preflight. Stdlib-only; the module is loaded from its file path.

Run:  python3 tests/test_answer_service.py
"""

import importlib.util
import pathlib
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
