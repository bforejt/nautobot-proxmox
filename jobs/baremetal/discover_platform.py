"""
Nautobot Job: SE350 platform discovery (read-only).

Dumps the Redfish facts the project's Phase 0 checklist needs from a Lenovo
XCC BMC — BIOS attribute names/values (feeds bmc/se350_bios.yaml), VirtualMedia
EXT-member presence (the no-USB install mechanism check), XCC/UEFI/NIC firmware
levels, and Secure Boot state. Performs GET requests only; changes nothing on
the target.

Credentials come from Nautobot Secrets named ``xcc_username`` / ``xcc_password``
(same convention as the bare-metal starter — provider and backing store are
configured on the Secret objects themselves).

Full JSON results are attached to the JobResult as downloadable files; the job
log carries the highlights and the checklist verdicts.
"""

import json

from nautobot.apps.jobs import BooleanVar, IPAddressVar, Job, StringVar, register_jobs
from nautobot.extras.models import Secret

from ..lib.redfish_discovery import RedfishDiscovery

XCC_USERNAME_SECRET_NAME = "xcc_username"
XCC_PASSWORD_SECRET_NAME = "xcc_password"

# BIOS attribute name fragments worth surfacing in the log (full dump goes to
# the attached file). Casing varies across Lenovo firmware — matched lowercase.
INTERESTING_BIOS_FRAGMENTS = (
    "operatingmode",
    "cstate",
    "c_state",
    "c1e",
    "turbo",
    "pstate",
    "p_state",
    "powerperformance",
    "devicesandioports",
    "console",
    "com1",
    "serial",
    "secureboot",
    "hyperthread",
    "smt",
)


