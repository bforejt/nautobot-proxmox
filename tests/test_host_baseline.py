#!/usr/bin/env python3
"""
Unit tests for the Host Baseline (decision #55): jobs/lib/host_baseline.py —
config-context validation, the install-NIC derivation, the bond/bridge model
with its /etc/network/interfaces rendering, MAC matching against the node,
/proc/net/bonding evaluation, snmpd.conf rendering, pveum/pvesh planning
(no secret ever on an argv or in a loggable string), payload building and
event parsing — plus the on-node applier (jobs/lib/host_baseline_applier.sh)
driven through `bash -s` with fake pveum/systemctl/... on PATH, and the job
module's call building with a stubbed Nautobot.

Stdlib-only. The applier tests need bash >= 4 on Linux (they run in the
answer-service image; on macOS's bash 3.2 they skip).

Run:  python3 tests/test_host_baseline.py
"""

import base64
import importlib
import importlib.util
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = ROOT / "jobs" / "lib" / "host_baseline.py"
spec = importlib.util.spec_from_file_location("host_baseline", MODULE)
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)

AD_PASSWORD = "Bind-P4ss with spaces 'quote' $dollar"
COMMUNITY = "s3cr3tCommunity"
AUTH_PASS = "auth-Passphrase-1"
PRIV_PASS = "priv Passphrase 2"


def example_context():
    """The documented example config context (docs/sot-data-contract.md §4c)."""
    return {"host_baseline": {
        "packages": ["snmp"],
        "serial_console": {"speed": 115200},
        "zfs_arc_max_bytes": 17179869184,
        "remove_subscription_nag": True,
        "root_email": "noc@example.net",
        "snmp": {
            "contact": "Example NOC <noc@example.net>",
            "community_secret": "snmp_community",
            "v3_users": [{"name": "datadog", "auth_protocol": "SHA", "priv_protocol": "AES"}],
        },
        "ad": {
            "realm": "EXAMPLE-AD",
            "domain": "example.net",
            "servers": ["192.0.2.10", "192.0.2.11"],
            "mode": "ldap",
            "port": 389,
            "base_dn": "DC=example,DC=net",
            "bind_dn": "CN=svc-pve,OU=Service Accounts,DC=example,DC=net",
            "user_filter": "(memberOf=CN=PVE-Admins,OU=Groups,DC=example,DC=net)",
            "group_filter": "(cn=PVE-Admins)",
            "sync_attributes": "email=mail",
            "sync_defaults_options": "remove-vanished=acl;entry;properties",
            "case_sensitive": False,
            "comment": "Example AD",
            "default_realm": True,
            "sync_job": {"name": "pve-admins-sync", "schedule": "*-*-* 06:00:00", "scope": "both",
                         "enable_new": True},
            "admin_group": "PVE-Admins-EXAMPLE-AD",
            "admin_role": "Administrator",
        },
        "service_accounts": [
            {"user": "datadog@pam", "token": "datadog", "role": "PVEAuditor", "path": "/", "privsep": False},
            {"user": "pdm@pve", "token": "pdm", "role": "Administrator", "path": "/", "privsep": False},
        ],
        "network": {"bond_miimon": 100, "lacp_rate": "fast", "rollback_seconds": 180},
    }}


def problems_of(mutate):
    ctx = example_context()
    mutate(ctx["host_baseline"])
    return hb.validate_context(ctx)[1]


class ContextValidation(unittest.TestCase):
    def test_documented_example_is_complete(self):
        cfg, problems = hb.validate_context(example_context())
        self.assertEqual(problems, [])
        self.assertEqual(cfg["packages"], ["lldpd", "snmpd", "snmp"])
        self.assertEqual(cfg["ad"]["servers"], ["192.0.2.10", "192.0.2.11"])
        self.assertEqual(cfg["snmp"]["v3_users"][0]["auth_secret"], "snmpv3_datadog_auth")
        self.assertEqual([a["name"] for a in cfg["service_accounts"]], ["datadog", "pdm"])
        self.assertEqual(cfg["network"]["rollback_seconds"], 180)

    def test_missing_block_is_a_named_refusal(self):
        cfg, problems = hb.validate_context({})
        self.assertIsNone(cfg)
        self.assertIn("config context has no 'host_baseline' block", problems[0])

    def test_sections_are_required_unless_disabled(self):
        for key in ("snmp", "ad", "network"):
            with self.subTest(section=key):
                problems = problems_of(lambda h, k=key: h.pop(k))
                self.assertTrue(any(f"host_baseline.{key} is missing" in p and "enabled: false" in p
                                    for p in problems), problems)
        cfg, problems = hb.validate_context({"host_baseline": dict(
            example_context()["host_baseline"], snmp={"enabled": False}, ad={"enabled": False},
            network={"enabled": False})})
        self.assertEqual(problems, [])
        self.assertIsNone(cfg["snmp"])
        self.assertIsNone(cfg["ad"])
        self.assertIsNone(cfg["network"])

    def test_required_facts_are_named(self):
        cases = {
            "root_email": ("host_baseline.root_email is missing", lambda h: h.pop("root_email")),
            "realm": ("host_baseline.ad.realm is missing", lambda h: h["ad"].pop("realm")),
            "domain": ("host_baseline.ad.domain is missing", lambda h: h["ad"].pop("domain")),
            "servers": ("host_baseline.ad.servers must list one or two AD servers",
                        lambda h: h["ad"].update(servers=[])),
            "mode": ("host_baseline.ad.mode must be ldap, ldaps or ldap+starttls",
                     lambda h: h["ad"].pop("mode")),
            "base_dn": ("host_baseline.ad.base_dn is missing", lambda h: h["ad"].pop("base_dn")),
            "bind_dn": ("host_baseline.ad.bind_dn is missing", lambda h: h["ad"].pop("bind_dn")),
            "sync_job": ("host_baseline.ad.sync_job is missing", lambda h: h["ad"].pop("sync_job")),
            "schedule": ("host_baseline.ad.sync_job.schedule is missing",
                         lambda h: h["ad"]["sync_job"].pop("schedule")),
            "admin_group": ("host_baseline.ad.admin_group is missing", lambda h: h["ad"].pop("admin_group")),
            "admin_role": ("host_baseline.ad.admin_role is missing", lambda h: h["ad"].pop("admin_role")),
            "contact": ("host_baseline.snmp.contact is missing", lambda h: h["snmp"].pop("contact")),
            "accounts": ("host_baseline.service_accounts is missing",
                         lambda h: h.pop("service_accounts")),
            "privsep": ("host_baseline.service_accounts[0].privsep must be true or false",
                        lambda h: h["service_accounts"][0].pop("privsep")),
        }
        for name, (needle, mutate) in cases.items():
            with self.subTest(fact=name):
                problems = problems_of(mutate)
                self.assertTrue(any(needle in p for p in problems), problems)

    def test_values_are_checked(self):
        cases = {
            "pkg": ("is not a valid Debian package name", lambda h: h.update(packages=["ok", "bad pkg"])),
            "email": ("is not an e-mail address", lambda h: h.update(root_email="noc")),
            "newline": ("contains a control character", lambda h: h["snmp"].update(contact="a\nrocommunity x")),
            "realm": ("is not a PVE realm id", lambda h: h["ad"].update(realm="1bad realm")),
            "builtin": ("is a built-in PVE realm", lambda h: h["ad"].update(realm="pam")),
            "schedule": ("is not a systemd calendar event",
                         lambda h: h["ad"]["sync_job"].update(schedule="daily; rm -rf /")),
            "scope": ("scope must be users, groups or both", lambda h: h["ad"]["sync_job"].update(scope="all")),
            "sync_attr": ("is not a PVE sync_attributes list", lambda h: h["ad"].update(sync_attributes="mail")),
            "view": ("is not a view the rendered snmpd.conf defines",
                     lambda h: h["snmp"]["v3_users"][0].update(view="everything")),
            "auth": ("auth_protocol 'MD5' is not one of",
                     lambda h: h["snmp"]["v3_users"][0].update(auth_protocol="MD5")),
            "source": ("is not an IP network", lambda h: h["snmp"].update(community_source="anywhere")),
            "rollback": ("rollback_seconds must be between 60 and 900",
                         lambda h: h["network"].update(rollback_seconds=5)),
            "lacp": ("lacp_rate must be slow or fast", lambda h: h["network"].update(lacp_rate="medium")),
            "miimon_bool": ("bond_miimon must be an integer", lambda h: h["network"].update(bond_miimon=True)),
        }
        for name, (needle, mutate) in cases.items():
            with self.subTest(case=name):
                problems = problems_of(mutate)
                self.assertTrue(any(needle in p for p in problems), problems)

    def test_snmp_must_grant_access(self):
        problems = problems_of(lambda h: h["snmp"].update(community_secret=None, v3_users=[]))
        self.assertTrue(any("grants no access" in p for p in problems), problems)

    def test_v3_user_as_plain_name_gets_the_verified_defaults(self):
        cfg, problems = hb.validate_context({"host_baseline": dict(
            example_context()["host_baseline"], snmp={"contact": "x", "v3_users": ["xcc"]})})
        self.assertEqual(problems, [])
        user = cfg["snmp"]["v3_users"][0]
        self.assertEqual((user["auth_protocol"], user["priv_protocol"]), ("SHA", "AES"))
        self.assertEqual((user["auth_secret"], user["priv_secret"]), ("snmpv3_xcc_auth", "snmpv3_xcc_priv"))

    def test_service_account_rules(self):
        def accounts(*entries):
            return problems_of(lambda h: h.update(service_accounts=list(entries)))

        base = {"token": "tok", "role": "PVEAuditor", "privsep": False}
        self.assertTrue(any("is not managed by the Host Baseline" in p
                            for p in accounts(dict(base, user="root@pam"))))
        self.assertTrue(any("is not managed by the Host Baseline" in p
                            for p in accounts(dict(base, user="svc-nfv@pve"))))
        self.assertTrue(any("is not a PVE token id" in p for p in accounts(dict(base, user="a@pam", token="t"))))
        self.assertTrue(any("is not a PVE user id in the pam or pve realm" in p
                            for p in accounts(dict(base, user="bob@EXAMPLE-AD"))))
        self.assertTrue(any("is listed twice" in p
                            for p in accounts(dict(base, user="a@pam"), dict(base, user="a@pam"))))
        self.assertTrue(any("reserved" in p for p in accounts(dict(base, user="proxmox@pve"))))
        self.assertTrue(any("would collide" in p
                            for p in accounts(dict(base, user="dd@pam", name="dd"),
                                              dict(base, user="dd@pve"))))
        self.assertEqual(accounts(dict(base, user="Data.Dog@pam", name="datadog")), [])

    def test_secret_names_and_value_checks_never_echo_values(self):
        cfg, _ = hb.validate_context(example_context())
        names = hb.secret_names(cfg)
        self.assertIn(("AD bind password", "ad_bind_password"), names)
        self.assertIn(("SNMP community", "snmp_community"), names)
        self.assertIn(("SNMPv3 datadog auth passphrase", "snmpv3_datadog_auth"), names)
        for purpose, value in (("SNMP community", "has space"), ("SNMP community", 'q"uote'),
                               ("SNMPv3 x auth passphrase", "short"),
                               ("SNMPv3 x priv passphrase", 'with"quote-long'),
                               ("AD bind password", "line\nbreak")):
            message = hb.secret_value_problem(purpose, "the-secret", value)
            self.assertIsNotNone(message, (purpose, value))
            self.assertNotIn(value, message)
        self.assertIsNone(hb.secret_value_problem("AD bind password", "n", AD_PASSWORD))
        self.assertIsNone(hb.secret_value_problem("SNMP community", "n", COMMUNITY))
        self.assertIsNone(hb.secret_value_problem("SNMPv3 x priv passphrase", "n", PRIV_PASS))

    def test_referenced_secret_names_for_the_bootstrap(self):
        self.assertEqual(
            hb.referenced_secret_names(example_context()),
            ["ad_bind_password", "snmp_community", "snmpv3_datadog_auth", "snmpv3_datadog_priv"],
        )
        self.assertEqual(hb.referenced_secret_names({"host_baseline": {"ad": {"enabled": False}}}), [])
        self.assertEqual(hb.referenced_secret_names({"other": 1}), [])


