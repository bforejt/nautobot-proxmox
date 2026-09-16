"""
Out-of-band RAID layout for the bare-metal install engine (decision #50).

The SE455 V3 replaces the admin's UEFI trip: its DeviceType profile declares
the virtual drives the RAID adapter must present, and the install job makes
the XCC create them over Redfish BEFORE the installer boots:

    storage:
      controller: "RAID_*"          # glob on the Storage member Id (optional)
      volumes:                      # creation order = VD target order
        - {name: boot,      raid: RAID1, select: smallest, count: 2}
        - {name: datastore, raid: RAID1, select: largest,  count: 2}

`select` picks drives by capacity from the adapter's UNCONFIGURED drives
("boot is always the smaller pair"): the `count` drives at that end must be
equal-sized and unambiguous (the next drive strictly differs), otherwise the
layout refuses. Volumes that already exist are kept, never re-created and
never deleted — a reinstall keeps the data volume. A volume is matched by
name first, then ADOPTED by role: a hand-made VD (units built the old way in
UEFI, e.g. Lenovo's default names VD_0/VD_1) is taken for a spec entry when
its RAID type matches and its drives are exactly the drives the entry's
capacity rule would pick. Drives in JBOD state are not touched:
Lenovo/Broadcom adapters can only build a VD from "Unconfigured good"
drives, so the job asks for the conversion instead of guessing.

The XCC only shows RAID inventory while the host is powered on; the layout
step therefore powers the host on with a one-time boot into UEFI Setup (so
nothing installs meanwhile) and waits for the adapter to enumerate.

Nautobot-free and importable by file path (tests/test_storage_layout.py); the
Redfish client is injected — anything with the RedfishDiscovery storage
surface (storage_controllers / controller_drives / controller_volumes /
create_volume / get_power_state / power_action / set_boot_once) works.
"""

import fnmatch
import re
import time

VALID_RAID = ("RAID0", "RAID1", "RAID10")
VALID_SELECT = ("smallest", "largest")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}$")  # Lenovo: Name <= 15 chars

# Lenovo Oem.Lenovo.DriveStatus values that mean "free for a new VD".
_FREE_STATES = ("unconfigured good", "unconfigured", "ready")


class StorageLayoutError(RuntimeError):
    """The profile's storage section is invalid, or the adapter cannot satisfy it."""


# ---- profile parsing ----------------------------------------------------------

def parse_storage_spec(profile):
    """profile['storage'] -> validated spec dict, or None when absent."""
    raw = (profile or {}).get("storage")
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise StorageLayoutError("profile storage: must be a mapping")
    volumes = raw.get("volumes")
    if not isinstance(volumes, list) or not volumes:
        raise StorageLayoutError("profile storage.volumes: a non-empty list is required")
    seen = set()
    parsed = []
    for i, vol in enumerate(volumes):
        if not isinstance(vol, dict):
            raise StorageLayoutError(f"profile storage.volumes[{i}]: must be a mapping")
        name = str(vol.get("name") or "")
        raid = str(vol.get("raid") or "").upper()
        select = str(vol.get("select") or "").lower()
        try:
            count = int(vol.get("count", 2))
        except (TypeError, ValueError):
            raise StorageLayoutError(f"profile storage.volumes[{i}].count: integer required")
        if not _NAME_RE.match(name):
            raise StorageLayoutError(
                f"profile storage.volumes[{i}].name {name!r}: 1-15 chars of A-Z a-z 0-9 . _ -"
            )
        if name in seen:
            raise StorageLayoutError(f"profile storage.volumes: duplicate name {name!r}")
        seen.add(name)
        if raid not in VALID_RAID:
            raise StorageLayoutError(
                f"profile storage.volumes[{i}].raid {raid!r}: one of {', '.join(VALID_RAID)}"
            )
        if select not in VALID_SELECT:
            raise StorageLayoutError(
                f"profile storage.volumes[{i}].select {select!r}: one of {', '.join(VALID_SELECT)}"
            )
        minimum = {"RAID0": 1, "RAID1": 2, "RAID10": 4}[raid]
        if count < minimum or (raid == "RAID10" and count % 2) or (raid == "RAID1" and count != 2):
            raise StorageLayoutError(
                f"profile storage.volumes[{i}] {name}: {raid} needs "
                f"{'exactly 2' if raid == 'RAID1' else f'>= {minimum}' + (' (even)' if raid == 'RAID10' else '')} drives, got {count}"
            )
        parsed.append({"name": name, "raid": raid, "select": select, "count": count})
    controller = raw.get("controller")
    if controller is not None and not isinstance(controller, str):
        raise StorageLayoutError("profile storage.controller: string glob required")
    try:
        power_wait = int(raw.get("power_wait_seconds", 300))
        create_wait = int(raw.get("create_wait_seconds", 180))
    except (TypeError, ValueError):
        raise StorageLayoutError("profile storage.*_wait_seconds: integers required")
    return {
        "controller": controller,
        "volumes": parsed,
        "power_wait_seconds": power_wait,
        "create_wait_seconds": create_wait,
    }


