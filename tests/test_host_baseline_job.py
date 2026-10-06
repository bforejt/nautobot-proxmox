#!/usr/bin/env python3
"""
End-to-end simulation of the Host Baseline job (decision #55): the REAL
HostBaseline.run() drives the REAL applier through `bash -s`, against a
stateful fake Proxmox node — pveum / pvesh / ip / systemctl / apt-get /
dpkg-query / ifup / systemd-run / hostname on PATH, sysfs/procfs paths and
every managed file redirected into a temp dir (NFV_* overrides). Nautobot is
stubbed: a fake Device with the fleet bond/bridge interfaces, the example
config context, Secrets in a dict, token storage into a dict.

What it proves: gates -> identity -> plan -> steps 3-8 run in order; a dry
run writes nothing; a real run builds the realm (bind password only in the
credential file), sync job, admin ACL, root e-mail, users, tokens (stored,
never logged), snmpd.conf + SNMPv3 user, and applies the network through the
rollback-timer dance with a reconnect (also when the session drops); a
re-run is a no-op; an identity mismatch writes nothing; no secret is ever on
an argv of any fake command or in any log line.

Needs bash >= 4 and python3 on Linux — runs in the answer-service image:
  docker run --rm -v "$PWD:/repo:ro" -w /repo nautobot-composer-answer-service \
      python3 tests/test_host_baseline_job.py
"""

import importlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASH_OK = platform.system() == "Linux" and shutil.which("bash") and subprocess.run(
    ["bash", "-c", "[ ${BASH_VERSINFO[0]} -ge 4 ]"]).returncode == 0 and shutil.which("python3")

AD_PASSWORD = "Bind-P4ss 'quoted' $x"
COMMUNITY = "s3cr3tCommunity"
AUTH_PASS = "auth-Passphrase-1"
PRIV_PASS = "priv Passphrase 2"
SSH_PASSWORD = "root-ssh-Passw0rd"
SECRETS = {
    "host_ssh_username": "root", "host_ssh_password": SSH_PASSWORD, "ad_bind_password": AD_PASSWORD,
    "snmp_community": COMMUNITY, "snmpv3_datadog_auth": AUTH_PASS, "snmpv3_datadog_priv": PRIV_PASS,
}
CONTEXT = {"host_baseline": {
    "root_email": "noc@example.net",
    "snmp": {"contact": "Example NOC", "community_secret": "snmp_community", "v3_users": ["datadog"]},
    "ad": {"realm": "EXAMPLE-AD", "domain": "example.net", "servers": ["192.0.2.10", "192.0.2.11"],
           "mode": "ldap", "base_dn": "DC=example,DC=net", "bind_dn": "CN=svc,DC=example,DC=net",
           "group_filter": "(cn=PVE-Admins)", "case_sensitive": False,
           "sync_job": {"name": "pve-admins-sync", "schedule": "*-*-* 06:00:00", "enable_new": True},
           "admin_group": "PVE-Admins-EXAMPLE-AD", "admin_role": "Administrator"},
    "service_accounts": [
        {"user": "datadog@pam", "token": "datadog", "role": "PVEAuditor", "privsep": False},
        {"user": "pdm@pve", "token": "pdm", "role": "Administrator", "privsep": False},
    ],
    "network": {"bond_miimon": 100, "lacp_rate": "fast", "rollback_seconds": 60},
}}