# ------------------------------------------------------------- the model

def iface(id, name, type="10gbase-x-sfpp", **kw):
    cf = {}
    for key in ("lag_mode", "lag_xmit_hash", "primary_member"):
        if key in kw:
            cf[key] = kw.pop(key)
    return hb.interface_record(id=id, name=name, type=type, custom_fields=cf, **kw)


PRIMARY = "10.40.3.10/23"
GATEWAY = "10.40.2.1"
NET_CFG = {"bond_miimon": 100, "lacp_rate": "fast", "rollback_seconds": 180}


def fleet_interfaces(**overrides):
    """The SE455 V3 fleet model: mgmt active-backup bond under vmbr0 (the IP),
    data LACP bond under the VLAN-aware vmbr1, jumbo on the data path."""
    rows = {
        "xcc": iface("x", "xcc", "1000base-t", mac="aa:aa:aa:aa:aa:01", ips=["10.40.9.5/24"]),
        "mgmt0": iface("m0", "mgmt0", "1000base-t", mac="3c:ec:ef:00:00:01", lag="b1", primary_member=True),
        "mgmt1": iface("m1", "mgmt1", "1000base-t", mac="3c:ec:ef:00:00:02", lag="b1"),
        "data0": iface("d0", "data0", mac="3c:ec:ef:00:00:03", lag="b0", mtu=9000),
        "data1": iface("d1", "data1", mac="3c:ec:ef:00:00:04", lag="b0", mtu=9000),
        "bond1": iface("b1", "bond1", "lag", bridge="v0", lag_mode="active-backup", description="Mgmt-PC"),
        "bond0": iface("b0", "bond0", "lag", bridge="v1", lag_mode="802.3ad", lag_xmit_hash="layer3+4",
                       mtu=9000, description="Lantrunk-PC"),
        "vmbr0": iface("v0", "vmbr0", "bridge", ips=[PRIMARY], description="Prox-Mgmt"),
        "vmbr1": iface("v1", "vmbr1", "bridge", mode="tagged-all", mtu=9000, description="Bridge for VMs"),
    }
    for name, row in overrides.items():
        if row is None:
            rows.pop(name)
        else:
            rows[name] = row
    return list(rows.values())


def model_of(interfaces=None, primary_ids=("v0",), gateway=GATEWAY, net=NET_CFG):
    return hb.build_network_model("pve-se455-01", interfaces or fleet_interfaces(), PRIMARY,
                                  list(primary_ids), gateway, net)


class InstallNicDerivation(unittest.TestCase):
    def derive(self, interfaces, ids=("v0",)):
        return hb.derive_install_interface("n1", PRIMARY, list(ids), interfaces)

    def test_bridge_lag_primary_member(self):
        nic, chain = self.derive(fleet_interfaces())
        self.assertEqual((nic["name"], nic["mac"]), ("mgmt0", "3c:ec:ef:00:00:01"))
        self.assertEqual(chain, ["vmbr0", "bond1", "mgmt0"])

    def test_plain_port_is_todays_behaviour(self):
        rows = [iface("p", "mgmt", "1000base-t", mac="AA-BB-CC-DD-EE-FF", ips=[PRIMARY])]
        nic, chain = self.derive(rows, ("p",))
        self.assertEqual((nic["name"], nic["mac"], chain), ("mgmt", "aa:bb:cc:dd:ee:ff", ["mgmt"]))

    def test_virtual_interface_carrying_the_ip_keeps_working(self):
        rows = [iface("p", "mgmt", "virtual", mac="aa:bb:cc:dd:ee:ff")]
        self.assertEqual(self.derive(rows, ("p",))[0]["name"], "mgmt")

    def test_bridge_with_single_port(self):
        rows = [iface("v", "vmbr0", "bridge"), iface("p", "eno1", "1000base-t", mac="aa:bb:cc:dd:ee:01", bridge="v")]
        nic, chain = self.derive(rows, ("v",))
        self.assertEqual(chain, ["vmbr0", "eno1"])

    def test_single_member_lag_needs_no_flag(self):
        rows = fleet_interfaces(mgmt1=None, mgmt0=iface("m0", "mgmt0", mac="3c:ec:ef:00:00:01", lag="b1"))
        self.assertEqual(self.derive(rows)[0]["name"], "mgmt0")

    def test_refusals_are_named(self):
        cases = {
            "unassigned": ((), fleet_interfaces(), "is not assigned to any interface of n1"),
            "several": (("v0", "v1"), fleet_interfaces(), "is assigned to several interfaces of n1 (vmbr0, vmbr1)"),
            "no flag": (("v0",), fleet_interfaces(mgmt0=iface("m0", "mgmt0", mac="3c:ec:ef:00:00:01", lag="b1")),
                        "LAG bond1 on n1 has several members (mgmt0, mgmt1) and none is flagged primary_member"),
            "two flags": (("v0",), fleet_interfaces(
                mgmt1=iface("m1", "mgmt1", mac="3c:ec:ef:00:00:02", lag="b1", primary_member=True)),
                "LAG bond1 on n1: primary_member is set on several members (mgmt0, mgmt1)"),
            "empty bridge": (("v0",), fleet_interfaces(bond1=iface("b1", "bond1", "lag", lag_mode="active-backup")),
                             "bridge vmbr0 on n1 has no member interfaces"),
            "empty lag": (("v0",), fleet_interfaces(mgmt0=None, mgmt1=None), "LAG bond1 on n1 has no member interfaces"),
            "bridge in bridge": (("v0",), fleet_interfaces(bond1=iface("b1", "vmbr9", "bridge", bridge="v0")),
                                 "reached vmbr9 (type bridge) via vmbr0 -> vmbr9"),
        }
        for name, (ids, rows, needle) in cases.items():
            with self.subTest(case=name), self.assertRaises(hb.InstallNicError) as ctx:
                self.derive(rows, ids)
            self.assertIn(needle, str(ctx.exception))

    def test_mac_problem_messages(self):
        nic, chain = self.derive(fleet_interfaces(
            mgmt0=iface("m0", "mgmt0", lag="b1", primary_member=True)))
        self.assertIn("install NIC mgmt0 (via vmbr0 -> bond1 -> mgmt0) has no MAC address",
                      hb.install_nic_mac_problem("n1", nic, chain))
        self.assertIn("the interface carrying primary_ip4 (mgmt) has no MAC address — pin the mgmt interface MAC",
                      hb.install_nic_mac_problem("n1", {"name": "mgmt", "mac": None}, ["mgmt"]))
        self.assertIsNone(hb.install_nic_mac_problem("n1", {"name": "x", "mac": "aa:bb:cc:dd:ee:ff"}, ["x"]))


EXPECTED_INTERFACES = hb.INTERFACES_HEADER + """
auto lo
iface lo inet loopback

auto data0
iface data0 inet manual
\tmtu 9000

auto data1
iface data1 inet manual
\tmtu 9000

auto mgmt0
iface mgmt0 inet manual

auto mgmt1
iface mgmt1 inet manual

auto bond0
iface bond0 inet manual
\tbond-slaves data0 data1
\tbond-miimon 100
\tbond-mode 802.3ad
\tbond-xmit-hash-policy layer3+4
\tbond-lacp-rate fast
\tmtu 9000
#Lantrunk-PC

auto bond1
iface bond1 inet manual
\tbond-slaves mgmt0 mgmt1
\tbond-miimon 100
\tbond-mode active-backup
\tbond-primary mgmt0
#Mgmt-PC

auto vmbr0
iface vmbr0 inet static
\taddress 10.40.3.10/23
\tgateway 10.40.2.1
\tbridge-ports bond1
\tbridge-stp off
\tbridge-fd 0
#Prox-Mgmt

auto vmbr1
iface vmbr1 inet manual
\tbridge-ports bond0
\tbridge-stp off
\tbridge-fd 0
\tbridge-vlan-aware yes
\tbridge-vids 2-4094
\tmtu 9000
#Bridge for VMs

source /etc/network/interfaces.d/*
"""