# ---- pure planning --------------------------------------------------------------

def _is_free(drive):
    if drive.get("volumes"):
        return False
    status = str(drive.get("status") or "").strip().lower()
    return any(status.startswith(s) for s in _FREE_STATES)


def _capacity(drive):
    return int(drive.get("capacity_bytes") or 0)


def _pick(free, select, n):
    """(pick, rest) of the n smallest/largest drives from a capacity-sorted list."""
    return (free[:n], free[n:]) if select == "smallest" else (free[-n:], free[:-n])


def _adopt(vol, volumes, claimed, spec_names, free, drive_by_path):
    """Find an unclaimed existing volume that IS this spec entry: same RAID
    type, same drive count, and its drives are exactly what the entry's
    capacity rule would pick from (free drives + its own drives)."""
    for cand in volumes:
        if cand.get("path") in claimed or cand.get("name") in spec_names:
            continue
        have = str(cand.get("raid_type") or "").upper().replace(" ", "")
        if have != vol["raid"]:
            continue
        cand_drives = [drive_by_path.get(p) for p in (cand.get("drives") or [])]
        if not cand_drives or any(d is None for d in cand_drives) or len(cand_drives) != vol["count"]:
            continue
        pool = sorted(free + cand_drives, key=lambda d: (_capacity(d), str(d.get("id"))))
        pick, _ = _pick(pool, vol["select"], vol["count"])
        if {d["path"] for d in pick} == {d["path"] for d in cand_drives}:
            return cand
    return None