IP_LINK = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT\\    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
    "2: mgmt0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq master vmbr0 state UP mode DEFAULT\\    link/ether 3c:ec:ef:00:00:01 brd ff:ff:ff:ff:ff:ff\n"
    "3: nic7: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN mode DEFAULT\\    link/ether 3c:ec:ef:00:00:02 brd ff:ff:ff:ff:ff:ff\n"
    "4: data0: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN mode DEFAULT\\    link/ether 3c:ec:ef:00:00:03 brd ff:ff:ff:ff:ff:ff\n"
    "5: data1: <BROADCAST,MULTICAST> mtu 1500 qdisc noop state DOWN mode DEFAULT\\    link/ether 3c:ec:ef:00:00:04 brd ff:ff:ff:ff:ff:ff\n"
    "6: vmbr0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT\\    link/ether 3c:ec:ef:00:00:01 brd ff:ff:ff:ff:ff:ff\n"
)
INSTALLER_INTERFACES = """auto lo
iface lo inet loopback

iface mgmt0 inet manual

auto vmbr0
iface vmbr0 inet static
\taddress 10.40.3.10/23
\tgateway 10.40.2.1
\tbridge-ports mgmt0
\tbridge-stp off
\tbridge-fd 0

source /etc/network/interfaces.d/*
"""
BOND_AFTER = {
    "bond0": "Bonding Mode: IEEE 802.3ad Dynamic link aggregation\nTransmit Hash Policy: layer3+4 (1)\n"
             "MII Status: up\nMII Polling Interval (ms): 100\nLACP rate: fast\nActive Aggregator Info:\n"
             "\tAggregator ID: 1\n\tNumber of ports: 2\n\tPartner Mac Address: 00:00:00:00:00:00\n\n"
             "Slave Interface: data0\nMII Status: up\nAggregator ID: 1\n\n"
             "Slave Interface: data1\nMII Status: up\nAggregator ID: 1\n",
    "bond1": "Bonding Mode: fault-tolerance (active-backup)\nPrimary Slave: mgmt0 (primary_reselect always)\n"
             "Currently Active Slave: mgmt0\nMII Status: up\nMII Polling Interval (ms): 100\n\n"
             "Slave Interface: mgmt0\nMII Status: up\n\nSlave Interface: nic7\nMII Status: up\n",
}