class NetworkModel(unittest.TestCase):
    def test_fleet_model_renders_the_tester_layout(self):
        model, problems = model_of()
        self.assertEqual(problems, [])
        names = {p["id"]: p["name"] for p in model["ports"]}  # pinned: Linux names = Nautobot names
        self.assertEqual(hb.render_interfaces(model, names), EXPECTED_INTERFACES)
        self.assertEqual(model["mgmt_bridge"], "vmbr0")

    def test_linux_names_come_from_the_mac_match(self):
        model, _ = model_of()
        names = {"m0": "nic2", "m1": "nic3", "d0": "nic0", "d1": "nic1"}
        text = hb.render_interfaces(model, names)
        self.assertIn("bond-slaves nic2 nic3", text)
        self.assertIn("bond-primary nic2", text)
        self.assertIn("bond-slaves nic0 nic1", text)
        self.assertNotIn("mgmt0", text)

    def test_active_backup_only_with_one_mtu(self):
        rows = fleet_interfaces(bond0=iface("b0", "bond0", "lag", bridge="v1", lag_mode="active-backup"),
                                data0=iface("d0", "data0", mac="3c:ec:ef:00:00:03", lag="b0", primary_member=True),
                                data1=iface("d1", "data1", mac="3c:ec:ef:00:00:04", lag="b0"),
                                vmbr1=iface("v1", "vmbr1", "bridge", mode="tagged-all"))
        model, problems = model_of(rows, net={"bond_miimon": 100})
        self.assertEqual(problems, [])
        text = hb.render_interfaces(model, {})
        self.assertNotIn("lacp-rate", text)
        self.assertNotIn("xmit-hash", text)
        self.assertNotIn("mtu", text)
        self.assertIn("bond-primary data0", text)

    def test_tagged_bridge_renders_its_vlans(self):
        rows = fleet_interfaces(vmbr1=iface("v1", "vmbr1", "bridge", mode="tagged", tagged_vids=[30, 10, 11, 12, 20]))
        model, problems = model_of(rows)
        self.assertEqual(problems, [])
        self.assertIn("bridge-vids 10-12 20 30", hb.render_interfaces(model, {}))

    def test_refusals_are_named(self):
        cases = {
            "not a bridge": (dict(rows=fleet_interfaces(), ids=("m0",)),
                             "is on mgmt0 (type 1000base-t), not on a bridge"),
            "no mode": (dict(rows=fleet_interfaces(bond0=iface("b0", "bond0", "lag", bridge="v1"))),
                        "LAG bond0 has no valid lag_mode custom field"),
            "lacp no hash": (dict(rows=fleet_interfaces(bond0=iface("b0", "bond0", "lag", bridge="v1",
                                                                     lag_mode="802.3ad", mtu=9000))),
                             "LAG bond0 is 802.3ad but has no lag_xmit_hash"),
            "hash unused": (dict(rows=fleet_interfaces(bond1=iface("b1", "bond1", "lag", bridge="v0",
                                                                    lag_mode="active-backup",
                                                                    lag_xmit_hash="layer2"))),
                            "sets lag_xmit_hash 'layer2' but mode active-backup does not use it"),
            "no miimon": (dict(net={"lacp_rate": "fast"}), "needs host_baseline.network.bond_miimon"),
            "no lacp rate": (dict(net={"bond_miimon": 100}), "LAG bond0 is 802.3ad but host_baseline.network.lacp_rate"),
            "no mac": (dict(rows=fleet_interfaces(data1=iface("d1", "data1", lag="b0", mtu=9000))),
                       "member data1 of LAG bond0 has no MAC address"),
            "no primary": (dict(rows=fleet_interfaces(mgmt0=iface("m0", "mgmt0", mac="3c:ec:ef:00:00:01", lag="b1"))),
                           "LAG bond1 is active-backup with 2 members but none is flagged primary_member"),
            "mtu mismatch": (dict(rows=fleet_interfaces(data1=iface("d1", "data1", mac="3c:ec:ef:00:00:04",
                                                                     lag="b0", mtu=1500))),
                             "data1 mtu 1500 differs from its LAG bond0 mtu 9000"),
            "bridge mtu": (dict(rows=fleet_interfaces(vmbr0=iface("v0", "vmbr0", "bridge", ips=[PRIMARY], mtu=9000),
                                                       bond1=iface("b1", "bond1", "lag", bridge="v0", mtu=1500,
                                                                   lag_mode="active-backup"))),
                           "bridge vmbr0 mtu 9000 is above its port bond1's mtu 1500"),
            "bond name": (dict(rows=fleet_interfaces(bond1=iface("b1", "mgmt-bond", "lag", bridge="v0",
                                                                  lag_mode="active-backup"))),
                          "LAG 'mgmt-bond' must be named bond<N>"),
            "bridge name": (dict(rows=fleet_interfaces(vmbr1=iface("v1", "br-data", "bridge", mode="tagged-all", mtu=9000))),
                            "bridge 'br-data' must be named vmbr<N>"),
            "access bridge": (dict(rows=fleet_interfaces(vmbr1=iface("v1", "vmbr1", "bridge", mode="access", mtu=9000))),
                              "bridge vmbr1 has mode access"),
            "tagged empty": (dict(rows=fleet_interfaces(vmbr1=iface("v1", "vmbr1", "bridge", mode="tagged", mtu=9000))),
                             "bridge vmbr1 is mode tagged but carries no tagged VLANs"),
            "extra ip": (dict(rows=fleet_interfaces(vmbr1=iface("v1", "vmbr1", "bridge", mode="tagged-all", mtu=9000,
                                                                 ips=["10.9.9.9/24"]))),
                         "bridge vmbr1 carries address(es) 10.9.9.9/24 besides primary_ip4"),
            "stray ip": (dict(rows=fleet_interfaces(spare=iface("s", "eno5", "1000base-t", ips=["10.8.8.8/24"]))),
                         "eno5 carries address(es) 10.8.8.8/24 but is not part of the bond/bridge topology"),
            "no gateway": (dict(gateway=None), "no DefaultGW-role IP in primary_ip4's parent prefix"),
            "gateway outside": (dict(gateway="192.0.2.1"), "DefaultGW 192.0.2.1 is outside primary_ip4's network 10.40.2.0/23"),
            "bridge two flags": (dict(rows=fleet_interfaces(
                bond1=iface("b1", "bond1", "lag", bridge="v0", lag_mode="active-backup", primary_member=True),
                spare=iface("s", "eno9", "1000base-t", mac="3c:ec:ef:00:00:09", bridge="v0", primary_member=True))),
                "bridge vmbr0: primary_member is set on several ports (bond1, eno9)"),
            "virtual member": (dict(rows=fleet_interfaces(data1=iface("d1", "data1.10", "virtual", lag="b0",
                                                                       mac="3c:ec:ef:00:00:04"))),
                               "data1.10 (type virtual) is a member of LAG bond0 but is not a physical port"),
        }
        for name, (kw, needle) in cases.items():
            with self.subTest(case=name):
                _, problems = model_of(kw.get("rows"), kw.get("ids", ("v0",)), kw.get("gateway", GATEWAY),
                                       kw.get("net", NET_CFG))
                self.assertTrue(any(needle in p for p in problems), problems)

    def test_gateway_pick(self):
        self.assertEqual(hb.pick_gateway(["10.40.2.1/23"]), ("10.40.2.1", None))
        self.assertEqual(hb.pick_gateway([]), (None, None))
        gw, problem = hb.pick_gateway(["10.40.2.1/23", "10.40.2.2/23"])
        self.assertIsNone(gw)
        self.assertIn("contract §3 allows exactly one", problem)


IP_LINK = """\
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: mgmt0: <BROADCAST,MULTICAST,SLAVE,UP,LOWER_UP> mtu 1500 qdisc mq master bond1 state UP mode DEFAULT group default qlen 1000\\    link/ether 3c:ec:ef:00:00:01 brd ff:ff:ff:ff:ff:ff\\    altname enp1s0f0
3: nic7: <BROADCAST,MULTICAST,SLAVE,UP,LOWER_UP> mtu 1500 qdisc mq master bond1 state UP mode DEFAULT group default qlen 1000\\    link/ether 3c:ec:ef:00:00:01 brd ff:ff:ff:ff:ff:ff permaddr 3c:ec:ef:00:00:02\\    altname enp1s0f1
4: data0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq master vmbr1 state UP mode DEFAULT group default qlen 1000\\    link/ether 3c:ec:ef:00:00:03 brd ff:ff:ff:ff:ff:ff
5: data1: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000\\    link/ether 3c:ec:ef:00:00:04 brd ff:ff:ff:ff:ff:ff
6: bond1: <BROADCAST,MULTICAST,MASTER,UP,LOWER_UP> mtu 1500 qdisc noqueue master vmbr0 state UP mode DEFAULT group default qlen 1000\\    link/ether 3c:ec:ef:00:00:01 brd ff:ff:ff:ff:ff:ff
7: vmbr0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT group default qlen 1000\\    link/ether 3c:ec:ef:00:00:01 brd ff:ff:ff:ff:ff:ff
8: enx0a: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000\\    link/ether 0a:94:ef:00:00:99 brd ff:ff:ff:ff:ff:ff
"""
PHYSICAL = ["mgmt0", "nic7", "data0", "data1", "enx0a"]


class MacMatching(unittest.TestCase):
    def test_parse_ip_link(self):
        links = {link["name"]: link for link in hb.parse_ip_link(IP_LINK)}
        self.assertEqual(links["nic7"]["permaddr"], "3c:ec:ef:00:00:02")
        self.assertEqual(links["nic7"]["mac"], "3c:ec:ef:00:00:01")
        self.assertEqual(links["mgmt0"]["master"], "bond1")
        self.assertEqual(links["data1"]["state"], "DOWN")
        self.assertEqual(links["lo"]["mac"], None)

    def test_ports_match_by_permanent_mac_never_by_name(self):
        model, _ = model_of()
        names, problems = hb.match_ports(model, hb.parse_ip_link(IP_LINK), PHYSICAL)
        self.assertEqual(problems, [])
        # mgmt1 (…:02) is enslaved and shows the bond's MAC; its permaddr finds it as nic7
        self.assertEqual(names, {"m0": "mgmt0", "m1": "nic7", "d0": "data0", "d1": "data1"})

    def test_missing_member_refuses(self):
        rows = fleet_interfaces(data1=iface("d1", "data1", mac="3c:ec:ef:00:00:99", lag="b0", mtu=9000))
        model, _ = model_of(rows)
        names, problems = hb.match_ports(model, hb.parse_ip_link(IP_LINK), PHYSICAL)
        self.assertEqual(len(problems), 1)
        self.assertIn("member data1 (3c:ec:ef:00:00:99) is not among the node's physical NICs", problems[0])
        self.assertIn("data0=3c:ec:ef:00:00:03", problems[0])

    def test_virtual_links_are_never_matched(self):
        model, _ = model_of(fleet_interfaces(mgmt0=iface("m0", "mgmt0", "1000base-t", mac="3c:ec:ef:00:00:01",
                                                           lag="b1", primary_member=True)))
        names, problems = hb.match_ports(model, hb.parse_ip_link(IP_LINK), ["nic7", "data0", "data1"])
        self.assertTrue(any("member mgmt0" in p for p in problems), problems)


BOND_LACP = """\
Ethernet Channel Bonding Driver: v6.14.8-2-pve

Bonding Mode: IEEE 802.3ad Dynamic link aggregation
Transmit Hash Policy: layer3+4 (1)
MII Status: up
MII Polling Interval (ms): 100
Up Delay (ms): 0
Down Delay (ms): 0
Peer Notification Delay (ms): 0

802.3ad info
LACP active: on
LACP rate: fast
Min links: 0
Aggregator selection policy (ad_select): stable
System priority: 65535
System MAC address: 3c:ec:ef:00:00:03
Active Aggregator Info:
\tAggregator ID: 1
\tNumber of ports: 2
\tActor Key: 21
\tPartner Key: 32769
\tPartner Mac Address: 00:11:22:33:44:55

Slave Interface: data0
MII Status: up
Speed: 10000 Mbps
Duplex: full
Link Failure Count: 0
Permanent HW addr: 3c:ec:ef:00:00:03
Slave queue ID: 0
Aggregator ID: 1
Actor Churn State: none
details actor lacp pdu:
    system priority: 65535
    port state: 63

Slave Interface: data1
MII Status: up
Speed: 10000 Mbps
Duplex: full
Link Failure Count: 0
Permanent HW addr: 3c:ec:ef:00:00:04
Slave queue ID: 0
Aggregator ID: 1
"""