def plan_volumes(spec, drives, volumes):
    """Decide, per spec volume, keep (exists by name / adopted by role) or
    create (drive pick).

    drives:  [{path, id, capacity_bytes, status, volumes: [volume paths]}]
    volumes: [{path, id, name, raid_type, drives: [drive paths]}]
    Returns [{action, name, raid, drives, capacity_bytes, existing_id,
    existing_path, adopted_from}], in spec order. Raises StorageLayoutError
    on any ambiguity — nothing is ever guessed on a RAID adapter.
    """
    by_name = {v.get("name"): v for v in volumes}
    drive_by_path = {d["path"]: d for d in drives}
    spec_names = {v["name"] for v in spec["volumes"]}
    claimed = set()
    free = sorted((d for d in drives if _is_free(d)), key=lambda d: (_capacity(d), str(d.get("id"))))
    jbod = [d["id"] for d in drives if not d.get("volumes")
            and "jbod" in str(d.get("status") or "").lower()]
    plan = []
    for vol in spec["volumes"]:
        existing = by_name.get(vol["name"])
        adopted_from = None
        if existing is None:
            existing = _adopt(vol, volumes, claimed, spec_names, free, drive_by_path)
            if existing is not None:
                adopted_from = existing.get("name")
        if existing is not None:
            have = str(existing.get("raid_type") or "").upper().replace(" ", "")
            if have and have != vol["raid"]:
                raise StorageLayoutError(
                    f"volume {vol['name']!r} exists as {have}, profile wants {vol['raid']} — "
                    "refusing (delete it by hand if that is intended)"
                )
            claimed.add(existing.get("path"))
            plan.append({
                "action": "keep", "name": vol["name"], "raid": vol["raid"],
                "drives": list(existing.get("drives") or []),
                "capacity_bytes": existing.get("capacity_bytes"),
                "existing_id": existing.get("id"), "existing_path": existing.get("path"),
                "adopted_from": adopted_from,
            })
            continue
        n = vol["count"]
        if len(free) < n:
            hint = f" ({len(jbod)} drive(s) are JBOD: {jbod} — convert them to Unconfigured Good first)" if jbod else ""
            raise StorageLayoutError(
                f"volume {vol['name']!r} needs {n} unconfigured drive(s), only {len(free)} free{hint}"
            )
        pick, rest = _pick(free, vol["select"], n)
        sizes = {_capacity(d) for d in pick}
        if len(sizes) != 1:
            raise StorageLayoutError(
                f"volume {vol['name']!r}: the {n} {vol['select']} free drives differ in size "
                f"({[d['id'] for d in pick]}) — a mirror needs equal drives"
            )
        size = sizes.pop()
        if any(_capacity(d) == size for d in rest):
            raise StorageLayoutError(
                f"volume {vol['name']!r}: more than {n} free drives share the "
                f"{vol['select']} size {size} — ambiguous pick, refusing"
            )
        plan.append({
            "action": "create", "name": vol["name"], "raid": vol["raid"],
            "drives": [d["path"] for d in pick], "capacity_bytes": size,
            "existing_id": None, "existing_path": None, "adopted_from": None,
        })
        picked = {d["path"] for d in pick}
        free = [d for d in free if d["path"] not in picked]
    return plan


def _boot_position_warning(spec, boot_id, volumes_after):
    """The install profile pins the boot VD by SCSI target (ID_PATH
    *-scsi-0:2:0:0 = the adapter's first VD; confirmed on a hand-built
    SE455 V3: VD target 0 -> sda, target 1 -> sdb). Warn when the resolved
    boot volume is not the first one the adapter lists."""
    if not spec["volumes"] or not volumes_after or boot_id is None:
        return None
    order = [(v.get("id"), v.get("name")) for v in volumes_after]
    if str(order[0][0]) != str(boot_id):
        return (
            f"boot volume (adapter id {boot_id}) is not the adapter's first VD "
            f"(order: {order}) — the profile's ID_PATH pin assumes target 0; "
            "verify with the host-verification job before installing"
        )
    return None


# ---- apply ------------------------------------------------------------------------

def _select_controller(redfish, spec):
    controllers = redfish.storage_controllers()
    if not controllers:
        raise StorageLayoutError("no Storage members on the ComputerSystem (host powered off?)")
    glob = spec.get("controller")
    if glob:
        matches = [c for c in controllers
                   if fnmatch.fnmatch(str(c.get("id") or ""), glob)
                   or fnmatch.fnmatch(str(c.get("name") or ""), glob)]
    else:
        matches = [c for c in controllers if c.get("drive_count")]
    if not matches:
        raise StorageLayoutError(
            f"no storage controller matches {glob or 'any with drives'} "
            f"(seen: {[c.get('id') for c in controllers]})"
        )
    if len(matches) > 1:
        raise StorageLayoutError(
            f"several controllers match {glob or 'any with drives'}: "
            f"{[c.get('id') for c in matches]} — set storage.controller in the profile"
        )
    return matches[0]


