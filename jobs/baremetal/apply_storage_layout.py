"""
Nautobot Job: apply a DeviceType's out-of-band RAID layout (decision #50).

The SE455 V3 replaces the admin's UEFI "create the mirror sets" trip with the
profile's `storage` section, applied through the BMC (Lenovo XCC/XCC2 Redfish
volume creation). The install job runs the same step itself before it boots
the installer; this job exists to prepare or re-check a unit on its own:
dry-run by default (reads the adapter, prints the plan), and with Confirm it
creates the missing volumes. Existing volumes are kept — never deleted, never
re-created — so a reinstall keeps the data volume.

Requires the host to be powered on for the read (the XCC reports RAID
inventory only then); a non-dry run powers it on into UEFI Setup itself.
"""

from nautobot.apps.jobs import BooleanVar, Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device

from ..lib.install_delivery import DeliveryError, load_profile
from ..lib.nautobot_helpers import CredentialError, resolve_bmc
from ..lib.redfish_discovery import RedfishDiscovery
from ..lib.storage_layout import StorageLayoutError, apply_storage_layout, parse_storage_spec


class ApplyStorageLayout(Job):
    class Meta:
        name = "Apply Storage Layout (SoT-driven)"
        description = (
            "Out-of-band RAID layout from the DeviceType profile's `storage` "
            "section via the BMC (Lenovo XCC/XCC2 Redfish): plans the boot/data "
            "virtual drives (dry run by default) and, with Confirm, creates the "
            "missing ones. Existing volumes are never touched. Powers the host on "
            "into UEFI Setup when needed."
        )
        has_sensitive_variables = False
        soft_time_limit = 1200
        time_limit = 1500

    device = ObjectVar(
        model=Device,
        label="Node",
        description="Physical NFV-role Device with an `xcc` interface (contract §4)",
        query_params={"role": "NFV"},
    )
    dry_run = BooleanVar(
        label="Dry run (plan only)",
        description="Read the adapter and print the plan; nothing is created. Needs the host powered on.",
        default=True,
    )
    confirm = BooleanVar(
        label="Confirm changes",
        description="Required when dry run is off: create the missing volumes on the RAID adapter.",
        default=False,
    )

    def run(self, device, dry_run, confirm):
        try:
            profile = load_profile(device.device_type.model)
        except DeliveryError as exc:
            raise RuntimeError(str(exc))
        try:
            spec = parse_storage_spec(profile)
        except StorageLayoutError as exc:
            raise RuntimeError(f"Profile storage section invalid: {exc}")
        if not spec:
            return f"{device.device_type.model} profile declares no storage layout — nothing to do."
        if not dry_run and not confirm:
            raise RuntimeError(
                "Dry run is off but Confirm is not ticked — refusing to change the RAID adapter"
            )
        try:
            bmc_ip, username, password = resolve_bmc(device)
        except CredentialError as exc:
            raise RuntimeError(str(exc))
        redfish = RedfishDiscovery(bmc_ip=bmc_ip, username=username, password=password)
        try:
            summary = apply_storage_layout(redfish, spec, self.logger, dry_run=dry_run)
        except StorageLayoutError as exc:
            raise RuntimeError(f"Storage layout refused: {exc}")
        for warning in summary["warnings"]:
            self.logger.warning("%s", warning)
        plan = ", ".join(f"{p['action']} {p['name']} ({p['raid']})" for p in summary["plan"])
        if summary["dry_run"]:
            return f"{device.name}: plan on {summary['controller']}: {plan} — dry run, nothing changed."
        return (
            f"{device.name}: storage layout applied on {summary['controller']}: "
            f"created {summary['created'] or 'nothing'}, kept {summary['kept'] or 'nothing'}."
        )


register_jobs(ApplyStorageLayout)