BOND_AB = """\
Ethernet Channel Bonding Driver: v6.14.8-2-pve

Bonding Mode: fault-tolerance (active-backup)
Primary Slave: mgmt0 (primary_reselect always)
Currently Active Slave: mgmt0
MII Status: up
MII Polling Interval (ms): 100

Slave Interface: mgmt0
MII Status: up
Permanent HW addr: 3c:ec:ef:00:00:01

Slave Interface: nic7
MII Status: up
Permanent HW addr: 3c:ec:ef:00:00:02
"""


class BondState(unittest.TestCase):
    def setUp(self):
        self.model, _ = model_of()
        self.bond0 = next(b for b in self.model["bonds"] if b["name"] == "bond0")
        self.bond1 = next(b for b in self.model["bonds"] if b["name"] == "bond1")

    def test_parse(self):
        info = hb.parse_proc_bonding(BOND_LACP)
        self.assertEqual(info["mode"], "802.3ad")
        self.assertEqual(info["xmit_hash"], "layer3+4")
        self.assertEqual(info["aggregator"], {"id": 1, "ports": 2, "partner_mac": "00:11:22:33:44:55"})
        self.assertEqual([s["aggregator_id"] for s in info["slaves"]], [1, 1])
        ab = hb.parse_proc_bonding(BOND_AB)
        self.assertEqual((ab["mode"], ab["primary"], ab["active_slave"]), ("active-backup", "mgmt0", "mgmt0"))

    def test_healthy_bonds(self):
        self.assertEqual(hb.evaluate_bond(self.bond0, ["data0", "data1"], None, BOND_LACP), ([], []))
        self.assertEqual(hb.evaluate_bond(self.bond1, ["mgmt0", "nic7"], "mgmt0", BOND_AB), ([], []))

    def test_lacp_state_is_only_a_warning(self):
        no_partner = BOND_LACP.replace("00:11:22:33:44:55", "00:00:00:00:00:00")
        errors, warnings = hb.evaluate_bond(self.bond0, ["data0", "data1"], None, no_partner)
        self.assertEqual(errors, [])
        self.assertTrue(any("LACP has no partner" in w for w in warnings), warnings)
        split = BOND_LACP.replace("Aggregator ID: 1\n", "Aggregator ID: 2\n", 2).replace(
            "\tAggregator ID: 2", "\tAggregator ID: 1")
        errors, warnings = hb.evaluate_bond(self.bond0, ["data0", "data1"], None, split)
        self.assertEqual(errors, [])
        self.assertTrue(any("aggregate with partner" in w for w in warnings), warnings)
        down = BOND_LACP.replace("Slave Interface: data1\nMII Status: up", "Slave Interface: data1\nMII Status: down")
        errors, warnings = hb.evaluate_bond(self.bond0, ["data0", "data1"], None, down)
        self.assertEqual(errors, [])
        self.assertTrue(any("member data1 link is down" in w for w in warnings), warnings)

    def test_intent_mismatch_is_an_error(self):
        errors, _ = hb.evaluate_bond(self.bond0, ["data0", "data1"], None, BOND_AB.replace("mgmt0", "data0"))
        self.assertTrue(any("running mode 'active-backup', SoT says '802.3ad'" in e for e in errors), errors)
        errors, _ = hb.evaluate_bond(self.bond0, ["data0", "data2"], None, BOND_LACP)
        self.assertTrue(any("running members" in e for e in errors), errors)
        errors, _ = hb.evaluate_bond(self.bond0, ["data0", "data1"], None, "")
        self.assertIn("bond0: /proc/net/bonding/bond0 is missing", errors[0])

    def test_active_backup_on_the_backup_port_warns(self):
        failed_over = BOND_AB.replace("Currently Active Slave: mgmt0", "Currently Active Slave: nic7")
        errors, warnings = hb.evaluate_bond(self.bond1, ["mgmt0", "nic7"], "mgmt0", failed_over)
        self.assertEqual(errors, [])
        self.assertTrue(any("active member is nic7" in w for w in warnings), warnings)

    def test_split_dump(self):
        dump = hb.split_bonding_dump(f"=== bond0\n{BOND_LACP}=== bond1\n{BOND_AB}")
        self.assertEqual(sorted(dump), ["bond0", "bond1"])
        self.assertIn("802.3ad", dump["bond0"])


# ------------------------------------------------------------------ SNMP

class Snmp(unittest.TestCase):
    def test_render_tester_layout(self):
        cfg, _ = hb.validate_context(example_context())
        text = hb.render_snmpd_conf(location="LAB-Example-1", contact="Example NOC", community=COMMUNITY,
                                    v3_users=cfg["snmp"]["v3_users"])
        lines = text.splitlines()
        self.assertIn("sysLocation    LAB-Example-1", lines)
        self.assertIn("sysContact     Example NOC", lines)
        self.assertIn("sysServices    72", lines)
        self.assertIn("master  agentx", lines)
        self.assertIn("agentaddress udp:161", lines)
        self.assertIn(f"rocommunity  {COMMUNITY}", lines)
        self.assertIn(f"rocommunity6 {COMMUNITY}", lines)
        self.assertIn("rouser datadog authpriv", lines)
        self.assertEqual(lines[-1], "includeDir /etc/snmp/snmpd.conf.d")

    def test_source_and_view_restrictions(self):
        text = hb.render_snmpd_conf(location="L", contact="C", community=COMMUNITY,
                                    community_source="10.0.0.0/8", community_view="systemonly",
                                    v3_users=[{"name": "u1", "view": "systemonly"}])
        self.assertIn(f"rocommunity  {COMMUNITY} 10.0.0.0/8 -V systemonly", text)
        self.assertNotIn("rocommunity6", text)
        self.assertIn("rouser u1 authpriv -V systemonly", text)
        v3_only = hb.render_snmpd_conf(location="L", contact="C", v3_users=[{"name": "u1"}])
        self.assertNotIn("community", v3_only)
        view_any = hb.render_snmpd_conf(location="L", contact="C", community=COMMUNITY, community_view="systemonly")
        self.assertIn(f"rocommunity  {COMMUNITY} default -V systemonly", view_any)

    def test_location_is_the_location_name(self):
        self.assertEqual(hb.snmp_location("LAB-Example-PA"), ("LAB-Example-PA", None))
        self.assertIsNotNone(hb.snmp_location("Site\nrocommunity x")[1])
        self.assertIsNotNone(hb.snmp_location("")[1])

    def test_fingerprint_tracks_every_credential_part(self):
        base = ("dev-uuid", "datadog", "SHA", AUTH_PASS, "AES", PRIV_PASS)
        fp = hb.snmpv3_fingerprint(*base)
        self.assertEqual(fp, hb.snmpv3_fingerprint(*base))
        self.assertEqual(len(fp), 64)
        for i in range(6):
            changed = list(base)
            changed[i] = changed[i] + "x"
            self.assertNotEqual(fp, hb.snmpv3_fingerprint(*changed), i)
        self.assertNotIn(AUTH_PASS, fp)


# -------------------------------------------------------------- AD / ACLs

def ad_cfg(**overrides):
    cfg, problems = hb.validate_context(example_context())
    assert problems == [], problems
    ad = cfg["ad"]
    ad.update(overrides)
    return ad


CURRENT_REALM = {
    "type": "ad", "domain": "example.net", "server1": "192.0.2.10", "server2": "192.0.2.11", "port": 389,
    "mode": "ldap", "base_dn": "DC=example,DC=net", "bind_dn": "CN=svc-pve,OU=Service Accounts,DC=example,DC=net",
    "filter": "(memberOf=CN=PVE-Admins,OU=Groups,DC=example,DC=net)", "group_filter": "(cn=PVE-Admins)",
    "sync_attributes": "email=mail", "sync-defaults-options": "remove-vanished=acl;entry;properties",
    "case-sensitive": 0, "comment": "Example AD", "default": 1, "digest": "abc",
}


class AdPlanning(unittest.TestCase):
    def test_add_carries_every_option_but_never_the_password(self):
        plan = hb.plan_realm(ad_cfg(), None)
        self.assertEqual(plan["action"], "add")
        argv = plan["argv"]
        self.assertEqual(argv[:6], ["pveum", "realm", "add", "EXAMPLE-AD", "--type", "ad"])
        pairs = dict(zip(argv[6::2], argv[7::2]))
        self.assertEqual(pairs["--server1"], "192.0.2.10")
        self.assertEqual(pairs["--server2"], "192.0.2.11")
        self.assertEqual(pairs["--filter"], "(memberOf=CN=PVE-Admins,OU=Groups,DC=example,DC=net)")
        self.assertEqual(pairs["--case-sensitive"], "0")
        self.assertEqual(pairs["--default"], "1")
        self.assertEqual(pairs["--sync-defaults-options"], "remove-vanished=acl;entry;properties")
        self.assertNotIn("--password", argv)

    def test_in_sync_realm_is_left_alone(self):
        self.assertEqual(hb.plan_realm(ad_cfg(), dict(CURRENT_REALM))["action"], "none")

    def test_modify_sends_only_the_drift(self):
        current = dict(CURRENT_REALM, server2="192.0.2.99", default=0)
        plan = hb.plan_realm(ad_cfg(), current)
        self.assertEqual(plan["action"], "modify")
        self.assertEqual(plan["argv"], ["pveum", "realm", "modify", "EXAMPLE-AD",
                                        "--server2", "192.0.2.11", "--default", "1"])

    def test_pve_boolean_defaults_count_as_values(self):
        current = dict(CURRENT_REALM)
        current.pop("default")  # PVE omits default=0
        plan = hb.plan_realm(ad_cfg(default_realm=False), current)
        self.assertEqual(plan["action"], "none")
        current = dict(CURRENT_REALM)
        current.pop("case-sensitive")  # PVE default 1, SoT says false
        self.assertIn("--case-sensitive", hb.plan_realm(ad_cfg(), current)["argv"])

    def test_dropped_second_server_is_deleted(self):
        plan = hb.plan_realm(ad_cfg(servers=["192.0.2.10"]), dict(CURRENT_REALM))
        self.assertEqual(plan["argv"][-2:], ["--delete", "server2"])

    def test_type_conflict_refuses(self):
        plan = hb.plan_realm(ad_cfg(), dict(CURRENT_REALM, type="ldap"))
        self.assertEqual(plan["action"], "refuse")
        self.assertIn("realm EXAMPLE-AD exists on the node with type 'ldap'", plan["problem"])

    def test_sync_job(self):
        ad = ad_cfg()
        plan = hb.plan_sync_job(ad, [])
        self.assertEqual(plan["argv"], ["pvesh", "create", "/cluster/jobs/realm-sync/pve-admins-sync",
                                        "--realm", "EXAMPLE-AD", "--schedule", "*-*-* 06:00:00",
                                        "--scope", "both", "--enable-new", "1"])
        current = [{"id": "pve-admins-sync", "realm": "EXAMPLE-AD", "schedule": "*-*-* 06:00:00",
                    "scope": "both", "enable-new": 1}]
        self.assertEqual(hb.plan_sync_job(ad, current)["action"], "none")
        moved = [dict(current[0], schedule="*-*-* 04:00:00", enabled=0)]
        plan = hb.plan_sync_job(ad, moved)
        self.assertEqual(plan["argv"], ["pvesh", "set", "/cluster/jobs/realm-sync/pve-admins-sync",
                                        "--schedule", "*-*-* 06:00:00", "--enabled", "1"])
        plan = hb.plan_sync_job(ad, [dict(current[0], realm="OTHER")])
        self.assertEqual(plan["action"], "refuse")
        self.assertIn("a sync job's realm is fixed", plan["problem"])

    def test_realm_sync_argv(self):
        self.assertEqual(hb.realm_sync_argv(ad_cfg()),
                         ["pveum", "realm", "sync", "EXAMPLE-AD", "--scope", "both", "--enable-new", "1"])
        self.assertEqual(hb.realm_sync_argv(ad_cfg(), dry_run=True)[-2:], ["--dry-run", "1"])

    def test_root_email(self):
        users = [{"userid": "root@pam", "email": "old@example.net"}]
        self.assertEqual(hb.plan_root_email("noc@example.net", users),
                         ["pveum", "user", "modify", "root@pam", "--email", "noc@example.net"])
        self.assertIsNone(hb.plan_root_email("old@example.net", users))


