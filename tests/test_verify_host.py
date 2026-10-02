#!/usr/bin/env python3
"""
Unit tests for the host-verification job's data-storage preflights (F47):
jobs/baremetal/verify_host.py must apply the firstboot hook's candidate rule
(firstboot.sh.j2) — a disk carrying children, a filesystem / zfs_member /
LVM2_member signature or a partition table is never built on, removable media
is ignored, and a pool / volume group of the profile's own name is imported /
reused — instead of PASSing layouts firstboot would skip.
Stdlib-only: requests and the nautobot modules are stubbed, the job package is loaded from
its file paths, and the SSH runner is a canned-output fake.

Run:  python3 tests/test_verify_host.py
"""

import importlib
import logging
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


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


if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        _stub("requests", RequestException=IOError, ConnectionError=IOError)
        _stub("requests.auth")
for _name in ("nautobot", "nautobot.apps", "nautobot.dcim", "nautobot.dcim.models",
              "nautobot.extras", "nautobot.extras.models"):
    _stub(_name)
_stub("nautobot.apps.jobs", Job=object, register_jobs=lambda *a: None)
_pkg = types.ModuleType("jobpkg")
_pkg.__path__ = [str(ROOT / "jobs")]
sys.modules["jobpkg"] = _pkg
for _sub in ("lib", "baremetal"):
    _m = types.ModuleType(f"jobpkg.{_sub}")
    _m.__path__ = [str(ROOT / "jobs" / _sub)]
    sys.modules[f"jobpkg.{_sub}"] = _m
vh = importlib.import_module("jobpkg.baremetal.verify_host")

GIB = 1024 ** 3
POOL_PROFILE = {"install": {
    "filesystem": "zfs", "zfs": {"raid": "raid1"}, "disk_filter": {"ID_MODEL": "*480*"},
    "data_pool": {"name": "datastore", "pve_storage": "datastore", "count": 2,
                  "min_size_gib": 1000},
}}
VOL_PROFILE = {"install": {
    "filesystem": "ext4", "disk_filter": {"ID_PATH": "*-scsi-0:2:0:0"},
    "data_volume": {"vg": "datastore", "thinpool": "data", "pve_storage": "datastore",
                    "min_size_gib": 1000},
}}


def job(disks=(), boot=()):
    j = vh.VerifySe350Host()
    j.logger = logging.getLogger("verify_host_test")
    j._disks = list(disks)
    j._boot_disks = [d for d in disks if d["DEVNAME"] in boot]
    return j


def disk(dev, gib, **extra):
    d = {"DEVNAME": dev, "_bytes": gib * GIB, "_size": f"{gib}G", "_parts": 0, "_rm": "0"}
    d.update(extra)
    return d


def jbod(**data_extra):
    return [disk("/dev/sda", 447, _parts=3), disk("/dev/sdb", 447, _parts=3),
            disk("/dev/sdc", 1788), disk("/dev/sdd", 1788, **data_extra)]


class DataPool(unittest.TestCase):
    def check(self, disks):
        return job(disks, boot={"/dev/sda", "/dev/sdb"})._check_data_pool(POOL_PROFILE)

    def test_clean_pair_passes(self):
        verdict, detail = self.check(jbod())
        self.assertEqual(verdict, "PASS", detail)
        self.assertIn("/dev/sdc", detail)

    def test_signatures_and_children_fail(self):
        for extra, expect in (
            ({"ID_FS_TYPE": "LVM2_member"}, "a LVM2_member signature"),
            ({"ID_FS_TYPE": "zfs_member"}, "a zfs_member signature"),
            ({"ID_FS_TYPE": "ext4"}, "a ext4 signature"),
            ({"ID_PART_TABLE_TYPE": "gpt"}, "a gpt partition table"),
            ({"_parts": 2}, "2 partition(s)/holder(s)"),
        ):
            with self.subTest(extra=extra):
                verdict, detail = self.check(jbod(**extra))
                self.assertEqual(verdict, "FAIL", detail)
                self.assertIn(f"/dev/sdd (1788G) carries {expect}", detail)
                self.assertIn("would skip the pool", detail)

    def test_foreign_pool_fails(self):
        node = {"NAME": "sdd1", "TYPE": "part", "FSTYPE": "zfs_member", "LABEL": "tank"}
        verdict, detail = self.check(jbod(_parts=2, _nodes=[node]))
        self.assertEqual(verdict, "FAIL", detail)

    def test_own_pool_is_imported(self):
        nodes = [{"NAME": "sdc", "TYPE": "disk", "FSTYPE": "", "LABEL": ""},
                 {"NAME": "sdc1", "TYPE": "part", "FSTYPE": "zfs_member", "LABEL": "datastore"}]
        disks = jbod(_parts=2)
        disks[2].update(_parts=2, ID_PART_TABLE_TYPE="gpt", _nodes=nodes)
        verdict, detail = self.check(disks)
        self.assertEqual(verdict, "PASS", detail)
        self.assertIn("already exists on ['/dev/sdc']", detail)
        self.assertIn("imports it", detail)

    def test_own_pool_name_on_boot_disk_does_not_count(self):
        node = {"NAME": "sda3", "TYPE": "part", "FSTYPE": "zfs_member", "LABEL": "datastore"}
        disks = jbod(ID_FS_TYPE="LVM2_member")
        disks[0]["_nodes"] = [node]
        self.assertEqual(self.check(disks)[0], "FAIL")

    def test_removable_media_is_ignored(self):
        disks = jbod() + [disk("/dev/sde", 3726, _rm="1")]
        self.assertEqual(self.check(disks)[0], "PASS")

    def test_count_mismatch_still_fails(self):
        disks = jbod()[:3]
        verdict, detail = self.check(disks)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("needs exactly 2 equal-sized", detail)


