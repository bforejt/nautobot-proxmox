#!/usr/bin/env python3
"""
Unit tests for jobs/lib/storage_layout.py — the out-of-band RAID layout
behind the SE455 V3 install (decision #50). Stdlib-only; the module is loaded
from its file path and the Redfish client is a recording fake.

Run:  python3 tests/test_storage_layout.py
"""

import importlib.util
import logging
import pathlib
import sys
import unittest

MODULE = pathlib.Path(__file__).resolve().parent.parent / "jobs" / "lib" / "storage_layout.py"
spec = importlib.util.spec_from_file_location("storage_layout", MODULE)
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)

PROFILE = {
    "storage": {
        "controller": "RAID_*",
        "volumes": [
            {"name": "boot", "raid": "RAID1", "select": "smallest", "count": 2},
            {"name": "datastore", "raid": "RAID1", "select": "largest", "count": 2},
        ],
    }
}
CTRL = "/redfish/v1/Systems/1/Storage/RAID_Slot4"
GB = 1_000_000_000


def drive(n, size_gb, status="Unconfigured good", volumes=()):
    return {"path": f"{CTRL}/Drives/Disk.{n}", "id": f"Disk.{n}", "model": "MTFDDAK",
            "capacity_bytes": size_gb * GB, "status": status, "volumes": list(volumes)}


def four_drives():
    return [drive(0, 480), drive(1, 1920), drive(2, 480), drive(3, 1920)]


class ParseSpec(unittest.TestCase):
    def test_valid(self):
        s = sl.parse_storage_spec(PROFILE)
        self.assertEqual([v["name"] for v in s["volumes"]], ["boot", "datastore"])
        self.assertEqual(s["controller"], "RAID_*")
        self.assertEqual(s["power_wait_seconds"], 300)

    def test_absent(self):
        self.assertIsNone(sl.parse_storage_spec({"install": {}}))

    def test_rejects(self):
        bad = [
            {"storage": {"volumes": []}},
            {"storage": {"volumes": [{"name": "a-name-that-is-way-too-long", "raid": "RAID1", "select": "smallest"}]}},
            {"storage": {"volumes": [{"name": "x", "raid": "RAID5", "select": "smallest"}]}},
            {"storage": {"volumes": [{"name": "x", "raid": "RAID1", "select": "middle"}]}},
            {"storage": {"volumes": [{"name": "x", "raid": "RAID1", "select": "smallest", "count": 3}]}},
            {"storage": {"volumes": [{"name": "x", "raid": "RAID1", "select": "smallest"},
                                     {"name": "x", "raid": "RAID1", "select": "largest"}]}},
        ]
        for profile in bad:
            with self.assertRaises(sl.StorageLayoutError, msg=profile):
                sl.parse_storage_spec(profile)