USERS = [
    {"userid": "root@pam", "email": "noc@example.net"},
    {"userid": "datadog@pam", "tokens": [{"tokenid": "datadog", "privsep": 0}]},
]
ACLS = [
    {"path": "/", "type": "user", "ugid": "datadog@pam", "roleid": "Administrator", "propagate": 1},
    {"path": "/", "type": "group", "ugid": "PVE-Admins-EXAMPLE-AD", "roleid": "Administrator", "propagate": 1},
]


class AccountPlanning(unittest.TestCase):
    def accounts(self):
        cfg, _ = hb.validate_context(example_context())
        return cfg["service_accounts"]

    def test_hand_built_node_converges_to_the_sot(self):
        plans = hb.plan_service_accounts(self.accounts(), USERS, ACLS, {"datadog": "ok", "pdm": "missing"})
        datadog, pdm = plans
        self.assertIsNone(datadog["user_add"])
        self.assertEqual(datadog["token"], "keep")
        # the hand-built node gave datadog Administrator: the SoT says PVEAuditor -> converge
        self.assertEqual(datadog["acl_deletes"], [["pveum", "acl", "delete", "/", "--users", "datadog@pam",
                                                   "--roles", "Administrator"]])
        self.assertEqual(datadog["acl_adds"], [["pveum", "acl", "modify", "/", "--users", "datadog@pam",
                                                "--roles", "PVEAuditor"]])
        self.assertEqual(pdm["user_add"], ["pveum", "user", "add", "pdm@pve", "--comment", hb.MANAGED_COMMENT])
        self.assertEqual(pdm["token"], "create")

    def test_missing_or_rejected_secret_rotates(self):
        for state, reason in (("missing", "Secrets are missing"), ("invalid", "rejects the stored value")):
            with self.subTest(state=state):
                plan = hb.plan_service_accounts(self.accounts()[:1], USERS, ACLS, {"datadog": state})[0]
                self.assertEqual(plan["token"], "rotate")
                self.assertIn(reason, plan["token_reason"])

    def test_privsep_tokens_get_their_own_acl_and_drift_is_fixed(self):
        accounts = self.accounts()[:1]
        accounts[0]["privsep"] = True
        plan = hb.plan_service_accounts(accounts, USERS, [], {"datadog": "ok"})[0]
        self.assertIn(["pveum", "acl", "modify", "/", "--tokens", "datadog@pam!datadog", "--roles", "PVEAuditor"],
                      plan["acl_adds"])
        self.assertEqual(plan["privsep_fix"], ["pveum", "user", "token", "modify", "datadog@pam", "datadog",
                                               "--privsep", "1"])

    def test_rotated_token_regains_its_acl(self):
        accounts = self.accounts()[:1]
        accounts[0]["privsep"] = True
        acls = [{"path": "/", "type": "token", "ugid": "datadog@pam!datadog", "roleid": "PVEAuditor"},
                {"path": "/", "type": "user", "ugid": "datadog@pam", "roleid": "PVEAuditor"}]
        plan = hb.plan_service_accounts(accounts, USERS, acls, {"datadog": "missing"})[0]
        self.assertEqual(plan["token"], "rotate")
        self.assertEqual(plan["acl_adds"], [["pveum", "acl", "modify", "/", "--tokens", "datadog@pam!datadog",
                                             "--roles", "PVEAuditor"]])

    def test_token_names_mirror_the_phone_home(self):
        self.assertEqual(hb.node_token_names("pve-se455-01", "datadog"), {
            "group": "pve-se455-01-datadog",
            "secret_username": "pve-se455-01-datadog-token-username",
            "secret_secret": "pve-se455-01-datadog-token-secret",
            "file_id": "pve-se455-01_datadog_token_id",
            "file_secret": "pve-se455-01_datadog_token_secret",
        })
        self.assertTrue(hb.valid_token_value("0f5c3a9e-1b2d-4c3e-8f9a-0123456789ab"))
        self.assertFalse(hb.valid_token_value("not-a-token"))


class Identity(unittest.TestCase):
    def test_match_and_mismatch(self):
        self.assertEqual(hb.identity_problems("pve-01", "J101YCEB", "pve-01\n", " j101yceb \n"), [])
        self.assertEqual(hb.identity_problems("PVE-01", "J1", "pve-01.nfv.lab", "J1"), [])
        problems = hb.identity_problems("pve-01", "J101YCEB", "pve-02", "J999")
        self.assertIn("calls itself 'pve-02', not 'pve-01'", problems[0])
        self.assertIn("the node's DMI serial is 'J999', but pve-01's serial is 'J101YCEB'", problems[1])
        problems = hb.identity_problems("pve-01", "J1", "", "")
        self.assertIn("could not read the node's hostname", problems[0])
        self.assertIn("ROOT login", problems[1])


# --------------------------------------------------- payloads and events

class Payload(unittest.TestCase):
    def test_structure_and_quoting(self):
        payload = hb.build_payload("nfv_lib() { :; }\n", [("DRY_RUN", "0"), ("SECRET", AD_PASSWORD),
                                                         ("LIST", ["a b", "c'd"])],
                                   [["nfv_run", "ad", "x y", "pveum", "realm", "sync", "R"]])
        lines = payload.splitlines()
        self.assertEqual(lines[0], "nfv_lib() { :; }")
        self.assertEqual(lines[-1], "__nfv_payload")
        body = payload[payload.index("__nfv_payload() {"):]
        self.assertLess(body.index("set +x"), body.index("local DRY_RUN"))
        self.assertLess(body.index("exec </dev/null"), body.index("nfv_run ad"))
        # nfv_init (path defaults, set +x, the root check) runs before any call
        self.assertLess(body.index("exec </dev/null"), body.index("  nfv_init\n"))
        self.assertLess(body.index("  nfv_init\n"), body.index("nfv_run ad"))
        self.assertLess(body.index("nfv_run ad"), body.index("nfv_done"))
        self.assertIn("local -a LIST=('a b' 'c'\"'\"'d')", payload)
        self.assertIn("nfv_run ad 'x y' pveum realm sync R", payload)
        with self.assertRaises(ValueError):
            hb.sh_local("BAD-NAME", "x")

    def test_planned_commands_never_carry_a_secret(self):
        cfg, _ = hb.validate_context(example_context())
        argvs = [hb.plan_realm(cfg["ad"], None)["argv"], hb.plan_sync_job(cfg["ad"], [])["argv"],
                 hb.realm_sync_argv(cfg["ad"]), hb.plan_root_email(cfg["root_email"], [])]
        for p in hb.plan_service_accounts(cfg["service_accounts"], USERS, ACLS, {}):
            argvs += [p["user_add"] or [], p["privsep_fix"] or []] + p["acl_adds"] + p["acl_deletes"]
        flat = " ".join(hb.sh_call(a) for a in argvs)
        for secret in (AD_PASSWORD, COMMUNITY, AUTH_PASS, PRIV_PASS):
            self.assertNotIn(secret, flat)

    def test_events_and_observation(self):
        out = "\n".join([
            "noise from a command",
            hb.EVENT_PREFIX + json.dumps({"step": "snmp", "item": "snmpd.conf", "status": "ok", "detail": "x"}),
            hb.EVENT_PREFIX + "{not json",
            hb.EVENT_PREFIX + json.dumps({"step": "observe", "item": "pve.roles", "rc": 0,
                                          "b64": base64.b64encode(b'[{"roleid": "PVEAuditor"}]').decode()}),
            hb.EVENT_PREFIX + json.dumps({"step": "observe", "item": "pve.realm", "rc": 2, "b64": ""}),
        ])
        events, noise = hb.parse_events(out)
        self.assertEqual(len(events), 3)
        self.assertEqual(noise, 2)
        obs = hb.decode_observation(events)
        self.assertEqual(hb.json_from(obs, "pve.roles"), [{"roleid": "PVEAuditor"}])
        self.assertIsNone(hb.json_from(obs, "pve.realm"))
        self.assertEqual(hb.json_from(obs, "pve.missing", []), [])

    def test_scrub(self):
        text = f"rc=1 from: something {AD_PASSWORD} and {COMMUNITY}"
        scrubbed = hb.scrub(text, {AD_PASSWORD, COMMUNITY, "", "ab"})
        self.assertNotIn(AD_PASSWORD, scrubbed)
        self.assertNotIn(COMMUNITY, scrubbed)
        self.assertEqual(scrubbed.count("<redacted>"), 2)

    def test_refusal_format(self):
        self.assertEqual(str(hb.BaselineRefusal(["one"])), "Host baseline refused: one")
        self.assertIn("(2) two", str(hb.BaselineRefusal(["one", "two"])))


class ContextSchema(unittest.TestCase):
    def test_patterns_compile_and_example_fits_the_shape(self):
        def walk(node):
            if isinstance(node, dict):
                if "pattern" in node:
                    re.compile(node["pattern"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(hb.CONTEXT_JSON_SCHEMA)
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed (validated in the Nautobot image)")
        jsonschema.Draft7Validator.check_schema(hb.CONTEXT_JSON_SCHEMA)
        jsonschema.validate(example_context(), hb.CONTEXT_JSON_SCHEMA)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"host_baseline": {"ad": {"mode": "plaintext"}}}, hb.CONTEXT_JSON_SCHEMA)
        jsonschema.validate({"host_baseline": {"snmp": {"contact": "partial"}}}, hb.CONTEXT_JSON_SCHEMA)


# ------------------------------------------------- the applier, for real

BASH_OK = platform.system() == "Linux" and shutil.which("bash") and subprocess.run(
    ["bash", "-c", "[ ${BASH_VERSINFO[0]} -ge 4 ]"]).returncode == 0 and shutil.which("python3")

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name] + args) + "\n")
state = os.environ["FAKE_STATE"]
def flag(key, value=None):
    path = os.path.join(state, key)
    if value is None:
        return open(path).read() if os.path.exists(path) else ""
    open(path, "w").write(value)
