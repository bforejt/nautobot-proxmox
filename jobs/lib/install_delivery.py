"""
Delivery adapters for the bare-metal install engine (decision #41 + #42).

The install core is delivery-agnostic: a prepared auto-install artifact boots,
POSTs its identity to the answer service, and installs from a per-node
answer.toml. The ONLY delivery-specific work is "make this machine boot that
artifact" — which is what these adapters do, selected by the DeviceType's
profile in bmc/profiles/<slug>.yaml:

  pve-nested      A VM on an existing lab Proxmox host stands in for a blank
                  server (carrier resolved via the Hosted On relationship).
                  Proves the whole L0 loop with zero special hardware.
  redfish-vmedia  BMC virtual media + one-shot CD boot. Vendor quirks (XCC1
                  PATCH-on-EXT vs XCC2 InsertMedia) live in the dual-mode
                  client, not here.
  (pxe)           Not an adapter at all: PXE-booting the same prepared
                  artifact needs only DHCP/boot infra outside Nautobot; the
                  answer service serves it unchanged.

Pure Python (no Nautobot imports) so the module loads anywhere; callers pass
resolved values in.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML ships with Nautobot
    yaml = None

from .proxmox_client import ProxmoxClient

PROFILE_DIR = Path(__file__).resolve().parents[2] / "bmc" / "profiles"


class DeliveryError(RuntimeError):
    """A delivery step failed or a profile is missing/invalid."""


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def load_profile(device_type_model: str) -> dict:
    """Load bmc/profiles/<device-type-slug>.yaml (platform profiles as data,
    decision #14). No profile = this DeviceType is not installable — refuse."""
    if yaml is None:
        raise DeliveryError("PyYAML is unavailable — cannot read install profiles")
    path = PROFILE_DIR / f"{slugify(device_type_model)}.yaml"
    if not path.exists():
        raise DeliveryError(
            f"No install profile for DeviceType {device_type_model!r} "
            f"(expected bmc/profiles/{path.name})"
        )
    profile = yaml.safe_load(path.read_text())
    if not isinstance(profile, dict) or "delivery" not in profile:
        raise DeliveryError(f"Install profile {path.name} is malformed (no delivery section)")
    return profile


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class PveNestedDelivery:
    """Boot the prepared installer ISO in a VM on a carrier Proxmox host.

    The nested VM is created with the target Device's SMBIOS serial so the
    installer's identity POST matches the SoT record exactly as a physical
    machine's would.
    """

    def __init__(self, client: ProxmoxClient, node: str, logger):
        self.client = client
        self.node = node
        self.logger = logger

    def ensure_iso(self, storage: str, filename: str, url: str,
                   checksum: str | None, checksum_algorithm: str = "sha256") -> str:
        for item in self.client.storage_content(self.node, storage, "iso"):
            if item.get("volid", "").endswith(f"/{filename}"):
                self.logger.info("Installer ISO already on %s: %s", self.node, item["volid"])
                return item["volid"]
        self.logger.info("Pulling installer ISO %s onto %s (checksum-verified)", filename, self.node)
        return self.client.download_url(
            self.node, storage, url, filename, content="iso",
            checksum=checksum, checksum_algorithm=checksum_algorithm,
        )

    def boot_installer(self, *, vmid: int, name: str, serial: str, iso_volid: str,
                       vm_storage: str, vm_cfg: dict, mgmt_mac: str | None) -> int:
        net0 = f"virtio={mgmt_mac},bridge={vm_cfg.get('bridge', 'vmbr0')}" if mgmt_mac \
            else f"virtio,bridge={vm_cfg.get('bridge', 'vmbr0')}"
        params = {
            "vmid": vmid,
            "name": name,
            "memory": int(vm_cfg.get("memory_mb", 4096)),
            "sockets": 1,
            "cores": int(vm_cfg.get("cores", 2)),
            "cpu": "host",  # nested KVM: the installed PVE must virtualize too
            "ostype": "l26",
            "scsihw": "virtio-scsi-single",
            "scsi0": f"{vm_storage}:{int(vm_cfg.get('disk_gb', 32))}",
            "ide2": f"{iso_volid},media=cdrom",
            "boot": "order=ide2;scsi0",
            "net0": net0,
            "serial0": "socket",
            "onboot": 0,
            "smbios1": f"base64=1,serial={_b64(serial)}",
            "tags": "nfv;l0-lab",
        }
        self.logger.info("Creating nested install VM %s (%s) on %s", vmid, name, self.node)
        self.client.create_vm(self.node, params)
        self.client.start_vm(self.node, vmid)
        return vmid

    def vm_status(self, vmid: int) -> str:
        return self.client.get(f"/nodes/{self.node}/qemu/{vmid}/status/current")["status"]

    def wait_install_poweroff(self, vmid: int, timeout: int = 2700, poll: int = 20) -> None:
        """The nested profile's answer file sets reboot-mode=power-off (the
        prepared ISO would otherwise reinstall on every boot). Wait for it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.vm_status(vmid) == "stopped":
                return
            time.sleep(poll)
        raise DeliveryError(
            f"Nested install VM {vmid} did not power off within {timeout}s — "
            "check the answer service log and the VM's serial console"
        )

    def finalize_boot_from_disk(self, vmid: int) -> None:
        self.client.set_vm_config(self.node, vmid, {
            "ide2": "none,media=cdrom",
            "boot": "order=scsi0",
        })
        self.client.start_vm(self.node, vmid)


class RedfishVmediaDelivery:
    """BMC virtual media + one-shot CD boot (physical servers).

    Wraps the dual-mode client (XCC1 PATCH-on-EXT / XCC2 InsertMedia) that the
    discovery job already validated read-side on real SE350s. XCC1 constraint:
    iso_url must be plain HTTP.
    """

    def __init__(self, redfish, logger):
        self.redfish = redfish  # a jobs.lib.redfish_discovery.RedfishDiscovery
        self.logger = logger

    def boot_installer(self, iso_url: str) -> dict:
        mount = self.redfish.mount_iso(iso_url)
        self.logger.info("Mounted %s via %s (%s)", iso_url, mount["mode"], mount["member_path"])
        if not self.redfish.wait_media_state(mount["member_path"], True):
            raise DeliveryError(
                f"Virtual media never reported Inserted=true for {iso_url} "
                f"(mode {mount['mode']}, member {mount['member_path']}) — the BMC's "
                "ISO fetch likely failed; NOT arming CD boot or touching power"
            )
        self.redfish.set_boot_once_cd()
        state = self.redfish.get_power_state()
        action = "ForceRestart" if state == "On" else "On"
        self.logger.info("Power state %s -> %s (one-shot CD boot armed)", state, action)
        self.redfish.power_action(action)
        return mount