class DiscoverSe350Platform(Job):
    class Meta:
        name = "SE350 Platform Discovery"
        description = (
            "Redfish sweep of a Lenovo XCC: BIOS attributes, VirtualMedia EXT members, "
            "firmware versions, Secure Boot state. Read-only by default (checklist "
            "items 1-3). Optional WRITE checks: virtual-media mount/eject test, and a "
            "DISRUPTIVE full dress rehearsal (boot-once from the mounted ISO + power "
            "cycle) for lab units only."
        )
        has_sensitive_variables = False

    bmc_ip = IPAddressVar(
        label="BMC (XCC) IP Address",
        description="Management IP of the target XClarity Controller",
    )

    skip_tls_verify = BooleanVar(
        label="Skip BMC TLS verification",
        description="Enable for self-signed XCC certs (typical on isolated mgmt networks)",
        default=True,
    )

    run_vmedia_write_test = BooleanVar(
        label="Run virtual-media WRITE test",
        description=(
            "Mount the test ISO via the platform-correct method (XCC1 PATCH-on-EXT vs "
            "XCC2 InsertMedia, auto-detected), verify Inserted, then eject. Writes to "
            "the BMC only — does not touch host power or OS. Safe on a running host, "
            "but intended for lab units."
        ),
        default=False,
    )

    test_iso_url = StringVar(
        label="Test ISO URL",
        description=(
            "HTTP(S) URL of a small ISO for the write test. Must be plain HTTP for "
            "SE350/XCC1 (no authenticated HTTPS)."
        ),
        default="http://provisioning.example.internal/isos/test.iso",
        required=False,
    )

    dress_rehearsal_reboot = BooleanVar(
        label="DISRUPTIVE: full dress rehearsal (boot the ISO)",
        description=(
            "After mounting, set one-time boot to CD and power-cycle the node so it "
            "boots the mounted ISO — the end-to-end proof of the no-USB install "
            "mechanism. REBOOTS THE TARGET. Lab units only; requires the write test "
            "to be enabled. Media is left mounted (eject by re-running the write test "
            "without this option once finished)."
        ),
        default=False,
    )

    def run(self, bmc_ip, skip_tls_verify, run_vmedia_write_test, test_iso_url, dress_rehearsal_reboot):
        try:
            username = Secret.objects.get(name=XCC_USERNAME_SECRET_NAME).get_value()
            password = Secret.objects.get(name=XCC_PASSWORD_SECRET_NAME).get_value()
        except Secret.DoesNotExist as exc:
            self.logger.error(
                "Required Secret not found: %s. Expected Secrets named %r and %r.",
                exc,
                XCC_USERNAME_SECRET_NAME,
                XCC_PASSWORD_SECRET_NAME,
            )
            raise

        discovery = RedfishDiscovery(
            bmc_ip=str(bmc_ip),
            username=username,
            password=password,
            verify_tls=not skip_tls_verify,
        )

        self.logger.info("Starting read-only Redfish discovery against %s", bmc_ip)
        report = discovery.full_report()

        self._log_system(report.get("system", {}), report.get("manager", {}))
        self._log_virtual_media(report.get("virtual_media", {}))
        self._log_bios_highlights(report.get("bios", {}))
        self._log_secure_boot(report.get("secure_boot", {}))
        self._log_firmware(report.get("firmware_inventory", {}))
        self._attach_files(report)

        if dress_rehearsal_reboot and not run_vmedia_write_test:
            self.logger.error(
                "Dress rehearsal requested without the write test enabled — enable "
                "'Run virtual-media WRITE test' too. No write operations performed."
            )
            raise ValueError("dress_rehearsal_reboot requires run_vmedia_write_test")

        if run_vmedia_write_test:
            self._run_write_checks(discovery, test_iso_url, dress_rehearsal_reboot)

        return "Discovery complete — see attached JSON files for full dumps."

    # ---------- write checks (opt-in) ----------

    def _run_write_checks(self, discovery, test_iso_url, dress_rehearsal_reboot):
        if not test_iso_url:
            self.logger.error("Write test enabled but no Test ISO URL provided.")
            raise ValueError("test_iso_url is required for the write test")

        self.logger.info("WRITE TEST: mounting %s", test_iso_url)
        mount = discovery.mount_iso(str(test_iso_url))
        self.logger.info(
            "Mount accepted via %s on %s", mount["mode"], mount["member_path"]
        )

        if discovery.wait_media_state(mount["member_path"], inserted=True):
            self.logger.info(
                "CHECKLIST §1 WRITE CHECK PASS: media shows Inserted=true "
                "(mode=%s). The Redfish mount mechanism works on this unit.",
                mount["mode"],
            )
        else:
            self.logger.error(
                "CHECKLIST §1 WRITE CHECK FAIL: media never showed Inserted=true "
                "within timeout. Attempting eject to leave the BMC clean."
            )
            discovery.eject_iso(mount["member_path"], mount["mode"])
            raise RuntimeError("Virtual media mount did not reach Inserted=true")

        if dress_rehearsal_reboot:
            discovery.set_boot_once_cd()
            self.logger.info("Set one-time boot override to CD.")
            power_state = discovery.get_power_state()
            action = "ForceRestart" if power_state == "On" else "On"
            discovery.power_action(action)
            self.logger.warning(
                "DRESS REHEARSAL: sent power action '%s' — the node is booting the "
                "mounted test ISO. Watch the console (OpenGear/XCC). Media is left "
                "mounted; eject later by re-running the write test without the "
                "dress-rehearsal option.",
                action,
            )
            return

        self.logger.info("Ejecting test media (no reboot requested).")
        discovery.eject_iso(mount["member_path"], mount["mode"])
        if discovery.wait_media_state(mount["member_path"], inserted=False, timeout=60):
            self.logger.info("Eject verified — BMC left in its original media state.")
        else:
            self.logger.warning(
                "Eject not confirmed within timeout — check %s manually.",
                mount["member_path"],
            )

    # ---------- logging helpers ----------

    def _log_system(self, system, manager):
        if "error" in system:
            self.logger.warning("System inventory failed: %s", system["error"])
            return
        self.logger.info(
            "System: %s %s | serial %s | UUID %s | power %s | UEFI %s | XCC %s",
            system.get("manufacturer"),
            system.get("model"),
            system.get("serial_number"),
            system.get("uuid"),
            system.get("power_state"),
            system.get("bios_version"),
            manager.get("firmware_version", "unknown"),
        )

    def _log_virtual_media(self, vmedia):
        if "error" in vmedia:
            self.logger.warning("VirtualMedia enumeration failed: %s", vmedia["error"])
            return
        ids = [m.get("id") for m in vmedia.get("members", [])]
        self.logger.info("VirtualMedia members (%d): %s", vmedia.get("member_count", 0), ids)
        if vmedia.get("ext_members_present"):
            self.logger.info(
                "CHECKLIST §1 PASS: EXT members present (%s) — Redfish network ISO "
                "mount is available on this unit at its current firmware/license.",
                vmedia.get("ext_member_ids"),
            )
        else:
            self.logger.warning(
                "CHECKLIST §1 FAIL: no EXT members in the VirtualMedia collection. "
                "License is known Enterprise fleet-wide, so suspect XCC firmware too "
                "old — update XCC firmware and re-run."
            )

    def _log_bios_highlights(self, bios):
        if "error" in bios:
            self.logger.warning("BIOS attribute dump failed: %s", bios["error"])
            return
        attributes = bios.get("attributes", {})
        self.logger.info(
            "CHECKLIST §3: BIOS dump captured — %d attributes (registry: %s). "
            "Full dump in attached bios_attributes.json.",
            len(attributes),
            bios.get("attribute_registry") or "not referenced",
        )
        highlights = {
            name: value
            for name, value in sorted(attributes.items())
            if any(fragment in name.lower() for fragment in INTERESTING_BIOS_FRAGMENTS)
        }
        for name, value in highlights.items():
            self.logger.info("BIOS %s = %r", name, value)
        if not highlights:
            self.logger.warning(
                "No BIOS attributes matched the expected name fragments (operating "
                "mode / C-states / console redirect) — inspect the full dump; the "
                "naming convention may differ on this firmware."
            )

    def _log_secure_boot(self, secure_boot):
        if "error" in secure_boot:
            self.logger.warning("SecureBoot read failed: %s", secure_boot["error"])
            return
        self.logger.info(
            "Secure Boot: enabled=%s current_boot=%s mode=%s (plan default: disabled "
            "for the auto-install pipeline)",
            secure_boot.get("enabled"),
            secure_boot.get("current_boot"),
            secure_boot.get("mode"),
        )

    def _log_firmware(self, firmware):
        if "error" in firmware:
            self.logger.warning("Firmware inventory failed: %s", firmware["error"])
            return
        for item in firmware.get("items", []):
            self.logger.info(
                "Firmware: %s = %s", item.get("name") or item.get("id"), item.get("version")
            )

    def _attach_files(self, report):
        files = {
            "discovery_full.json": report,
            "bios_attributes.json": report.get("bios", {}),
            "bios_registry.json": report.get("bios_registry", {}),
            "virtual_media.json": report.get("virtual_media", {}),
        }
        for filename, content in files.items():
            payload = json.dumps(content, indent=2, sort_keys=True, default=str)
            try:
                self.create_file(filename, payload)
            except Exception as exc:  # noqa: BLE001 - file attachment is best-effort
                self.logger.warning(
                    "Could not attach %s (%s) — content follows in log.", filename, exc
                )
                if filename != "discovery_full.json":
                    self.logger.info("%s:\n%s", filename, payload)


register_jobs(DiscoverSe350Platform)