def _wait(predicate, timeout, poll, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        sleep(poll)


def apply_storage_layout(redfish, spec, logger, dry_run=False, sleep=time.sleep):
    """Ensure the adapter presents the profile's volumes. Returns a summary
    dict: {controller, plan, created, kept, warnings}. dry_run plans only."""
    warnings = []
    redfish.discover_paths()
    power = redfish.get_power_state()
    if power != "On":
        if dry_run:
            raise StorageLayoutError(
                f"host is {power}: the XCC only reports RAID inventory while powered on — "
                "power it on (it parks in UEFI Setup) or run without dry-run"
            )
        logger.info("Host is %s — powering on into UEFI Setup so the RAID adapter enumerates", power)
        redfish.set_boot_once("BiosSetup")
        redfish.power_action("On")

    def controller_ready():
        try:
            ctrl = _select_controller(redfish, spec)
        except StorageLayoutError:
            return None
        return ctrl if ctrl.get("drive_count") else None

    controller = _wait(controller_ready, spec["power_wait_seconds"], 10, sleep) if power != "On" \
        else _select_controller(redfish, spec)
    if not controller:
        raise StorageLayoutError(
            f"RAID adapter did not enumerate drives within {spec['power_wait_seconds']}s of power-on"
        )
    logger.info("Storage controller %s (%s): %s drive(s)",
                controller.get("id"), controller.get("name"), controller.get("drive_count"))
    drives = redfish.controller_drives(controller["path"])
    volumes = redfish.controller_volumes(controller["path"])
    for d in drives:
        logger.info("  drive %s: %s %s bytes status=%r in-volume=%s",
                    d.get("id"), d.get("model"), d.get("capacity_bytes"),
                    d.get("status"), bool(d.get("volumes")))
    for v in volumes:
        logger.info("  volume %s %r: %s %s bytes over %s",
                    v.get("id"), v.get("name"), v.get("raid_type"), v.get("capacity_bytes"),
                    [p.rsplit("/", 1)[-1] for p in (v.get("drives") or [])])
    plan = plan_volumes(spec, drives, volumes)
    for step in plan:
        logger.info("  plan: %s %s %s over %s%s", step["action"].upper(), step["name"], step["raid"],
                    [p.rsplit("/", 1)[-1] for p in step["drives"]],
                    f" (adopting existing VD {step['adopted_from']!r}, adapter id {step['existing_id']})"
                    if step.get("adopted_from") else "")
    created, kept = [], [s["name"] for s in plan if s["action"] == "keep"]
    boot_name = spec["volumes"][0]["name"]
    resolved = {s["name"]: s["existing_id"] for s in plan if s["action"] == "keep"}
    if dry_run:
        if plan[0]["action"] == "create" and volumes:
            warnings.append(
                f"boot volume {boot_name!r} would be created AFTER existing volume(s) "
                f"{[(v.get('id'), v.get('name')) for v in volumes]} and so would not be the "
                "adapter's first VD — the profile's ID_PATH pin assumes target 0"
            )
        warning = _boot_position_warning(spec, resolved.get(boot_name), volumes)
        if warning:
            warnings.append(warning)
        return {"controller": controller.get("id"), "plan": plan, "created": created,
                "kept": kept, "warnings": warnings, "dry_run": True}
    for step in plan:
        if step["action"] != "create":
            continue
        logger.info("Creating %s %s over %s", step["raid"], step["name"],
                    [p.rsplit("/", 1)[-1] for p in step["drives"]])
        redfish.create_volume(controller["path"], step["name"], step["raid"], step["drives"])
        appeared = _wait(
            lambda: next((v for v in redfish.controller_volumes(controller["path"])
                          if v.get("name") == step["name"]), None),
            spec["create_wait_seconds"], 5, sleep,
        )
        if not appeared:
            raise StorageLayoutError(
                f"volume {step['name']!r} did not appear within {spec['create_wait_seconds']}s "
                "after the create request — check the XCC storage page"
            )
        created.append(step["name"])
        resolved[step["name"]] = appeared.get("id")
        logger.info("Volume %s created (adapter id %s, %s bytes)", step["name"],
                    appeared.get("id"), appeared.get("capacity_bytes"))
    volumes_after = redfish.controller_volumes(controller["path"])
    warning = _boot_position_warning(spec, resolved.get(boot_name), volumes_after)
    if warning:
        warnings.append(warning)
        logger.warning("%s", warning)
    return {"controller": controller.get("id"), "plan": plan, "created": created,
            "kept": kept, "warnings": warnings, "dry_run": False, "resolved": resolved,
            "volumes": [(v.get("id"), v.get("name"), v.get("raid_type")) for v in volumes_after]}