class DataVolume(unittest.TestCase):
    def disks(self, **data_extra):
        return [disk("/dev/sda", 446, _parts=3, ID_PART_TABLE_TYPE="gpt"),
                disk("/dev/sdb", 1786, **data_extra)]

    def check(self, disks):
        return job(disks, boot={"/dev/sda"})._check_data_volume(VOL_PROFILE)

    def test_clean_volume_passes(self):
        verdict, detail = self.check(self.disks())
        self.assertEqual(verdict, "PASS", detail)
        self.assertIn("would use /dev/sdb", detail)

    def test_foreign_lvm_pv_fails(self):
        verdict, detail = self.check(self.disks(ID_FS_TYPE="LVM2_member", _pv_vgs=["vmdata"]))
        self.assertEqual(verdict, "FAIL", detail)
        self.assertIn("/dev/sdb (1786G) carries a LVM2_member signature", detail)
        self.assertIn("vgrename", detail)

    def test_partitioned_disk_fails(self):
        verdict, detail = self.check(self.disks(_parts=1, ID_PART_TABLE_TYPE="dos"))
        self.assertEqual(verdict, "FAIL", detail)
        self.assertIn("1 partition(s)/holder(s), a dos partition table", detail)

    def test_own_vg_from_pvs_is_reused(self):
        verdict, detail = self.check(self.disks(ID_FS_TYPE="LVM2_member", _pv_vgs=["datastore"]))
        self.assertEqual(verdict, "PASS", detail)
        self.assertIn("reuses it", detail)
        self.assertIn("thin pool datastore/data", detail)

    def test_own_vg_from_active_lv_names_is_reused(self):
        nodes = [{"NAME": "sdb", "TYPE": "disk", "FSTYPE": "LVM2_member", "LABEL": ""},
                 {"NAME": "datastore-data_tmeta", "TYPE": "lvm", "FSTYPE": "", "LABEL": ""}]
        disks = self.disks(ID_FS_TYPE="LVM2_member", _parts=2, _nodes=nodes)
        self.assertEqual(self.check(disks)[0], "PASS")

    def test_dm_vg_name_unescapes_hyphens(self):
        self.assertEqual(vh._dm_vg_name("my--vg-thin--pool"), "my-vg")
        self.assertEqual(vh._dm_vg_name("datastore-data"), "datastore")
        self.assertEqual(vh._dm_vg_name("nohyphen"), "")


class DiskProbeParsing(unittest.TestCase):
    OUT = "\n".join([
        "DEV /dev/sda", "ID_MODEL=MTFDDAK480", "ID_PATH=pci-0000:05:00.0-ata-1.0",
        "ID_PART_TABLE_TYPE=gpt", f"SIZE {447 * GIB}", "PARTS 3", "RM 0",
        'NODE NAME="sda" TYPE="disk" FSTYPE="" LABEL=""',
        'NODE NAME="sda3" TYPE="part" FSTYPE="zfs_member" LABEL="rpool"',
        "PVVG ",
        "DEV /dev/sdb", "ID_MODEL=MTFDDAK1T9", "ID_FS_TYPE=LVM2_member",
        f"SIZE {1788 * GIB}", "PARTS 0", "RM 0",
        'NODE NAME="sdb" TYPE="disk" FSTYPE="LVM2_member" LABEL=""',
        "PVVG  vmdata ",
    ])

    def test_parse(self):
        j = job()
        j._run = lambda client, cmd, timeout=30: (0, self.OUT, "")
        verdict, _ = j._check_disks(None, {"install": {"filesystem": "ext4",
                                                       "disk_filter": {"ID_MODEL": "*480*"}}})
        self.assertEqual(verdict, "PASS")
        sda, sdb = j._disks
        self.assertEqual(sda["ID_PART_TABLE_TYPE"], "gpt")
        self.assertEqual(vh.zfs_pools_on(sda), {"rpool"})
        self.assertEqual(sdb["ID_FS_TYPE"], "LVM2_member")
        self.assertEqual(sdb["_rm"], "0")
        self.assertEqual(vh.volume_groups_on(sdb), {"vmdata"})
        self.assertEqual(vh.disk_in_use(sdb), ["a LVM2_member signature"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