if name == "apt-cache":
    for p in [a for a in args[1:] if not a.startswith("-")]:
        print(f"{p}:\n  Installed: (none)\n  Candidate: " + ("(none)" if flag(f"no-candidate:{p}") == "yes" else "1.2-3"))
    sys.exit(0)
if name == "systemctl":
    verb = args[0]
    unit = args[-1]
    if verb in ("is-active", "is-enabled"):
        sys.exit(0 if flag(f"{verb}:{unit}") == "yes" else 3)
    if verb == "stop":
        flag(f"is-active:{unit}", "no"); sys.exit(0)
    if verb in ("restart", "start"):
        sys.exit(int(flag("fail-restart") or 0)) if flag("fail-restart") else None
        flag(f"is-active:{unit}", "yes"); sys.exit(0)
    if verb == "enable":
        flag(f"is-enabled:{unit}", "yes")
        if "--now" in args:
            flag(f"is-active:{unit}", "yes")
        sys.exit(0)
    sys.exit(0)
if name == "pveum":
    if args[:3] == ["user", "token", "add"]:
        print(json.dumps({"full-tokenid": f"{args[3]}!{args[4]}", "info": {}, "value": flag("token-value")}))
        sys.exit(0)
    if args[:2] == ["group", "list"]:
        print(flag("groups") or "[]"); sys.exit(0)
    if args[:2] == ["acl", "list"]:
        print(flag("acls") or "[]"); sys.exit(0)
    if args[:2] == ["realm", "sync"]:
        sys.exit(int(flag("sync-rc") or 0))
    sys.exit(0)
if name == "ifup":
    sys.exit(int(flag("ifup-rc") or 0))
if name == "systemd-run":
    sys.exit(int(flag("apply-rc") or 0) if "--wait" in args else 0)
