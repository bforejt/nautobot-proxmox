#!/usr/bin/env python3
"""
Unit tests for jobs/lib/published_set.py — the pure parsing/coherence rules
behind the Register Image from Published Set job. Stdlib-only, no Nautobot:
the module is loaded straight from its file path to avoid importing the jobs
package (whose __init__ needs Nautobot).

Run:  python3 tests/test_published_set.py
"""

import importlib.util
import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "published_set.py"
spec = importlib.util.spec_from_file_location("published_set", MODULE)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)

SHA = "fef3033e645a219fe223e2ead67690116b9406149d347429fae736ab407ec4d0"
OTHER_SHA = "a" * 64


class DeriveSetUrls(unittest.TestCase):
    def test_pa_artifact(self):
        name, sidecar, manifest = ps.derive_set_urls(
            "https://fw.lab/images/PA-VM-KVM-11.2.8.qcow2"
        )
        self.assertEqual(name, "PA-VM-KVM-11.2.8.qcow2")
        self.assertEqual(sidecar, "https://fw.lab/images/PA-VM-KVM-11.2.8.qcow2.sha256")
        self.assertEqual(manifest, "https://fw.lab/images/PA-VM-KVM-11.2.8.manifest.json")

    def test_ubuntu_artifact(self):
        name, sidecar, manifest = ps.derive_set_urls(
            "http://fw/images/ubuntu-jumphost-24.04-v3.qcow2"
        )
        self.assertEqual(name, "ubuntu-jumphost-24.04-v3.qcow2")
        self.assertEqual(manifest, "http://fw/images/ubuntu-jumphost-24.04-v3.manifest.json")

    def test_non_qcow2_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.derive_set_urls("https://fw.lab/images/something.iso")

    def test_query_string_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.derive_set_urls("https://fw.lab/images/a.qcow2?token=abc123")

    def test_fragment_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.derive_set_urls("https://fw.lab/images/a.qcow2#frag")


class ParseSidecar(unittest.TestCase):
    def test_sha256sum_format(self):
        self.assertEqual(ps.parse_sidecar(f"{SHA}  a.qcow2\n", "a.qcow2"), SHA)

    def test_binary_mode_star_prefix(self):
        self.assertEqual(ps.parse_sidecar(f"{SHA} *a.qcow2\n", "a.qcow2"), SHA)

    def test_uppercase_hash_normalized(self):
        self.assertEqual(ps.parse_sidecar(f"{SHA.upper()}  a.qcow2", "a.qcow2"), SHA)

    def test_wrong_filename_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.parse_sidecar(f"{SHA}  other.qcow2", "a.qcow2")

    def test_garbage_refused(self):
        for text in ("", "not-a-hash  a.qcow2", SHA, f"{SHA[:-1]}  a.qcow2"):
            with self.assertRaises(ps.PublishedSetError):
                ps.parse_sidecar(text, "a.qcow2")


class ResolveField(unittest.TestCase):
    def test_manifest_only(self):
        self.assertEqual(ps.resolve_field("platform", "paloalto-panos", None), "paloalto-panos")

    def test_input_only(self):
        self.assertEqual(ps.resolve_field("version", None, "24.04-v3"), "24.04-v3")

    def test_agreement(self):
        self.assertEqual(ps.resolve_field("version", "11.2.8", "11.2.8"), "11.2.8")

    def test_conflict_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.resolve_field("version", "11.2.8", "11.2.9")

    def test_neither_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.resolve_field("platform", None, "")

    def test_non_string_manifest_value_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.resolve_field("version", 11.2, None)


class CheckCoherence(unittest.TestCase):
    def test_vendor_manifest_coherent(self):
        ps.check_coherence({"sha256": SHA, "size_bytes": 100}, SHA, 100)

    def test_template_manifest_without_fields(self):
        ps.check_coherence({"name": "x", "packages": []}, SHA, 100)

    def test_sha_mismatch_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.check_coherence({"sha256": OTHER_SHA}, SHA, None)

    def test_size_mismatch_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.check_coherence({"size_bytes": 5}, SHA, 100)

    def test_unknown_served_size_tolerated(self):
        ps.check_coherence({"size_bytes": 5}, SHA, None)

    def test_non_string_sha_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.check_coherence({"sha256": 123}, SHA, None)

    def test_non_integer_size_refused(self):
        with self.assertRaises(ps.PublishedSetError):
            ps.check_coherence({"size_bytes": "6.7GB"}, SHA, 100)


class ParseManifest(unittest.TestCase):
    def test_object_ok(self):
        self.assertEqual(ps.parse_manifest('{"a": 1}'), {"a": 1})

    def test_non_object_refused(self):
        for text in ("[1]", "not json", ""):
            with self.assertRaises(ps.PublishedSetError):
                ps.parse_manifest(text)


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=1))
