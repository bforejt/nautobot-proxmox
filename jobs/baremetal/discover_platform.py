"""
Nautobot Job: Lenovo XCC platform discovery (read-only) — SE350 (XCC1) and
SE455 V3 (XCC2).

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
    # AMD (SE455 V3 / EPYC) attribute families
    "determinism",
    "cppc",
    "globalc",
    "corepe",
    "numa",
    "powerprofile",
)


class DiscoverSe350Platform(Job):
    class Meta:
        name = "SE350 Platform Discovery"
        description = (
            "Redfish sweep of a Lenovo XCC (SE350/XCC1, SE455 V3/XCC2): BIOS "
            "attributes, VirtualMedia EXT members, licenses, drive inventory, "
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
            "URL of a small bootable ISO for the write test — e.g. "
            "http://<composer-host>/images/<file>.iso. Must be plain HTTP for "
            "SE350/XCC1 (no authenticated HTTPS). Required when the write test "
            "is enabled."
        ),
        default="",
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
        self._log_licenses(report.get("licenses", {}))
        self._log_drives(report.get("drives", {}))
        self._log_bios_highlights(report.get("bios", {}))
        self._log_secure_boot(report.get("secure_boot", {}))
        self._log_firmware(report.get("firmware_inventory", {}))
        self._log_thermal_probe(report.get("chassis", {}))
        self._attach_files(report)

        if run_vmedia_write_test and not (test_iso_url or "").strip():
            self.logger.error(
                "Write test enabled but no Test ISO URL given — host a small "
                "bootable ISO at a plain-HTTP URL (e.g. the composer firmware "
                "server: http://<host>/images/<file>.iso) and re-run."
            )
            raise ValueError("run_vmedia_write_test requires test_iso_url")
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
                "XCC1: Enterprise FoD is known fleet-wide, so suspect XCC firmware too "
                "old — update and re-run. XCC2: check the license section below "
                "(XCC2_Platinum must be Enabled)."
            )

    def _log_licenses(self, licenses):
        if "error" in licenses:
            self.logger.warning("License enumeration failed: %s", licenses["error"])
            return
        if not licenses.get("supported"):
            self.logger.info(
                "No LicenseService on this BMC (XCC1) — license state is not readable "
                "via Redfish; EXT-member presence above is the proxy."
            )
            return
        for item in licenses.get("items", []):
            self.logger.info(
                "License %s (%s): %s%s",
                item.get("id"), item.get("name"), item.get("state"),
                f", expires {item['expiration']}" if item.get("expiration") else "",
            )
        if licenses.get("platinum_enabled"):
            self.logger.info("XCC2 Platinum ENABLED — remote media (virtual media) is entitled.")
        else:
            self.logger.warning(
                "XCC2 Platinum NOT enabled — Redfish virtual media will not work on "
                "this unit; install the Platinum key (7S0X000KWW) or deliver via PXE."
            )

    def _log_drives(self, drives):
        if "error" in drives:
            self.logger.warning("Drive inventory failed: %s", drives["error"])
            return
        items = drives.get("drives", [])
        if not items:
            self.logger.info("Drive inventory: %s", drives.get("note") or "no drives reported")
            return
        for d in items:
            gb = (d.get("capacity_bytes") or 0) / 1e9
            self.logger.info(
                "Drive %s [%s]: model=%r serial=%s %.0f GB %s/%s (udev ID_MODEL=%r)",
                d.get("location") or d.get("id"), d.get("controller"), d.get("model"),
                d.get("serial"), gb, d.get("media_type"), d.get("protocol"),
                d.get("udev_id_model"),
            )
        # Boot-pair hint for JBOD profiles (SE455 V3 rule: boot = the smaller
        # pair): the smallest capacity group, its model string, and whether a
        # model-glob on it would also catch larger drives.
        sized = [d for d in items if d.get("capacity_bytes")]
        if len(sized) < 2:
            return
        smallest = sized[0]["capacity_bytes"]
        pair = [d for d in sized if d["capacity_bytes"] == smallest]
        models = {d.get("udev_id_model") for d in pair}
        if len(pair) == 2 and len(models) == 1:
            model = pair[0]["udev_id_model"] or ""
            clash = [d for d in sized if d["capacity_bytes"] != smallest and d.get("udev_id_model") == model]
            if clash:
                self.logger.warning(
                    "Boot-pair hint: the two smallest drives share ID_MODEL %r with larger "
                    "drives — a model glob cannot discriminate; pin ID_PATH (bay) instead.",
                    model,
                )
            else:
                self.logger.info(
                    "Boot-pair hint: the two smallest drives (%.0f GB) are ID_MODEL %r — "
                    "profile disk_filter ID_MODEL: \"%s\" selects exactly them; confirm "
                    "from the installer shell with: proxmox-auto-install-assistant "
                    "device-match disk ID_MODEL='%s'",
                    smallest / 1e9, model, model, model,
                )
        else:
            self.logger.info(
                "Boot-pair hint: smallest capacity %.0f GB is shared by %d drive(s) with "
                "model(s) %s — no unambiguous pair; pin the boot filter by ID_PATH.",
                smallest / 1e9, len(pair), sorted(m for m in models if m),
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

    def _log_thermal_probe(self, chassis):
        if "error" in chassis:
            self.logger.warning("Chassis/OEM thermal probe failed: %s", chassis["error"])
            return
        hits = chassis.get("thermal_hits", [])
        if hits:
            self.logger.info(
                "THERMAL PROBE: %d thermal/cooling-related Redfish paths found — "
                "inspect chassis.json to see whether the XCC 'thermal' mode is "
                "settable via Redfish OEM (would let ApplyBiosPolicyJob stay "
                "pure-Redfish).",
                len(hits),
            )
            for hit in hits[:20]:
                self.logger.info("thermal hit: %s = %r", hit["path"], hit["value"])
        else:
            self.logger.info(
                "THERMAL PROBE: no thermal/cooling mode setting found in Chassis or "
                "Manager OEM — plan on keeping the XCC-SSH 'thermal performance' "
                "step in ApplyBiosPolicyJob."
            )

    def _attach_files(self, report):
        files = {
            "discovery_full.json": report,
            "bios_attributes.json": report.get("bios", {}),
            "bios_registry.json": report.get("bios_registry", {}),
            "virtual_media.json": report.get("virtual_media", {}),
            "licenses.json": report.get("licenses", {}),
            "drives.json": report.get("drives", {}),
            "chassis.json": report.get("chassis", {}),
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