FAKE_NODE = r'''#!/usr/bin/env python3
import json, os, sys, uuid
name = os.path.basename(sys.argv[0]); args = sys.argv[1:]
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name] + args) + "\n")
def save():
    json.dump(state, open(state_path, "w"))
def opts(rest):
    out, i = {}, 0
    while i < len(rest):
        out[rest[i].lstrip("-")] = rest[i + 1]; i += 2
    return out
if name == "hostname":
    print(state["hostname"]); sys.exit(0)
if name == "pveversion":
    print("pve-manager/9.0.10/fake (running kernel: 6.14.8-2-pve)"); sys.exit(0)
if name == "ip":
    print(state["ip_link"], end=""); sys.exit(0)
if name == "dpkg-query":
    pkgs = [a for a in args if not a.startswith("-")]
    if any(a.startswith("-f=${Package}") for a in args):
        for p in pkgs:
            if p in state["installed"]: print(f"{p} ii ")
        sys.exit(0 if all(p in state["installed"] for p in pkgs) else 1)
    print("ii " if pkgs[-1] in state["installed"] else "un ", end=""); sys.exit(0)
if name == "apt-cache":
    for p in [a for a in args[1:] if not a.startswith("-")]:
        cand = "(none)" if p in state.get("no_candidate", []) else "1.2-3"
        print(f"{p}:\n  Installed: (none)\n  Candidate: {cand}")
    sys.exit(0)
if name == "apt-get":
    if "update" in args and state.get("apt_update_rc"):
        print("E: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Could not resolve 'deb.debian.org'")
        sys.exit(state["apt_update_rc"])
    if args and args[0] == "install":
        state["installed"] += [a for a in args[1:] if not a.startswith("-") and "Dpkg" not in a]; save()
    sys.exit(0)
if name == "systemctl":
    verb, unit = args[0], args[-1]
    svc = state["services"]
    if verb == "is-active": sys.exit(0 if svc.get(unit, {}).get("active") else 3)
    if verb == "is-enabled": sys.exit(0 if svc.get(unit, {}).get("enabled") else 1)
    s = svc.setdefault(unit, {})
    if verb == "stop": s["active"] = False
    if verb in ("start", "restart"): s["active"] = True
    if verb == "enable":
        s["enabled"] = True
        if "--now" in args: s["active"] = True
    save(); sys.exit(0)
if name == "ifup":
    sys.exit(0)
if name == "ifreload":
    sys.exit(0)
if name == "systemd-run":
    if "--wait" in args:  # the detached ifreload -a: the kernel now has the SoT's bonds
        for bond, text in state["bond_after"].items():
            open(os.path.join(os.environ["NFV_BONDING"], bond), "w").write(text)
    sys.exit(0)
if name == "pvesh":
    verb, path = args[0], args[1]
    if verb == "get" and path == "/access/domains":
        print(json.dumps([{"realm": r, "type": c.get("type")} for r, c in state["realms"].items()])); sys.exit(0)
    if verb == "get" and path.startswith("/access/domains/"):
        realm = path.rsplit("/", 1)[1]
        if realm not in state["realms"]:
            print(f"domain '{realm}' does not exist", file=sys.stderr); sys.exit(2)
        print(json.dumps(state["realms"][realm])); sys.exit(0)
    if verb == "get" and path == "/cluster/jobs/realm-sync":
        print(json.dumps(list(state["jobs"].values()))); sys.exit(0)
    if verb in ("create", "set") and path.startswith("/cluster/jobs/realm-sync/"):
        jid = path.rsplit("/", 1)[1]
        job = state["jobs"].setdefault(jid, {"id": jid})
        job.update(opts(args[2:])); save(); sys.exit(0)
    sys.exit(1)
if name == "pveum":
    a = args
    if a[:2] == ["role", "list"]:
        print(json.dumps([{"roleid": r} for r in ("Administrator", "PVEAuditor", "NFVAutomation")])); sys.exit(0)
    if a[:2] == ["user", "list"]:
        print(json.dumps(state["users"])); sys.exit(0)
    if a[:2] == ["acl", "list"]:
        print(json.dumps(state["acls"])); sys.exit(0)
    if a[:2] == ["group", "list"]:
        print(json.dumps([{"groupid": g} for g in state["groups"]])); sys.exit(0)
    if a[:2] == ["realm", "add"]:
        conf = opts(a[3:]); state["realms"][a[2]] = conf; save(); sys.exit(0)
    if a[:2] == ["realm", "modify"]:
        state["realms"][a[2]].update(opts(a[3:])); save(); sys.exit(0)
    if a[:2] == ["realm", "sync"]:
        if "--dry-run" not in a and "PVE-Admins-EXAMPLE-AD" not in state["groups"]:
            state["groups"].append("PVE-Admins-EXAMPLE-AD"); save()
        sys.exit(0)
    if a[:2] == ["acl", "modify"] or a[:2] == ["acl", "delete"]:
        path, kind, ugid, role = a[2], a[3].lstrip("-").rstrip("s"), a[4], a[6]
        entry = {"path": path, "type": kind, "ugid": ugid, "roleid": role, "propagate": 1}
        if a[1] == "modify" and entry not in state["acls"]: state["acls"].append(entry)
        if a[1] == "delete" and entry in state["acls"]: state["acls"].remove(entry)
        save(); sys.exit(0)
    if a[:2] == ["user", "add"]:
        state["users"].append({"userid": a[2], "tokens": []}); save(); sys.exit(0)
    if a[:2] == ["user", "modify"]:
        user = next(u for u in state["users"] if u["userid"] == a[2]); user.update(opts(a[3:])); save(); sys.exit(0)
    if a[:3] == ["user", "token", "add"]:
        user = next(u for u in state["users"] if u["userid"] == a[3])
        value = str(uuid.uuid4())
        user.setdefault("tokens", []).append({"tokenid": a[4], "privsep": int(opts(a[5:7])["privsep"])})
        state["token_values"][f"{a[3]}!{a[4]}"] = value; save()
        print(json.dumps({"full-tokenid": f"{a[3]}!{a[4]}", "info": {}, "value": value})); sys.exit(0)
    if a[:3] == ["user", "token", "remove"]:
        user = next(u for u in state["users"] if u["userid"] == a[3])
        user["tokens"] = [t for t in user["tokens"] if t["tokenid"] != a[4]]; save(); sys.exit(0)
    sys.exit(1)
sys.exit(0)
'''


class _Anything:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
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
            _stub("requests", RequestException=IOError)
    for name in ("nautobot", "nautobot.apps", "nautobot.dcim", "nautobot.dcim.models", "nautobot.ipam",
                 "nautobot.ipam.models", "nautobot.extras", "nautobot.extras.models", "nautobot.extras.choices"):
        _stub(name)
    _stub("nautobot.apps.jobs", Job=object, register_jobs=lambda *a: None)
    pkg = types.ModuleType("simjobs")
    pkg.__path__ = [str(ROOT / "jobs")]
    sys.modules["simjobs"] = pkg
    for sub in ("lib", "baremetal"):
        mod = types.ModuleType(f"simjobs.{sub}")
        mod.__path__ = [str(ROOT / "jobs" / sub)]
        sys.modules[f"simjobs.{sub}"] = mod
    return importlib.import_module("simjobs.baremetal.host_baseline")