class Plan(unittest.TestCase):
    def setUp(self):
        self.spec = sl.parse_storage_spec(PROFILE)

    def test_fresh_adapter_boot_is_smaller_pair(self):
        plan = sl.plan_volumes(self.spec, four_drives(), [])
        self.assertEqual([p["action"] for p in plan], ["create", "create"])
        self.assertEqual(plan[0]["drives"], [f"{CTRL}/Drives/Disk.0", f"{CTRL}/Drives/Disk.2"])
        self.assertEqual(plan[1]["drives"], [f"{CTRL}/Drives/Disk.1", f"{CTRL}/Drives/Disk.3"])
        self.assertEqual(plan[0]["capacity_bytes"], 480 * GB)

    def test_existing_volumes_kept(self):
        drives = [drive(0, 480, "Online", [f"{CTRL}/Volumes/1"]), drive(2, 480, "Online", [f"{CTRL}/Volumes/1"]),
                  drive(1, 1920), drive(3, 1920)]
        volumes = [{"path": f"{CTRL}/Volumes/1", "id": "1", "name": "boot", "raid_type": "RAID1",
                    "drives": [f"{CTRL}/Drives/Disk.0", f"{CTRL}/Drives/Disk.2"], "capacity_bytes": 480 * GB}]
        plan = sl.plan_volumes(self.spec, drives, volumes)
        self.assertEqual(plan[0]["action"], "keep")
        self.assertEqual(plan[1]["action"], "create")
        self.assertEqual(plan[1]["drives"], [f"{CTRL}/Drives/Disk.1", f"{CTRL}/Drives/Disk.3"])

    def test_raid_type_mismatch_refuses(self):
        volumes = [{"path": f"{CTRL}/Volumes/1", "id": "1", "name": "boot", "raid_type": "RAID0", "drives": []}]
        with self.assertRaises(sl.StorageLayoutError):
            sl.plan_volumes(self.spec, four_drives(), volumes)

    def test_unequal_pair_refuses(self):
        drives = [drive(0, 480), drive(1, 960), drive(2, 1920), drive(3, 1920)]
        with self.assertRaises(sl.StorageLayoutError):
            sl.plan_volumes(self.spec, drives, [])

    def test_ambiguous_size_refuses(self):
        drives = four_drives() + [drive(4, 480)]
        with self.assertRaises(sl.StorageLayoutError):
            sl.plan_volumes(self.spec, drives, [])

    def test_jbod_drives_not_used(self):
        drives = [drive(0, 480, "JBOD"), drive(2, 480, "JBOD"), drive(1, 1920), drive(3, 1920)]
        with self.assertRaises(sl.StorageLayoutError) as ctx:
            sl.plan_volumes(self.spec, drives, [])
        self.assertIn("JBOD", str(ctx.exception))

    def test_too_few_free(self):
        with self.assertRaises(sl.StorageLayoutError):
            sl.plan_volumes(self.spec, [drive(0, 480), drive(1, 1920)], [])

    def test_hand_made_volumes_adopted_by_role(self):
        # The old way: admin-made VD_0 (480G pair) + VD_1 (1.92T pair) with Lenovo default names.
        drives = [drive(0, 480, "Online", [f"{CTRL}/Volumes/1"]), drive(2, 480, "Online", [f"{CTRL}/Volumes/1"]),
                  drive(1, 1920, "Online", [f"{CTRL}/Volumes/2"]), drive(3, 1920, "Online", [f"{CTRL}/Volumes/2"])]
        volumes = [
            {"path": f"{CTRL}/Volumes/1", "id": "1", "name": "VD_0", "raid_type": "RAID1",
             "drives": [f"{CTRL}/Drives/Disk.0", f"{CTRL}/Drives/Disk.2"], "capacity_bytes": 480 * GB},
            {"path": f"{CTRL}/Volumes/2", "id": "2", "name": "VD_1", "raid_type": "RAID1",
             "drives": [f"{CTRL}/Drives/Disk.1", f"{CTRL}/Drives/Disk.3"], "capacity_bytes": 1920 * GB},
        ]
        plan = sl.plan_volumes(self.spec, drives, volumes)
        self.assertEqual([(p["action"], p["adopted_from"], p["existing_id"]) for p in plan],
                         [("keep", "VD_0", "1"), ("keep", "VD_1", "2")])

    def test_adoption_refuses_wrong_shape(self):
        # One RAID10 over all four drives: nothing matches the boot/datastore roles.
        paths = [f"{CTRL}/Drives/Disk.{i}" for i in range(4)]
        drives = [drive(i, 480 if i in (0, 2) else 1920, "Online", [f"{CTRL}/Volumes/1"]) for i in range(4)]
        volumes = [{"path": f"{CTRL}/Volumes/1", "id": "1", "name": "VD_0", "raid_type": "RAID10",
                    "drives": paths, "capacity_bytes": 1}]
        with self.assertRaises(sl.StorageLayoutError):
            sl.plan_volumes(self.spec, drives, volumes)


class FakeRedfish:
    """Records write calls; volumes appear after create_volume."""

    def __init__(self, drives, volumes=None, power="On"):
        self.drives = drives
        self.volumes = list(volumes or [])
        self.power = power
        self.calls = []

    def discover_paths(self):
        self.calls.append(("discover_paths",))

    def get_power_state(self):
        return self.power

    def power_action(self, action):
        self.calls.append(("power_action", action))
        self.power = "On"

    def set_boot_once(self, target):
        self.calls.append(("set_boot_once", target))

    def storage_controllers(self):
        if self.power != "On":
            return []
        return [{"path": CTRL, "id": "RAID_Slot4", "name": "RAID 540-8i", "drive_count": len(self.drives)},
                {"path": "/redfish/v1/Systems/1/Storage/NVMe", "id": "NVMe_Onboard", "name": "onboard", "drive_count": 0}]

    def controller_drives(self, path):
        return self.drives

    def controller_volumes(self, path):
        return list(self.volumes)

    def create_volume(self, controller_path, name, raid_type, drive_paths):
        self.calls.append(("create_volume", name, raid_type, tuple(drive_paths)))
        vid = str(len(self.volumes) + 1)
        self.volumes.append({"path": f"{controller_path}/Volumes/{vid}", "id": vid, "name": name,
                             "raid_type": raid_type, "drives": list(drive_paths), "capacity_bytes": 1})
        for d in self.drives:
            if d["path"] in drive_paths:
                d["volumes"] = [f"{controller_path}/Volumes/{vid}"]
                d["status"] = "Online"
        return {}


