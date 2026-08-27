#!/usr/bin/env python3
"""
Unit tests for jobs/lib/pa_bootstrap.py — init-cfg rendering, md5-crypt
(verified against `openssl passwd -1` vectors), bootstrap.xml, netmask
conversion, and (when pycdlib is installed) ISO mastering. Stdlib-only;
module loaded from its file path to avoid the jobs package's Nautobot import.

Run:  python3 tests/test_pa_bootstrap.py
"""

import importlib.util
import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "pa_bootstrap.py"
spec = importlib.util.spec_from_file_location("pa_bootstrap", MODULE)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)


class Md5Crypt(unittest.TestCase):
    # Vectors generated with: openssl passwd -1 -salt <salt> '<password>'
    def test_openssl_vector_long(self):
        self.assertEqual(
            pa.md5crypt("Test-Passw0rd!", "saltsalt"),
            "$1$saltsalt$BkFFMJJztml/EKYp.vmns1",
        )

    def test_openssl_vector_short(self):
        self.assertEqual(pa.md5crypt("x", "abc"), "$1$abc$OGyl6dDvZCDiGmIVbeuCq/")

    def test_random_salt_shape(self):
        phash = pa.md5crypt("secret")
        self.assertRegex(phash, r"^\$1\$[./0-9A-Za-z]{8}\$[./0-9A-Za-z]{22}$")

    def test_bad_salt_refused(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.md5crypt("x", "toolongsalt99")


class Netmask(unittest.TestCase):
    def test_common(self):
        self.assertEqual(pa.netmask_from_prefixlen(24), "255.255.255.0")
        self.assertEqual(pa.netmask_from_prefixlen(27), "255.255.255.224")
        self.assertEqual(pa.netmask_from_prefixlen(16), "255.255.0.0")


class InitCfg(unittest.TestCase):
    def test_static_standalone(self):
        text = pa.render_init_cfg(
            "fw-01", dhcp=False, ip="10.40.200.5", netmask="255.255.255.0",
            gateway="10.40.200.1", dns=["10.40.200.2", "10.40.200.3"],
        )
        self.assertIn("type=static\n", text)
        self.assertIn("ip-address=10.40.200.5\n", text)
        self.assertIn("netmask=255.255.255.0\n", text)
        self.assertIn("default-gateway=10.40.200.1\n", text)
        self.assertIn("hostname=fw-01\n", text)
        self.assertIn("dns-primary=10.40.200.2\n", text)
        self.assertIn("dns-secondary=10.40.200.3", text)
        self.assertNotIn("panorama", text)

    def test_static_requires_dns(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.render_init_cfg("fw", dhcp=False, ip="1.2.3.4",
                               netmask="255.255.255.0", gateway="1.2.3.1", dns=[])

    def test_static_requires_net_triple(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.render_init_cfg("fw", dhcp=False, ip="1.2.3.4", dns=["1.1.1.1"])

    def test_dhcp(self):
        text = pa.render_init_cfg("fw-02", dhcp=True)
        self.assertIn("type=dhcp-client", text)
        self.assertIn("dhcp-send-hostname=yes", text)
        self.assertNotIn("ip-address", text)

    def test_dhcp_forbids_static_fields(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.render_init_cfg("fw", dhcp=True, ip="1.2.3.4")

    def test_scm_registration(self):
        text = pa.render_init_cfg(
            "fw-03", dhcp=True, scm_pin_id="pin-id-1", scm_pin_value="pin-val-1"
        )
        self.assertIn("panorama-server=cloud\n", text)
        self.assertIn("vm-series-auto-registration-pin-id=pin-id-1\n", text)
        self.assertIn("vm-series-auto-registration-pin-value=pin-val-1", text)

    def test_scm_half_pin_refused(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.render_init_cfg("fw", dhcp=True, scm_pin_id="only-id")

    def test_bad_hostnames_refused(self):
        for name in ("", "a" * 32, "fw 01", "fw\ninjected=1", "fw:1", "fw/1"):
            with self.assertRaises(pa.PaBootstrapError):
                pa.render_init_cfg(name, dhcp=True)

    def test_hostname_charset_accepted(self):
        text = pa.render_init_cfg("Fw-01.lab_x", dhcp=True)
        self.assertIn("hostname=Fw-01.lab_x\n", text)


class BootstrapXml(unittest.TestCase):
    def test_minimal(self):
        xml = pa.render_bootstrap_xml("$1$abc$OGyl6dDvZCDiGmIVbeuCq/")
        self.assertIn('<entry name="admin">', xml)
        self.assertIn("<phash>$1$abc$OGyl6dDvZCDiGmIVbeuCq/</phash>", xml)
        self.assertIn("<superuser>yes</superuser>", xml)
        self.assertIn('version="10.1.0"', xml)  # safe default floor

    def test_config_version_from_image(self):
        xml = pa.render_bootstrap_xml("$1$abc$OGyl6dDvZCDiGmIVbeuCq/", config_version="11.2.0")
        self.assertIn('<config version="11.2.0"', xml)

    def test_bad_config_version_refused(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.render_bootstrap_xml("$1$abc$OGyl6dDvZCDiGmIVbeuCq/", config_version="11.2")

    def test_plaintext_refused(self):
        with self.assertRaises(pa.PaBootstrapError):
            pa.render_bootstrap_xml("plaintext-password")


class IsoBuild(unittest.TestCase):
    def setUp(self):
        try:
            import pycdlib  # noqa: F401
        except ImportError:
            self.skipTest("pycdlib not installed locally (present in the composer image)")

    def test_iso_layout(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/test-bootstrap.iso"
            pa.build_bootstrap_iso(
                path, "type=dhcp-client\nhostname=fw\n",
                bootstrap_xml=pa.render_bootstrap_xml(pa.md5crypt("pw", "abc")),
                authcodes="D1234567\n",
            )
            import pycdlib
            iso = pycdlib.PyCdlib()
            iso.open(path)
            names = set()
            for dirname, dirlist, filelist in iso.walk(rr_path="/"):
                for f in filelist:
                    names.add(f"{dirname.rstrip('/')}/{f}")
                for d in dirlist:
                    names.add(f"{dirname.rstrip('/')}/{d}/")
            iso.close()
            self.assertIn("/config/init-cfg.txt", names)
            self.assertIn("/config/bootstrap.xml", names)
            self.assertIn("/license/authcodes", names)
            for d in ("config", "license", "software", "content"):
                self.assertIn(f"/{d}/", names)


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=1))