# ---- fake Nautobot ---------------------------------------------------------

class _List(list):
    def all(self):
        return self

    def prefetch_related(self, *a):
        return self

    def filter(self, **kw):
        return self


class _Addr:
    def __init__(self, cidr):
        self.cidr = cidr
        self.ip = cidr.split("/")[0]

    def __str__(self):
        return self.cidr


def _iface(pk, name, type, lag=None, bridge=None, mac=None, mtu=None, mode="", cf=None, ips=(), desc=""):
    return types.SimpleNamespace(pk=pk, name=name, type=type, lag_id=lag, bridge_id=bridge, mac_address=mac,
                                 mtu=mtu, mode=mode, description=desc, cf=cf or {}, tagged_vlans=_List(),
                                 ip_addresses=_List(types.SimpleNamespace(address=_Addr(i)) for i in ips))


def fake_device(serial="J101YCEB", state="bm_installed"):
    ifaces = _List([
        _iface("m0", "mgmt0", "1000base-t", lag="b1", mac="3C:EC:EF:00:00:01", cf={"primary_member": True}),
        _iface("m1", "mgmt1", "1000base-t", lag="b1", mac="3C:EC:EF:00:00:02"),
        _iface("d0", "data0", "10gbase-x-sfpp", lag="b0", mac="3C:EC:EF:00:00:03", mtu=9000),
        _iface("d1", "data1", "10gbase-x-sfpp", lag="b0", mac="3C:EC:EF:00:00:04", mtu=9000),
        _iface("b1", "bond1", "lag", bridge="v0", cf={"lag_mode": "active-backup"}),
        _iface("b0", "bond0", "lag", bridge="v1", mtu=9000, cf={"lag_mode": "802.3ad", "lag_xmit_hash": "layer3+4"}),
        _iface("v0", "vmbr0", "bridge", ips=["10.40.3.10/23"]),
        _iface("v1", "vmbr1", "bridge", mode="tagged-all", mtu=9000),
        _iface("x", "xcc", "1000base-t", mac="AA:AA:AA:AA:AA:01", ips=["10.40.9.5/24"]),
    ])
    primary = types.SimpleNamespace(address=_Addr("10.40.3.10/23"), parent=object(),
                                    interfaces=_List([ifaces[6]]))
    cf = {"provisioning_state": state}
    device = types.SimpleNamespace(
        name="pve-se455-01", serial=serial, pk="dev-uuid-1", role=types.SimpleNamespace(name="NFV"),
        cf=cf, _custom_field_data=cf, primary_ip4=primary, location=types.SimpleNamespace(name="LAB-Example-1"),
        interfaces=ifaces, saves=[])
    device.get_config_context = lambda: json.loads(json.dumps(CONTEXT))
    device.validated_save = lambda: device.saves.append(dict(cf))
    return device


class _Logger:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda fmt, *args: self.lines.append((level, fmt % args if args else fmt))


# ---- the fake SSH session: runs `bash -s` locally against the fake node ------