class Apply(unittest.TestCase):
    def setUp(self):
        self.spec = sl.parse_storage_spec(PROFILE)
        self.logger = logging.getLogger("test")

    def test_creates_in_order_when_on(self):
        rf = FakeRedfish(four_drives())
        result = sl.apply_storage_layout(rf, self.spec, self.logger, sleep=lambda s: None)
        creates = [c for c in rf.calls if c[0] == "create_volume"]
        self.assertEqual([c[1] for c in creates], ["boot", "datastore"])
        self.assertEqual(result["created"], ["boot", "datastore"])
        self.assertEqual([v[1] for v in result["volumes"]], ["boot", "datastore"])
        self.assertEqual(result["warnings"], [])
        self.assertNotIn(("power_action", "On"), rf.calls)

    def test_powers_on_into_setup_when_off(self):
        rf = FakeRedfish(four_drives(), power="Off")
        sl.apply_storage_layout(rf, self.spec, self.logger, sleep=lambda s: None)
        self.assertIn(("set_boot_once", "BiosSetup"), rf.calls)
        self.assertIn(("power_action", "On"), rf.calls)
        self.assertEqual(len([c for c in rf.calls if c[0] == "create_volume"]), 2)

    def test_dry_run_plans_only(self):
        rf = FakeRedfish(four_drives())
        result = sl.apply_storage_layout(rf, self.spec, self.logger, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual([c for c in rf.calls if c[0] == "create_volume"], [])
        self.assertEqual([p["action"] for p in result["plan"]], ["create", "create"])

    def test_dry_run_refuses_when_off(self):
        rf = FakeRedfish(four_drives(), power="Off")
        with self.assertRaises(sl.StorageLayoutError):
            sl.apply_storage_layout(rf, self.spec, self.logger, dry_run=True)

    def test_idempotent_second_run_creates_nothing(self):
        rf = FakeRedfish(four_drives())
        sl.apply_storage_layout(rf, self.spec, self.logger, sleep=lambda s: None)
        rf.calls.clear()
        result = sl.apply_storage_layout(rf, self.spec, self.logger, sleep=lambda s: None)
        self.assertEqual([c for c in rf.calls if c[0] == "create_volume"], [])
        self.assertEqual(result["kept"], ["boot", "datastore"])

    def test_warns_when_boot_is_not_first_volume(self):
        drives = [drive(0, 480), drive(2, 480), drive(1, 1920, "Online", [f"{CTRL}/Volumes/1"]),
                  drive(3, 1920, "Online", [f"{CTRL}/Volumes/1"])]
        volumes = [{"path": f"{CTRL}/Volumes/1", "id": "1", "name": "datastore", "raid_type": "RAID1",
                    "drives": [f"{CTRL}/Drives/Disk.1", f"{CTRL}/Drives/Disk.3"], "capacity_bytes": 1920 * GB}]
        rf = FakeRedfish(drives, volumes)
        result = sl.apply_storage_layout(rf, self.spec, self.logger, sleep=lambda s: None)
        self.assertEqual(result["created"], ["boot"])
        self.assertEqual(result["resolved"], {"datastore": "1", "boot": "2"})
        self.assertTrue(result["warnings"] and "first VD" in result["warnings"][0])

    def test_dry_run_warns_when_boot_would_not_be_first(self):
        drives = [drive(0, 480), drive(2, 480), drive(1, 1920, "Online", [f"{CTRL}/Volumes/1"]),
                  drive(3, 1920, "Online", [f"{CTRL}/Volumes/1"])]
        volumes = [{"path": f"{CTRL}/Volumes/1", "id": "1", "name": "datastore", "raid_type": "RAID1",
                    "drives": [f"{CTRL}/Drives/Disk.1", f"{CTRL}/Drives/Disk.3"], "capacity_bytes": 1920 * GB}]
        result = sl.apply_storage_layout(FakeRedfish(drives, volumes), self.spec, self.logger, dry_run=True)
        self.assertTrue(result["warnings"] and "first VD" in result["warnings"][0])

    def test_adopted_unit_is_a_no_op_without_warning(self):
        drives = [drive(0, 480, "Online", [f"{CTRL}/Volumes/1"]), drive(2, 480, "Online", [f"{CTRL}/Volumes/1"]),
                  drive(1, 1920, "Online", [f"{CTRL}/Volumes/2"]), drive(3, 1920, "Online", [f"{CTRL}/Volumes/2"])]
        volumes = [
            {"path": f"{CTRL}/Volumes/1", "id": "1", "name": "VD_0", "raid_type": "RAID1",
             "drives": [f"{CTRL}/Drives/Disk.0", f"{CTRL}/Drives/Disk.2"], "capacity_bytes": 480 * GB},
            {"path": f"{CTRL}/Volumes/2", "id": "2", "name": "VD_1", "raid_type": "RAID1",
             "drives": [f"{CTRL}/Drives/Disk.1", f"{CTRL}/Drives/Disk.3"], "capacity_bytes": 1920 * GB},
        ]
        rf = FakeRedfish(drives, volumes)
        result = sl.apply_storage_layout(rf, self.spec, self.logger, sleep=lambda s: None)
        self.assertEqual([c for c in rf.calls if c[0] == "create_volume"], [])
        self.assertEqual(result["kept"], ["boot", "datastore"])
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