sys.exit(0)
'''


@unittest.skipUnless(BASH_OK, "needs bash >= 4 and python3 on Linux (run in the answer-service image)")
class Applier(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.bin)
        os.makedirs(self.state)
        for name in ("systemctl", "pveum", "pvesh", "ifup", "ifreload", "systemd-run", "apt-get", "apt-cache"):
            path = os.path.join(self.bin, name)
            with open(path, "w") as fh:
                fh.write(FAKE)
            os.chmod(path, 0o755)
        self.log = os.path.join(self.tmp, "argv.log")
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ.get('PATH', '')}", FAKE_LOG=self.log,
                        FAKE_STATE=self.state, NFV_ALLOW_NONROOT="1",
                        NFV_SNMPD_CONF=os.path.join(self.tmp, "snmpd.conf"),
                        NFV_SNMP_PERSIST=os.path.join(self.tmp, "persist", "snmpd.conf"),
                        NFV_STATE_DIR=os.path.join(self.tmp, "nfv-state"),
                        NFV_REALM_PW_DIR=os.path.join(self.tmp, "realm"),
                        NFV_IFACES=os.path.join(self.tmp, "interfaces"),
                        NFV_RUN_DIR=os.path.join(self.tmp, "run"))
        self.applier = hb.APPLIER_PATH.read_text()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def flag(self, key, value):
        with open(os.path.join(self.state, key), "w") as fh:
            fh.write(value)

    def run_payload(self, locals_, calls, truncate=None):
        payload = hb.build_payload(self.applier, locals_, calls)
        if truncate:
            payload = payload[:truncate]
        proc = subprocess.run(["bash", "-s"], input=payload, capture_output=True, text=True, env=self.env,
                              timeout=60)
        events, _ = hb.parse_events(proc.stdout)
        return events, proc

    def argv_log(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def assert_no_secret_on_argv(self, *secrets):
        flat = json.dumps(self.argv_log())
        for secret in secrets:
            self.assertNotIn(secret, flat)

    def snmp_locals(self, auth=AUTH_PASS, names=("datadog",)):
        conf = hb.render_snmpd_conf(location="LAB-1", contact="NOC", community=COMMUNITY,
                                    v3_users=[{"name": n} for n in names])
        fps = [hb.snmpv3_fingerprint("dev", n, "SHA", auth, "AES", PRIV_PASS) for n in names]
        return [("DRY_RUN", "0"), ("SNMPD_CONF_CONTENT", conf), ("V3_NAMES", list(names)),
                ("V3_AUTH_PROTO", ["SHA"] * len(names)), ("V3_AUTH_PASS", [auth] * len(names)),
                ("V3_PRIV_PROTO", ["AES"] * len(names)), ("V3_PRIV_PASS", [PRIV_PASS] * len(names)),
                ("V3_FP", fps)], conf

    def test_truncated_upload_runs_nothing(self):
        payload = hb.build_payload(self.applier, [("DRY_RUN", "0")], [["nfv_run", "x", "y", "pveum", "realm", "list"]])
        cut = payload.index("nfv_run x y")
        events, _ = self.run_payload([("DRY_RUN", "0")], [["nfv_run", "x", "y", "pveum", "realm", "list"]],
                                     truncate=cut)
        self.assertEqual(events, [])
        self.assertEqual(self.argv_log(), [])

    def test_path_defaults_apply_without_overrides(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("NFV_") or k == "NFV_ALLOW_NONROOT"}
        payload = hb.build_payload(self.applier, [], [["nfv_step_paths"]])
        proc = subprocess.run(["bash", "-s"], input=payload, capture_output=True, text=True, env=env, timeout=60)
        paths = {e["item"]: e["detail"] for e in hb.parse_events(proc.stdout)[0] if e["step"] == "paths"}
        self.assertEqual(paths["NFV_SNMPD_CONF"], "/etc/snmp/snmpd.conf")
        self.assertEqual(paths["NFV_SNMP_PERSIST"], "/var/lib/snmp/snmpd.conf")
        self.assertEqual(paths["NFV_REALM_PW_DIR"], "/etc/pve/priv/realm")
        self.assertEqual(paths["NFV_IFACES"], "/etc/network/interfaces")
        self.assertEqual(paths["NFV_RUN_DIR"], "/run/nfv-baseline")
        self.assertEqual(paths["NFV_BONDING"], "/proc/net/bonding")

    def test_unset_dry_run_means_dry(self):
        events, _ = self.run_payload([], [["nfv_run", "x", "y", "pveum", "realm", "list"]])
        self.assertEqual(events[0]["status"], "would_change")
        self.assertEqual(self.argv_log(), [])
        self.assertEqual(events[-1]["step"], "done")

    def test_snmp_apply_rerun_rotate_remove(self):
        locals_, conf = self.snmp_locals()
        events, proc = self.run_payload(locals_, [["nfv_step_snmp"]])
        by_item = {e["item"]: e for e in events}
        self.assertEqual(by_item["snmpd.conf"]["status"], "changed", proc.stderr)
        diff = base64.b64decode(by_item["snmpd.conf"]["diff_b64"]).decode()
        self.assertIn("rocommunity  <redacted>", diff)
        self.assertNotIn(COMMUNITY, diff)
        self.assertEqual(by_item["v3 user datadog"]["status"], "changed")
        self.assertEqual(by_item["snmpd"]["status"], "changed")
        with open(self.env["NFV_SNMPD_CONF"]) as fh:
            self.assertEqual(fh.read(), conf)
        self.assertEqual(os.stat(self.env["NFV_SNMPD_CONF"]).st_mode & 0o777, 0o600)
        with open(self.env["NFV_SNMP_PERSIST"]) as fh:
            persist = fh.read()
        self.assertEqual(persist, f'createUser datadog SHA "{AUTH_PASS}" AES "{PRIV_PASS}"\n')
        calls = [a for a in self.argv_log() if a[0] == "systemctl"]
        self.assertLess(calls.index(["systemctl", "stop", "snmpd"]), calls.index(["systemctl", "restart", "snmpd"]))
        self.assert_no_secret_on_argv(COMMUNITY, AUTH_PASS, PRIV_PASS)
        # re-run: nothing changes, snmpd is not touched
        os.remove(self.log)
        events, _ = self.run_payload(locals_, [["nfv_step_snmp"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "snmp"},
                         {"snmpd.conf": "ok", "v3 user datadog": "ok", "snmpd": "ok"})
        self.assertNotIn(["systemctl", "stop", "snmpd"], self.argv_log())
        # rotated auth passphrase: re-created, the old line purged
        locals_, _ = self.snmp_locals(auth="rotated-Passphrase-9")
        events, _ = self.run_payload(locals_, [["nfv_step_snmp"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "snmp"}["v3 user datadog"], "changed")
        with open(self.env["NFV_SNMP_PERSIST"]) as fh:
            self.assertEqual(fh.read(), f'createUser datadog SHA "rotated-Passphrase-9" AES "{PRIV_PASS}"\n')
        # user dropped from the SoT: purged
        locals_, _ = self.snmp_locals(names=())
        events, _ = self.run_payload(locals_, [["nfv_step_snmp"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "snmp"}["v3 user datadog"], "changed")
        with open(self.env["NFV_SNMP_PERSIST"]) as fh:
            self.assertEqual(fh.read(), "")
        self.assertFalse(os.path.exists(os.path.join(self.env["NFV_STATE_DIR"], "snmpv3", "datadog.fp")))

    def test_snmp_recognises_a_consumed_user_in_hex(self):
        locals_, conf = self.snmp_locals(names=("data_dog",))
        os.makedirs(os.path.dirname(self.env["NFV_SNMP_PERSIST"]))
        hexname = "data_dog".encode().hex()
        with open(self.env["NFV_SNMP_PERSIST"], "w") as fh:
            fh.write(f"usmUser 1 3 0x80001f8880aabbcc 0x{hexname} 0x{hexname} NULL .1.3.6.1.6.3.10.1.1.3 0x01 "
                     ".1.3.6.1.6.3.10.1.2.4 0x02 \"\"\n")
        os.makedirs(os.path.join(self.env["NFV_STATE_DIR"], "snmpv3"))
        with open(os.path.join(self.env["NFV_STATE_DIR"], "snmpv3", "data_dog.fp"), "w") as fh:
            fh.write(dict(locals_)["V3_FP"][0] + "\n")
        events, _ = self.run_payload(locals_, [["nfv_step_snmp"]])
        self.assertEqual({e["item"]: e["status"] for e in events}["v3 user data_dog"], "ok")

    def test_snmp_dry_run_writes_nothing(self):
        locals_, _ = self.snmp_locals()
        locals_[0] = ("DRY_RUN", "1")
        events, _ = self.run_payload(locals_, [["nfv_step_snmp"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "snmp"},
                         {"snmpd.conf": "would_change", "v3 user datadog": "would_change", "snmpd": "would_change"})
        self.assertFalse(os.path.exists(self.env["NFV_SNMPD_CONF"]))
        self.assertFalse(os.path.exists(self.env["NFV_SNMP_PERSIST"]))
        self.assertEqual([a for a in self.argv_log() if a[:2] != ["systemctl", "is-enabled"]
                          and a[:2] != ["systemctl", "is-active"]], [])

    def test_realm_password_goes_to_the_credential_file_only(self):
        call = [["nfv_realm_password", "ad", "EXAMPLE-AD"]]
        events, proc = self.run_payload([("DRY_RUN", "0"), ("REALM_BIND_PASSWORD", AD_PASSWORD)], call)
        self.assertEqual(events[0]["status"], "changed", proc.stderr)
        path = os.path.join(self.env["NFV_REALM_PW_DIR"], "EXAMPLE-AD.pw")
        with open(path) as fh:
            self.assertEqual(fh.read(), AD_PASSWORD)
        events, _ = self.run_payload([("DRY_RUN", "0"), ("REALM_BIND_PASSWORD", AD_PASSWORD)], call)
        self.assertEqual(events[0]["status"], "ok")
        events, _ = self.run_payload([("DRY_RUN", "1"), ("REALM_BIND_PASSWORD", "new-one")], call)
        self.assertEqual(events[0]["status"], "would_change")
        with open(path) as fh:
            self.assertEqual(fh.read(), AD_PASSWORD)
        self.assertNotIn(AD_PASSWORD, json.dumps(events))
        self.assert_no_secret_on_argv(AD_PASSWORD, "new-one")

    def test_realm_sync_failure_is_a_warning_and_acl_waits_for_the_group(self):
        self.flag("sync-rc", "1")
        events, _ = self.run_payload([("DRY_RUN", "0")], [
            ["nfv_realm_sync", "ad", "1", "Admins-R", "pveum", "realm", "sync", "R", "--scope", "both"],
            ["nfv_group_acl", "ad", "/", "Admins-R", "Administrator"],
        ])
        status = {e["item"]: e["status"] for e in events}
        self.assertEqual(status["realm sync"], "warning")
        self.assertEqual(status["acl / Admins-R"], "warning")
        self.assertNotIn(["pveum", "acl", "modify", "/", "--groups", "Admins-R", "--roles", "Administrator"],
                         self.argv_log())
        self.flag("groups", json.dumps([{"groupid": "Admins-R"}]))
        events, _ = self.run_payload([("DRY_RUN", "0")], [["nfv_group_acl", "ad", "/", "Admins-R", "Administrator"]])
        self.assertEqual(events[0]["status"], "changed")
        self.assertIn(["pveum", "acl", "modify", "/", "--groups", "Admins-R", "--roles", "Administrator"],
                      self.argv_log())
        self.flag("acls", json.dumps([{"path": "/", "type": "group", "ugid": "Admins-R", "roleid": "Administrator"}]))
        events, _ = self.run_payload([("DRY_RUN", "0")], [["nfv_group_acl", "ad", "/", "Admins-R", "Administrator"]])
        self.assertEqual(events[0]["status"], "ok")

    def test_token_value_travels_only_on_its_event(self):
        value = "0f5c3a9e-1b2d-4c3e-8f9a-0123456789ab"
        self.flag("token-value", value)
        events, proc = self.run_payload([("DRY_RUN", "0")], [
            ["nfv_token", "accounts", "pdm", "pdm@pve", "pdm", "0", "rotate", "Secrets missing"]])
        token = [e for e in events if e.get("event") == "token"]
        self.assertEqual(len(token), 1, proc.stderr)
        self.assertEqual((token[0]["value"], token[0]["tokenid"], token[0]["account"]), (value, "pdm@pve!pdm", "pdm"))
        argv = self.argv_log()
        self.assertEqual(argv[0], ["pveum", "user", "token", "remove", "pdm@pve", "pdm"])
        self.assertEqual(argv[1][:7], ["pveum", "user", "token", "add", "pdm@pve", "pdm", "--privsep"])
        self.assert_no_secret_on_argv(value)
        self.assertNotIn(value, proc.stderr)

    def test_network_apply_arms_the_rollback_then_applies(self):
        with open(self.env["NFV_IFACES"], "w") as fh:
            fh.write("auto lo\niface lo inet loopback\n")
        events, proc = self.run_payload([("DRY_RUN", "0"), ("NEW_IFACES", EXPECTED_INTERFACES),
                                         ("ROLLBACK_SECONDS", "180"), ("NET_TS", "20261002T120000Z")],
                                        [["nfv_step_network_apply"]])
        status = {e["item"]: e for e in events if e["step"] == "network"}
        self.assertEqual(status["apply"]["status"], "changed", proc.stderr)
        self.assertEqual(status["rollback-timer"]["unit"], "nfv-baseline-net-rollback-20261002T120000Z")
        with open(self.env["NFV_IFACES"]) as fh:
            self.assertEqual(fh.read(), EXPECTED_INTERFACES)
        backup = self.env["NFV_IFACES"] + ".nfv-baseline.20261002T120000Z"
        with open(backup) as fh:
            self.assertEqual(fh.read(), "auto lo\niface lo inet loopback\n")
        self.assertFalse(os.path.exists(self.env["NFV_IFACES"] + ".new"))
        argv = self.argv_log()
        self.assertEqual(argv[0][:4], ["ifup", "-a", "-s", "-i"])
        timer = argv[1]
        self.assertIn("--on-active=180s", timer)
        self.assertIn(f"cp -a '{backup}' '{self.env['NFV_IFACES']}' && ifreload -a; touch ", timer[-1])
        self.assertEqual(argv[2][:2], ["systemd-run", "--unit=nfv-baseline-net-apply-20261002T120000Z"])
        self.assertIn("--wait", argv[2])

    def test_network_apply_refusals_leave_the_node_alone(self):
        with open(self.env["NFV_IFACES"], "w") as fh:
            fh.write("old\n")
        self.flag("ifup-rc", "1")
        events, _ = self.run_payload([("DRY_RUN", "0"), ("NEW_IFACES", EXPECTED_INTERFACES),
                                      ("ROLLBACK_SECONDS", "180"), ("NET_TS", "1")], [["nfv_step_network_apply"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "network"}, {"syntax": "failed"})
        with open(self.env["NFV_IFACES"]) as fh:
            self.assertEqual(fh.read(), "old\n")
        self.assertFalse([a for a in self.argv_log() if a[0] == "systemd-run"])
        # ifreload fails: the applier restores the backup and cancels its timer
        self.flag("ifup-rc", "0")
        self.flag("apply-rc", "1")
        events, _ = self.run_payload([("DRY_RUN", "0"), ("NEW_IFACES", EXPECTED_INTERFACES),
                                      ("ROLLBACK_SECONDS", "180"), ("NET_TS", "2")], [["nfv_step_network_apply"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "network"}["apply"], "failed")
        with open(self.env["NFV_IFACES"]) as fh:
            self.assertEqual(fh.read(), "old\n")
        self.assertIn(["systemctl", "stop", "nfv-baseline-net-rollback-2.timer"], self.argv_log())
        # in sync: nothing armed
        with open(self.env["NFV_IFACES"], "w") as fh:
            fh.write(EXPECTED_INTERFACES)
        os.remove(self.log)
        events, _ = self.run_payload([("DRY_RUN", "0"), ("NEW_IFACES", EXPECTED_INTERFACES),
                                      ("ROLLBACK_SECONDS", "180"), ("NET_TS", "3")], [["nfv_step_network_apply"]])
        self.assertEqual({e["item"]: e["status"] for e in events if e["step"] == "network"}, {"apply": "ok"})
        self.assertEqual(self.argv_log(), [])

    def test_network_confirm(self):
        os.makedirs(self.env["NFV_RUN_DIR"])
        marker = os.path.join(self.env["NFV_RUN_DIR"], "net-rollback-9.done")
        events, _ = self.run_payload([("DRY_RUN", "0"), ("NET_TS", "9")], [["nfv_step_network_confirm"]])
        self.assertEqual(events[0]["status"], "changed")
        self.assertIn(["systemctl", "stop", "nfv-baseline-net-rollback-9.timer"], self.argv_log())
        open(marker, "w").close()
        events, _ = self.run_payload([("DRY_RUN", "0"), ("NET_TS", "9")], [["nfv_step_network_confirm"]])
        self.assertEqual(events[0]["status"], "failed")
        self.assertIn("the rollback already ran", events[0]["detail"])

    def test_json_escaping_survives_hostile_details(self):
        events, _ = self.run_payload([("DRY_RUN", "1")], [
            ["nfv_emit", "x", 'item "quoted" \\ back', "info", "line1\nline2\ttab \x01 ctl"]])
        self.assertEqual(events[0]["item"], 'item "quoted" \\ back')
        self.assertEqual(events[0]["detail"], "line1\nline2\ttab  ctl")


# ------------------------------------------- the job module (stubbed Nautobot)

class _Anything:
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


def _load_job_module():
    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ImportError:
            _stub("requests", RequestException=IOError, ConnectionError=IOError)
            _stub("requests.auth")
    for name in ("nautobot", "nautobot.apps", "nautobot.dcim", "nautobot.dcim.models", "nautobot.ipam",
                 "nautobot.ipam.models", "nautobot.extras", "nautobot.extras.models", "nautobot.extras.choices"):
        _stub(name)
    _stub("nautobot.apps.jobs", Job=object, register_jobs=lambda *a: None)
    pkg = types.ModuleType("jobpkg")
    pkg.__path__ = [str(ROOT / "jobs")]
    sys.modules["jobpkg"] = pkg
    for sub in ("lib", "baremetal"):
        mod = types.ModuleType(f"jobpkg.{sub}")
        mod.__path__ = [str(ROOT / "jobs" / sub)]
        sys.modules[f"jobpkg.{sub}"] = mod
    return importlib.import_module("jobpkg.baremetal.host_baseline")


class _Logger:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda fmt, *args: self.lines.append((level, fmt % args if args else fmt))


class Job(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_job_module()

    def job(self):
        job = self.mod.HostBaseline()
        job.logger = _Logger()
        job._secret_values, job._token_values, job._summary, job._clients = set(), {}, [], []
        job._dry = False
        return job

    @staticmethod
    def device(role="NFV", state="bm_installed"):
        return types.SimpleNamespace(
            name="pve-se455-01", serial="J101YCEB", role=types.SimpleNamespace(name=role),
            cf={"provisioning_state": state}, primary_ip4=None,
            get_config_context=lambda: (_ for _ in ()).throw(AssertionError("config read before the role gate")),
        )

    def test_role_is_checked_before_anything_else(self):
        with self.assertRaises(self.mod.hb.BaselineRefusal) as ctx:
            self.job()._gates(self.device(role="Hypervisor"), True, False)
        self.assertIn("has role 'Hypervisor', not 'NFV' — refusing to baseline it", str(ctx.exception))

    def test_confirm_required_for_a_real_run(self):
        with self.assertRaises(self.mod.hb.BaselineRefusal) as ctx:
            self.job()._gates(self.device(), False, False)
        self.assertIn("Dry run is off but Confirm is not ticked", str(ctx.exception))

    def test_ad_and_account_calls_carry_no_secret(self):
        cfg, _ = hb.validate_context(example_context())
        job = self.job()
        ctx = {"cfg": cfg, "secrets": {("AD bind password", "ad_bind_password"): AD_PASSWORD}}
        plan = {"realm": hb.plan_realm(cfg["ad"], None), "sync": hb.plan_sync_job(cfg["ad"], []),
                "realm_exists": False, "root_email": hb.plan_root_email(cfg["root_email"], []),
                "accounts": hb.plan_service_accounts(cfg["service_accounts"], USERS, ACLS, {"datadog": "ok"})}
        locals_, calls = job._ad_calls(ctx, plan)
        self.assertEqual(locals_, [("REALM_BIND_PASSWORD", AD_PASSWORD)])
        calls += job._account_calls(plan)
        rendered = "\n".join(hb.sh_call(c) for c in calls)
        self.assertNotIn(AD_PASSWORD, rendered)
        self.assertIn("nfv_realm_password ad EXAMPLE-AD", rendered)
        self.assertIn("nfv_realm_sync ad 1 PVE-Admins-EXAMPLE-AD pveum realm sync EXAMPLE-AD", rendered)
        self.assertIn("nfv_token accounts pdm pdm@pve pdm 0 create", rendered)
        self.assertIn("pveum acl delete / --users datadog@pam --roles Administrator", rendered)
        self.assertLess(rendered.index("nfv_realm_password"), rendered.index("nfv_realm_sync"))
        self.assertLess(rendered.index("nfv_realm_sync"), rendered.index("nfv_group_acl"))

    def test_token_values_never_reach_the_log(self):
        job = self.job()
        value = "0f5c3a9e-1b2d-4c3e-8f9a-0123456789ab"
        events = [{"step": "accounts", "item": "token pdm@pve!pdm", "status": "changed", "event": "token",
                   "account": "pdm", "tokenid": "pdm@pve!pdm", "detail": "token created", "value": value}]
        job._take_token_values(events)
        self.assertNotIn("value", events[0])
        stored = []
        self.mod.store_node_token = lambda device, account, token_id, token_value: stored.append(
            (account, token_id, token_value)) or f"{device.name}-{account}"
        job._verify_token = lambda host, token_id, token_value: "ok"
        groups = job._store_tokens(types.SimpleNamespace(name="pve-se455-01"), events, "10.0.0.1")
        self.assertEqual(groups, ["pve-se455-01-pdm"])
        self.assertEqual(stored, [("pdm", "pdm@pve!pdm", value)])
        status, _ = job._report(6, events)
        self.assertEqual(status, "changed")
        self.assertNotIn(value, json.dumps(job.logger.lines))


# ------------------------------------------- docs <-> code (troubleshooting)

# Stable fragments of every refusal/warning the baseline adds (job, library,
# applier, answer service, firstboot). Each must still exist in the code AND
# be quoted in docs/baremetal-install.md's troubleshooting table — so a
# reworded message cannot silently orphan its row (or the reverse).
MESSAGE_FRAGMENTS = [
    # config context + secrets (jobs/lib/host_baseline.py)
    "config context has no 'host_baseline' block",
    "to skip that step on purpose",
    "contains a control character (newline, tab, ...) — not allowed",
    "grants no access",
    "is not managed by the Host Baseline",
    "is reserved (<node>-proxmox holds the deploy token)",
    "would collide",
    "is not a SecretsGroup suffix",
    "is a built-in PVE realm",
    "must be 1-64 printable ASCII characters without spaces, quotes, '#' or backslashes (snmpd.conf token)",
    "must be 8-128 printable ASCII characters without double quotes or backslashes (net-snmp createUser)",
    "contains a line break or NUL — PVE reads only its first line",
    "the Device has no Location name for SNMP sysLocation",
    "contains a control character — cannot be sysLocation",
    # install NIC derivation (library + answer service)
    "is not assigned to any interface of",
    "ambiguous install NIC; keep it on one",
    "has no member interfaces — set the",
    "none is flagged primary_member — flag the port that carries the install",
    "primary_member is set on several members",
    "only bridge -> LAG -> port nesting is supported",
    # network model
    "must sit on exactly one interface of the device",
    "not on a bridge — model the management bridge (vmbr0, type bridge)",
    "no DefaultGW-role IP in primary_ip4's parent prefix (contract §3)",
    "is outside primary_ip4's network",
    "contract §3 allows exactly one",
    "is outside 576-9216",
    "but is not a physical port",
    "is both a LAG member and a bridge port",
    "has no MAC address — the baseline matches ports to the node's NICs by MAC, never by name",
    "must be named bond<N>",
    "must be named vmbr<N>",
    "has no valid lag_mode custom field",
    "but has no lag_xmit_hash — set the transmit hash policy in the SoT (it must match the switch side)",
    "does not use it — clear the field or fix the mode",
    "needs host_baseline.network.bond_miimon in the config context",
    "is 802.3ad but host_baseline.network.lacp_rate is not set",
    "(the kernel forces the bond's MTU onto members)",
    "has another bridge",
    "is set on several ports",
    "is mode tagged but carries no tagged VLANs",
    "has mode access — use tagged-all (VLAN-aware) or no mode (plain bridge)",
    "besides primary_ip4",
    "is not part of the bond/bridge topology — the baseline would drop it",
    "is not among the node's physical NICs",
    "wrong MAC in Nautobot, or the card is missing",
    "appears on several node NICs",
    "(duplicate MAC in Nautobot)",
    # identity + planning
    "wrong primary_ip4 or wrong Device; refusing to touch it",
    "could not read the node's hostname",
    "could not read the node's DMI serial",
    "refusing to touch that machine",
    "exists on the node with type",
    "a sync job's realm is fixed",
    "could not list the node's PVE roles (pveum role list)",
    "(named in host_baseline) does not exist on the node",
    # bond state
    "is missing — the bond is not up",
    "running mode",
    "running members",
    "LACP has no partner",
    "the switch ports are not running LACP for this bundle yet",
    "check the switch-side channel",
    "is not carrying traffic (its link down?)",
    # the job (jobs/baremetal/host_baseline.py)
    "refusing to baseline it",
    "Dry run is off but Confirm is not ticked — refusing to change the node",
    "the Host Baseline applies to",
    "has no serial — the identity check compares it with the node's DMI serial",
    "has no primary_ip4 — the job connects to it and renders it onto the management bridge",
    "does not exist — re-run Bootstrap NFV Data Model",
    "has no readable value",
    "the worker cannot write",
    "could not open an SSH session to",
    "login is not root",
    "Could not verify the stored",
    "the node returned no usable value for",
    "but the node rejects it (401)",
    "the SSH session ended during the apply",
    "the SSH session to the node ended mid-step",
    "the applier did not finish",
    "network apply lost management reachability",
    "the rollback timer fired before the job could cancel it",
    "the apply did not take effect",
    "but the running bonds do not match the SoT",
    "the running bonds do not match the SoT although the file does",
    "pending PVE GUI network changes exist in /etc/network/interfaces.new",
    "failed on the node — see the log",
    # the applier (jobs/lib/host_baseline_applier.sh)
    "the applier must run as root",
    "(trying the install anyway)",
    "(SNMPv3 users are created with snmpd stopped)",
    "could not append createUser to",
    "snmpd is not active after the restart",
    "systemctl enable snmpd failed",
    "the payload carries no bind password",
    "initial realm sync failed",
    "sync --dry-run failed",
    "(realm sync failed, or the group filter excludes it)",
    "token remove rc=",
    "(a re-run rotates a token left without stored Secrets)",
    "token add printed no value",
    "discarding pending PVE GUI network changes staged in",
    "ifupdown2 rejected the rendered file",
    "could not arm the rollback timer",
    "nothing applied, timer cancelled",
    "the rollback already ran",
    "the rollback fired while confirming",
    "it will restore the previous file",
    # answer service + firstboot (bmc/answer_service)
    "static install needs the install port's MAC (derived through the management bridge/LAG) recorded in Nautobot (contract §4)",
    "record the port's MAC on its Nautobot interface",
    "parity must be no, odd or even",
    "has unknown key(s)",
    "zfs_arc_max_bytes must be an integer >= 67108864",
    "remove_subscription_nag must be true or false",
    "packages must be a list of Debian package names",
    "must be a mapping like {speed: 115200}",
    "word must be 5-8",
    "stop must be 1 or 2",
    "config context host_baseline must be a mapping",
    "must be a mapping like {unit: 0}",
    "must be an integer 0-7 (ttyS<unit>)",
    "does not render the host_baseline firstboot input(s)",
    "serial console NOT configured",
    "declares no serial port (install.serial_console) — skipped",
    "FAILED — the Host Baseline job retries it",
    "update-grub FAILED",
    "proxmox-boot-tool refresh FAILED",
    "could not enable serial-getty@",
    "update-initramfs FAILED",
    "step incomplete — continuing",
    "this toolkit version needs a new pattern",
]
SOURCES = [
    ROOT / "jobs" / "lib" / "host_baseline.py",
    ROOT / "jobs" / "lib" / "host_baseline_applier.sh",
    ROOT / "jobs" / "baremetal" / "host_baseline.py",
    ROOT / "jobs" / "baremetal" / "install_node.py",
    ROOT / "jobs" / "lib" / "answer_service.py",
    ROOT / "bmc" / "answer_service" / "app.py",
    ROOT / "bmc" / "answer_service" / "templates" / "firstboot.sh.j2",
]


def _flatten(text):
    """Joined f-string pieces / wrapped lines -> one comparable string."""
    text = re.sub(r'"\s*\n\s*f?"', "", text)  # adjacent string literals across lines
    text = re.sub(r"\s+", " ", text)
    return text


class DocsCoverMessages(unittest.TestCase):
    def test_every_message_is_in_the_code_and_the_runbook(self):
        code = _flatten("\n".join(path.read_text() for path in SOURCES))
        code = code.replace("{CF_PRIMARY_MEMBER}", "primary_member").replace("{CF_LAG_MODE}", "lag_mode")
        code = code.replace("{CF_LAG_XMIT_HASH}", "lag_xmit_hash").replace("{CONTEXT_KEY}", "host_baseline")
        code = code.replace("{hb.HOST_SSH_USERNAME_SECRET}", "host_ssh_username")
        code = code.replace("{ARC_MIN_BYTES}", "67108864").replace("{{", "{").replace("}}", "}")
        if 'nfv_role_refusal(device, "baseline it")' in code:  # "... refusing to {action} ..."
            code += " refusing to baseline it"
        docs = _flatten((ROOT / "docs" / "baremetal-install.md").read_text())
        missing_code = [m for m in MESSAGE_FRAGMENTS if m not in code]
        missing_docs = [m for m in MESSAGE_FRAGMENTS if m not in docs]
        self.assertEqual(missing_code, [], "fragments no longer in the code")
        self.assertEqual(missing_docs, [], "fragments missing from docs/baremetal-install.md troubleshooting")


if __name__ == "__main__":
    unittest.main(verbosity=1)
