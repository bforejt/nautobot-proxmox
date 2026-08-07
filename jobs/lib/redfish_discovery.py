"""
Read-only Redfish discovery client for Lenovo XCC BMCs (SE350 / XCC1 era and later).

Collects the platform facts the nautobot-proxmox project needs before writing
BIOS policy YAML and installer templates:

- System inventory (model, serial, SKU, power state)
- Full BIOS attribute dump (exact Lenovo attribute names + current values)
- Pending BIOS settings, and the BIOS attribute registry when the XCC
  publishes one (allowed values per attribute)
- Manager (XCC) firmware version
- VirtualMedia collection members, classified by Id prefix
  (EXT* = Redfish-insertable, RDOC*/Remote* = not usable for network ISO mount)
- Secure Boot state
- Firmware inventory (XCC/UEFI/NIC versions)

Every section is best-effort: a failure is captured as an "error" string in
that section's dict instead of aborting the whole discovery, so partial data
from older firmware still comes back.

No Nautobot imports — testable standalone (see __main__ at the bottom),
mirroring the xcc_client.py separation in the bare-metal starter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth


class RedfishDiscoveryError(RuntimeError):
    pass


@dataclass
class RedfishDiscovery:
    bmc_ip: str
    username: str
    password: str
    verify_tls: bool = False
    timeout: int = 30

    _system_path: Optional[str] = field(default=None, init=False, repr=False)
    _manager_path: Optional[str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.base_url = f"https://{self.bmc_ip}"
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(self.username, self.password)
        self.session.verify = self.verify_tls
        if not self.verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    # ---------- low level ----------

    def _get(self, path: str) -> dict:
        r = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _section_error(exc: Exception) -> dict:
        return {"error": f"{type(exc).__name__}: {exc}"}

    # ---------- discovery ----------

    def discover_paths(self) -> None:
        """Resolve the (single) ComputerSystem and Manager paths from the service root."""
        root = self._get("/redfish/v1/")
        systems = self._get(root["Systems"]["@odata.id"])
        self._system_path = systems["Members"][0]["@odata.id"]
        managers = self._get(root["Managers"]["@odata.id"])
        self._manager_path = managers["Members"][0]["@odata.id"]

    def system_info(self) -> dict:
        try:
            if not self._system_path:
                self.discover_paths()
            system = self._get(self._system_path)
            return {
                "system_path": self._system_path,
                "manufacturer": system.get("Manufacturer"),
                "model": system.get("Model"),
                "sku": system.get("SKU"),
                "serial_number": system.get("SerialNumber"),
                "part_number": system.get("PartNumber"),
                "uuid": system.get("UUID"),
                "power_state": system.get("PowerState"),
                "bios_version": system.get("BiosVersion"),
                "processor_summary": system.get("ProcessorSummary"),
                "memory_summary": system.get("MemorySummary"),
            }
        except Exception as exc:  # noqa: BLE001 - best-effort section
            return self._section_error(exc)

    def bios_attributes(self) -> dict:
        """Current BIOS attributes — the exact names/values bmc/se350_bios.yaml must use."""
        try:
            if not self._system_path:
                self.discover_paths()
            bios = self._get(f"{self._system_path}/Bios")
            return {
                "attribute_registry": bios.get("AttributeRegistry"),
                "attributes": bios.get("Attributes", {}),
            }
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def bios_pending_settings(self) -> dict:
        try:
            if not self._system_path:
                self.discover_paths()
            pending = self._get(f"{self._system_path}/Bios/Settings")
            return {"attributes": pending.get("Attributes", {})}
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def bios_attribute_registry(self) -> dict:
        """Allowed values per BIOS attribute, when the XCC publishes a registry."""
        try:
            registries = self._get("/redfish/v1/Registries")
            for member in registries.get("Members", []):
                member_path = member.get("@odata.id", "")
                if "BiosAttributeRegistry" not in member_path:
                    continue
                entry = self._get(member_path)
                for location in entry.get("Location", []):
                    uri = location.get("Uri")
                    if uri:
                        return self._get(uri)
            return {"error": "No BiosAttributeRegistry found under /redfish/v1/Registries"}
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def manager_info(self) -> dict:
        try:
            if not self._manager_path:
                self.discover_paths()
            manager = self._get(self._manager_path)
            return {
                "manager_path": self._manager_path,
                "model": manager.get("Model"),
                "firmware_version": manager.get("FirmwareVersion"),
                "manager_type": manager.get("ManagerType"),
            }
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def virtual_media(self) -> dict:
        """List VirtualMedia members and classify by Id prefix.

        EXT members present == the Redfish network-ISO-mount path exists on this
        unit at its current license + firmware (the load-bearing check for the
        no-USB bare-metal install).
        """
        try:
            if not self._manager_path:
                self.discover_paths()
            manager = self._get(self._manager_path)
            collection = self._get(manager["VirtualMedia"]["@odata.id"])
            members = []
            for ref in collection.get("Members", []):
                detail = self._get(ref["@odata.id"])
                members.append(
                    {
                        "path": ref["@odata.id"],
                        "id": detail.get("Id"),
                        "name": detail.get("Name"),
                        "media_types": detail.get("MediaTypes"),
                        "connected_via": detail.get("ConnectedVia"),
                        "inserted": detail.get("Inserted"),
                        "image": detail.get("Image"),
                    }
                )
            ext_members = [m for m in members if str(m["id"]).upper().startswith("EXT")]
            return {
                "member_count": len(members),
                "ext_member_ids": [m["id"] for m in ext_members],
                "ext_members_present": bool(ext_members),
                "members": members,
            }
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def secure_boot(self) -> dict:
        try:
            if not self._system_path:
                self.discover_paths()
            sb = self._get(f"{self._system_path}/SecureBoot")
            return {
                "enabled": sb.get("SecureBootEnable"),
                "current_boot": sb.get("SecureBootCurrentBoot"),
                "mode": sb.get("SecureBootMode"),
            }
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def firmware_inventory(self) -> dict:
        try:
            inventory = self._get("/redfish/v1/UpdateService/FirmwareInventory")
            items = []
            for member in inventory.get("Members", []):
                try:
                    detail = self._get(member["@odata.id"])
                    items.append(
                        {
                            "id": detail.get("Id"),
                            "name": detail.get("Name"),
                            "version": detail.get("Version"),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - keep the rest of the list
                    items.append({"id": member.get("@odata.id"), "error": str(exc)})
            return {"items": items}
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    def chassis_and_oem(self) -> dict:
        """Chassis members + Manager Oem section, hunting for a thermal-mode setting.

        The legacy tool sets thermal mode via the XCC SSH CLI ("thermal
        performance") — it is not a UEFI Bios attribute. This probe looks for a
        Redfish OEM representation of the same setting so ApplyBiosPolicyJob
        can stay pure-Redfish; if nothing surfaces, the job keeps one small
        XCC-SSH step instead.
        """
        try:
            result: dict = {"chassis_members": [], "manager_oem": {}, "thermal_hits": []}
            root = self._get("/redfish/v1/")
            chassis_col = self._get(root["Chassis"]["@odata.id"])
            for ref in chassis_col.get("Members", []):
                result["chassis_members"].append(self._get(ref["@odata.id"]))
            if not self._manager_path:
                self.discover_paths()
            result["manager_oem"] = self._get(self._manager_path).get("Oem", {})

            def hunt(obj, path):
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        sub = f"{path}.{key}" if path else key
                        if any(w in key.lower() for w in ("thermal", "cooling", "fanmode", "fan_mode", "acoustic")):
                            result["thermal_hits"].append(
                                {"path": sub, "value": value if not isinstance(value, (dict, list)) else "<object>"}
                            )
                        hunt(value, sub)
                elif isinstance(obj, list):
                    for i, value in enumerate(obj):
                        hunt(value, f"{path}[{i}]")

            for i, member in enumerate(result["chassis_members"]):
                hunt(member, f"chassis[{i}]")
            hunt(result["manager_oem"], "manager.Oem")
            return result
        except Exception as exc:  # noqa: BLE001
            return self._section_error(exc)

    # ---------- write operations (opt-in; used by the job's write checks) ----------

    def _patch(self, path: str, body: dict) -> None:
        r = self.session.patch(f"{self.base_url}{path}", json=body, timeout=self.timeout)
        if r.status_code not in (200, 202, 204):
            raise RedfishDiscoveryError(f"PATCH {path} failed: {r.status_code} {r.text}")

    def _post(self, path: str, body: dict) -> None:
        r = self.session.post(f"{self.base_url}{path}", json=body, timeout=self.timeout)
        if r.status_code not in (200, 202, 204):
            raise RedfishDiscoveryError(f"POST {path} failed: {r.status_code} {r.text}")

    def mount_iso(self, iso_url: str) -> dict:
        """Mount an ISO via the platform-correct virtual media method.

        XCC1 (SE350, firmware "19A"+): PATCH on a free EXT member — the only
        Redfish-insertable members on that generation; ISO URL must be plain
        HTTP or credential-less NFS. XCC2 (SE455 V3+): standard POST
        InsertMedia on a CD/DVD-capable member. Mode is auto-detected from the
        collection contents. Returns {"mode": ..., "member_path": ...}.
        """
        vm = self.virtual_media()
        if "error" in vm:
            raise RedfishDiscoveryError(f"VirtualMedia enumeration failed: {vm['error']}")
        members = vm["members"]
        ext = [m for m in members if str(m["id"]).upper().startswith("EXT")]
        if ext:
            member = next((m for m in ext if not m.get("inserted")), ext[0])
            try:
                self._patch(
                    member["path"],
                    {"Image": iso_url, "WriteProtected": True, "Inserted": True},
                )
            except RedfishDiscoveryError:
                # Some XCC1 firmware rejects Inserted/WriteProtected in the body.
                self._patch(member["path"], {"Image": iso_url})
            return {"mode": "xcc1-patch-ext", "member_path": member["path"]}
        cd = next(
            (m for m in members if set(m.get("media_types") or []) & {"CD", "DVD"}),
            None,
        )
        member = cd or (members[0] if members else None)
        if not member:
            raise RedfishDiscoveryError("No VirtualMedia members available to mount into")
        self._post(
            f"{member['path']}/Actions/VirtualMedia.InsertMedia",
            {"Image": iso_url, "Inserted": True, "WriteProtected": True},
        )
        return {"mode": "xcc2-insertmedia", "member_path": member["path"]}

    def media_inserted(self, member_path: str) -> bool:
        detail = self._get(member_path)
        return bool(detail.get("Inserted")) and bool(detail.get("Image"))

    def wait_media_state(
        self, member_path: str, inserted: bool, poll_seconds: int = 5, timeout: int = 120
    ) -> bool:
        """Poll until the member's Inserted state matches `inserted` (True/False)."""
        waited = 0
        while waited <= timeout:
            if self.media_inserted(member_path) == inserted:
                return True
            time.sleep(poll_seconds)
            waited += poll_seconds
        return False

    def eject_iso(self, member_path: str, mode: str) -> None:
        if mode == "xcc1-patch-ext":
            self._patch(member_path, {"Image": None})
        else:
            self._post(f"{member_path}/Actions/VirtualMedia.EjectMedia", {})

    def set_boot_once_cd(self) -> None:
        if not self._system_path:
            self.discover_paths()
        self._patch(
            self._system_path,
            {"Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": "Cd"}},
        )

    def get_power_state(self) -> str:
        if not self._system_path:
            self.discover_paths()
        return self._get(self._system_path).get("PowerState", "Unknown")

    def power_action(self, action: str) -> None:
        """action: Redfish ResetType — 'On', 'ForceRestart', 'GracefulShutdown', ..."""
        if not self._system_path:
            self.discover_paths()
        self._post(f"{self._system_path}/Actions/ComputerSystem.Reset", {"ResetType": action})

    # ---------- one-shot report ----------

    def full_report(self) -> dict:
        self.discover_paths()  # let a total connectivity/auth failure raise loudly
        return {
            "bmc_ip": self.bmc_ip,
            "system": self.system_info(),
            "manager": self.manager_info(),
            "bios": self.bios_attributes(),
            "bios_pending": self.bios_pending_settings(),
            "bios_registry": self.bios_attribute_registry(),
            "virtual_media": self.virtual_media(),
            "secure_boot": self.secure_boot(),
            "firmware_inventory": self.firmware_inventory(),
            "chassis": self.chassis_and_oem(),
        }


if __name__ == "__main__":
    # Standalone smoke test:
    #   python redfish_discovery.py --bmc-ip 10.0.0.10 --username USER --password PASS --insecure
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Read-only Redfish discovery dump for Lenovo XCC")
    parser.add_argument("--bmc-ip", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification (self-signed BMC certs)")
    args = parser.parse_args()

    discovery = RedfishDiscovery(
        bmc_ip=args.bmc_ip,
        username=args.username,
        password=args.password,
        verify_tls=not args.insecure,
    )
    print(json.dumps(discovery.full_report(), indent=2))