class _Session:
    def __init__(self, env, drop_on=None):
        self.env, self.drop_on = env, drop_on
        self.channel = self
        self._buf, self._out, self._pos, self._rc = [], b"", 0, None

    # stdin
    def write(self, data):
        self._buf.append(data)

    def flush(self):
        pass

    def shutdown_write(self):
        payload = "".join(self._buf)
        proc = subprocess.run(["bash", "-s"], input=payload.encode(), capture_output=True, env=self.env,
                              timeout=120)
        self._out, self._err, self._rc = proc.stdout, proc.stderr, proc.returncode
        self._dropped = self.drop_on is not None and self.drop_on in payload

    # stdout / stderr
    def read(self, size=-1):
        if self._dropped:
            raise TimeoutError("simulated: the session died when the management path moved")
        chunk = self._out[self._pos:] if size < 0 else self._out[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def recv_exit_status(self):
        return self._rc


class _Stderr:
    def __init__(self, session):
        self.session = session

    def read(self, size=-1):
        return self.session._err


class _Client:
    def __init__(self, env, drop_on=None):
        self.env, self.drop_on, self.sessions = env, drop_on, 0

    def exec_command(self, command, timeout=None):
        assert command == "bash -s", command
        self.sessions += 1
        session = _Session(self.env, self.drop_on)
        return session, session, _Stderr(session)

    def close(self):
        pass


@unittest.skipUnless(BASH_OK, "needs bash >= 4 and python3 on Linux (run in the answer-service image)")
class JobSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_job_module()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = pathlib.Path(self.tmp)
        (t / "bin").mkdir()
        for name in ("hostname", "pveversion", "ip", "dpkg-query", "apt-get", "apt-cache", "systemctl", "ifup", "ifreload",
                     "systemd-run", "pvesh", "pveum"):
            (t / "bin" / name).write_text(FAKE_NODE)
            (t / "bin" / name).chmod(0o755)
        for nic in ("mgmt0", "nic7", "data0", "data1"):
            (t / "sysnet" / nic / "device").mkdir(parents=True)
        (t / "sysnet" / "vmbr0").mkdir()
        (t / "bonding").mkdir()
        (t / "dmi_serial").write_text("J101YCEB\n")
        (t / "interfaces").write_text(INSTALLER_INTERFACES)
        self.state = t / "state.json"
        self.state.write_text(json.dumps({
            "hostname": "pve-se455-01", "ip_link": IP_LINK, "installed": [], "services": {},
            "realms": {"pam": {"type": "pam"}, "pve": {"type": "pve"}}, "jobs": {}, "groups": [],
            "users": [{"userid": "root@pam", "email": "old@example.net", "tokens": []},
                      {"userid": "svc-nfv@pve", "tokens": [{"tokenid": "deploy", "privsep": 1}]}],
            "acls": [], "token_values": {}, "bond_after": BOND_AFTER,
        }))
        self.log = t / "argv.log"
        self.env = dict(os.environ, PATH=f"{t / 'bin'}:{os.environ.get('PATH', '')}", FAKE_STATE=str(self.state),
                        FAKE_LOG=str(self.log), NFV_ALLOW_NONROOT="1",
                        NFV_SNMPD_CONF=str(t / "snmpd.conf"), NFV_SNMP_PERSIST=str(t / "persist" / "snmpd.conf"),
                        NFV_STATE_DIR=str(t / "nfv-state"), NFV_REALM_PW_DIR=str(t / "realm"),
                        NFV_IFACES=str(t / "interfaces"), NFV_RUN_DIR=str(t / "run"),
                        NFV_DMI_SERIAL=str(t / "dmi_serial"), NFV_SYSNET=str(t / "sysnet"),
                        NFV_BONDING=str(t / "bonding"))
        self.stored = {}
        mod = self.mod
        mod.Secret = types.SimpleNamespace(
            DoesNotExist=KeyError,
            objects=types.SimpleNamespace(get=lambda name: types.SimpleNamespace(
                get_value=lambda obj=None: SECRETS[name])))
        mod.IPAddress = types.SimpleNamespace(objects=types.SimpleNamespace(
            filter=lambda **kw: [types.SimpleNamespace(address=_Addr("10.40.2.1/23"))]))
        mod.stored_node_token = lambda device, account: self.stored.get(account, (None, None))
        mod.store_node_token = lambda device, account, token_id, value: (
            self.stored.__setitem__(account, (token_id, value)) or f"{device.name}-{account}")
        mod.NODE_SECRETS_DIR = self.tmp  # writable
        mod.time.sleep = lambda s: None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def node(self):
        return json.loads(self.state.read_text())

    def job(self, drop_on=None):
        job = self.mod.HostBaseline()
        job.logger = _Logger()
        job._connect = lambda host, timeout=15: _Client(self.env, drop_on)
        job._verify_token = lambda host, token_id, value: "ok"
        return job

    def assert_no_secret_leaked(self, job):
        node = self.node()
        token_values = list(node["token_values"].values())
        logged = json.dumps(job.logger.lines)
        argv = self.log.read_text() if self.log.exists() else ""
        for secret in [AD_PASSWORD, COMMUNITY, AUTH_PASS, PRIV_PASS, SSH_PASSWORD] + token_values:
            self.assertNotIn(secret, logged, "secret in a log line")
            self.assertNotIn(secret, argv, "secret on an argv")

    def test_dry_run_reports_and_writes_nothing(self):
        before = self.state.read_text()
        job = self.job()
        result = job.run(fake_device(), dry_run=True, confirm=False)
        self.assertIn("DRY RUN", result)
        self.assertIn("[7/8] Network (bond/bridge, rollback timer): would change", result)
        self.assertEqual(json.loads(self.state.read_text())["realms"], json.loads(before)["realms"])
        self.assertEqual(self.node()["users"], json.loads(before)["users"])
        self.assertEqual((pathlib.Path(self.tmp) / "interfaces").read_text(), INSTALLER_INTERFACES)
        self.assertFalse((pathlib.Path(self.tmp) / "snmpd.conf").exists())
        self.assertFalse((pathlib.Path(self.tmp) / "realm").exists())
        self.assertEqual(self.stored, {})
        self.assert_no_secret_leaked(job)

    def set_node(self, **fields):
        node = self.node()
        node.update(fields)
        self.state.write_text(json.dumps(node))

    def test_packages_without_an_apt_candidate_fail_before_any_install(self):
        """The tester's node (2026-10-05): apt-get update 'succeeds' with no Debian index, so apt-get
        install died with 'Unable to locate package'. Now the candidates are checked first, the
        update's real error is surfaced, and nothing is installed."""
        self.set_node(no_candidate=["lldpd", "snmpd"], apt_update_rc=100)
        job = self.job()
        with self.assertRaises(self.mod.StepFailed) as ctx:
            job.run(fake_device(), dry_run=False, confirm=True)
        self.assertIn("Packages failed on the node", str(ctx.exception))
        logged = "\n".join(line for _, line in job.logger.lines)
        self.assertIn("[3/8] packages: failed — apt has no installable candidate for: lldpd (none) snmpd (none)", logged)
        self.assertIn("Could not resolve 'deb.debian.org'", logged)
        self.assertIn("APT::Update::Error-Mode=any", logged)
        self.assertEqual(self.node()["installed"], [])
        self.assertEqual(self.node()["realms"].keys(), {"pam", "pve"})  # later steps did not run

    def test_dry_run_warns_when_apt_cannot_install_the_packages(self):
        self.set_node(no_candidate=["snmpd"])
        job = self.job()
        job.run(fake_device(), dry_run=True, confirm=False)
        logged = "\n".join(line for _, line in job.logger.lines)
        self.assertIn("[3/8] apt: warning — apt has no installable candidate for: snmpd (none)", logged)
        self.assertIn("[3/8] packages: would_change — would install: lldpd snmpd", logged)
        self.assertEqual(self.node()["installed"], [])

    def run_apply(self, device=None, drop_on=None):
        device = device or fake_device()
        job = self.job(drop_on)
        result = job.run(device, dry_run=False, confirm=True)
        return job, device, result

    def test_apply_builds_the_node_then_reruns_clean(self):
        job, device, result = self.run_apply()
        self.assertIn("host baseline applied", result)
        node = self.node()
        t = pathlib.Path(self.tmp)
        # packages + services
        self.assertEqual(sorted(node["installed"]), ["lldpd", "snmpd"])
        self.assertTrue(node["services"]["snmpd"]["active"])
        # SNMP
        conf = (t / "snmpd.conf").read_text()
        self.assertIn("sysLocation    LAB-Example-1", conf)
        self.assertIn(f"rocommunity  {COMMUNITY}", conf)
        self.assertEqual((t / "persist" / "snmpd.conf").read_text(),
                         f'createUser datadog SHA "{AUTH_PASS}" AES "{PRIV_PASS}"\n')
        # AD: realm without a password option, password only in the credential file
        realm = node["realms"]["EXAMPLE-AD"]
        self.assertEqual((realm["type"], realm["server2"], realm["default"]), ("ad", "192.0.2.11", "1"))
        self.assertNotIn("password", realm)
        self.assertEqual((t / "realm" / "EXAMPLE-AD.pw").read_text(), AD_PASSWORD)
        self.assertEqual(node["jobs"]["pve-admins-sync"]["schedule"], "*-*-* 06:00:00")
        self.assertIn({"path": "/", "type": "group", "ugid": "PVE-Admins-EXAMPLE-AD", "roleid": "Administrator",
                       "propagate": 1}, node["acls"])
        root = next(u for u in node["users"] if u["userid"] == "root@pam")
        self.assertEqual(root["email"], "noc@example.net")
        # service accounts: tokens created, stored with the node's values, ACLs per the SoT
        self.assertEqual(self.stored["datadog"], ("datadog@pam!datadog", node["token_values"]["datadog@pam!datadog"]))
        self.assertEqual(self.stored["pdm"][0], "pdm@pve!pdm")
        self.assertIn({"path": "/", "type": "user", "ugid": "datadog@pam", "roleid": "PVEAuditor", "propagate": 1},
                      node["acls"])
        # network: rendered file in place, bonds checked, state advanced
        rendered = (t / "interfaces").read_text()
        self.assertIn("bond-slaves mgmt0 nic7", rendered)
        self.assertIn("bond-primary mgmt0", rendered)
        self.assertIn("bridge-vids 2-4094", rendered)
        self.assertTrue(list(t.glob("interfaces.nfv-baseline.*")))
        self.assertEqual(device.cf["provisioning_state"], "baseline_done")
        self.assertEqual(device.saves[-1]["provisioning_state"], "baseline_done")
        warnings = [l for lvl, l in job.logger.lines if lvl == "warning"]
        self.assertTrue(any("LACP has no partner" in w for w in warnings), warnings)  # switch side: warning only
        self.assert_no_secret_leaked(job)
        argv = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(["systemctl", "stop", "snmpd"], argv)
        timer = [a for a in argv if a[0] == "systemd-run" and any(x.startswith("--on-active=") for x in a)]
        self.assertEqual(len(timer), 1)
        self.assertTrue(any(a[:2] == ["systemctl", "stop"] and a[2].endswith(".timer") for a in argv))

        # a re-run converges to nothing
        os.remove(self.log)
        before = self.state.read_text()
        job2, device2, result2 = self.run_apply(fake_device(state="baseline_done"))
        self.assertIn("[7/8] Network (bond/bridge, rollback timer): warning — in sync with the SoT render", result2)
        self.assertEqual(json.loads(self.state.read_text())["realms"], json.loads(before)["realms"])
        argv = [json.loads(line) for line in self.log.read_text().splitlines()]
        writes = [a for a in argv if a[0] in ("pveum", "pvesh") and a[1] in ("add", "modify", "create", "set", "delete")
                  or a[:3] in (["pveum", "user", "token"],) and a[3] in ("add", "remove")
                  or a[0] == "systemd-run" or a[:2] == ["systemctl", "stop"]]
        self.assertEqual(writes, [], "a re-run must not change anything")
        self.assertEqual(device2.saves, [], "baseline_done stays without a save")
        self.assert_no_secret_leaked(job2)

    def test_session_drop_during_the_apply_reconnects(self):
        job, device, result = self.run_apply(drop_on="\n  nfv_step_network_apply\n")
        self.assertIn("host baseline applied", result)
        self.assertTrue(any("the SSH session ended during the apply" in l for _, l in job.logger.lines))
        self.assertTrue(any("rollback timer cancelled" in l for _, l in job.logger.lines))
        self.assertEqual(device.cf["provisioning_state"], "baseline_done")

    def test_wrong_machine_is_refused_before_any_write(self):
        before = self.state.read_text()
        job = self.job()
        with self.assertRaises(self.mod.hb.BaselineRefusal) as ctx:
            job.run(fake_device(serial="OTHER-SERIAL"), dry_run=False, confirm=True)
        self.assertIn("the node's DMI serial is 'J101YCEB', but pve-se455-01's serial is 'OTHER-SERIAL'",
                      str(ctx.exception))
        self.assertEqual(self.state.read_text(), before)
        self.assertFalse((pathlib.Path(self.tmp) / "snmpd.conf").exists())

    def test_missing_member_nic_is_refused_before_any_write(self):
        state = self.node()
        state["ip_link"] = "\n".join(l for l in IP_LINK.splitlines() if "data1" not in l) + "\n"
        self.state.write_text(json.dumps(state))
        shutil.rmtree(pathlib.Path(self.tmp) / "sysnet" / "data1")
        job = self.job()
        with self.assertRaises(self.mod.hb.BaselineRefusal) as ctx:
            job.run(fake_device(), dry_run=False, confirm=True)
        self.assertIn("member data1 (3c:ec:ef:00:00:04) is not among the node's physical NICs", str(ctx.exception))
        self.assertEqual(self.node()["installed"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
