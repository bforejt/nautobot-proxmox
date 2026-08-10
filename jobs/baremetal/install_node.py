"""
Nautobot Job: install Proxmox VE on a bare-metal (or nested-lab) node — L0.

SoT-driven end to end: ONE input, the Hypervisor Device in
provisioning_state=awaiting_install. Everything else resolves from Nautobot
per the contract: target PVE version from device.software_version (Active-
gated, like every other deploy), the prepared installer ISO from its
SoftwareImageFile, the delivery method from the DeviceType's profile
(bmc/profiles/), the carrier host (nested) via Hosted On, the BMC (physical)
via the xcc interface.

The job's share of the work is deliberately small — boot the installer, then
watch the state machine. The heavy lifting is the answer service's: it gets
the installer's identity POST, renders the per-node answer.toml, receives the
post-install webhook (flips provisioning_state to bm_installed), and stores
the firstboot-created API token as this node's SecretsGroup. See
docs/baremetal-install.md.
"""

import time

from nautobot.apps.jobs import BooleanVar, Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import Secret

from ..lib.install_delivery import (
    DeliveryError,
    PveNestedDelivery,
    RedfishVmediaDelivery,
    load_profile,
)
from ..lib.nautobot_helpers import resolve_hypervisor, resolve_proxmox_credentials
from ..lib.proxmox_client import ProxmoxClient
from ..lib.redfish_discovery import RedfishDiscovery

XCC_USERNAME_SECRET_NAME = "xcc_username"
XCC_PASSWORD_SECRET_NAME = "xcc_password"


class ContractViolation(Exception):
    """The SoT record is missing something the contract requires — refuse
    precisely before touching anything (fail closed)."""


def _require(condition, message):
    if not condition:
        raise ContractViolation(message)


class InstallProxmoxNode(Job):
    class Meta:
        name = "Install Proxmox Node (SoT-driven)"
        description = (
            "Boots the prepared auto-installer on a Hypervisor Device in "
            "provisioning_state=awaiting_install (nested lab VM or Redfish "
            "virtual media per the DeviceType profile) and follows the state "
            "machine to bm_installed + stored credentials. Reinstalls the OS "
            "— requires explicit confirmation."
        )
        has_sensitive_variables = False
        # Worst case (nested): ISO pull (~1800s) + install to power-off
        # (~2700s) + state watch (1800s) + overhead — limits must exceed it.
        soft_time_limit = 9000
        time_limit = 9600

    device = ObjectVar(
        model=Device,
        label="Node to install",
        description="Hypervisor Device in provisioning_state=awaiting_install",
        query_params={"role": "Hypervisor"},
    )
    confirm = BooleanVar(
        label="Confirm install",
        description="This boots an OS installer against the target. Required.",
        default=False,
    )

    # ---- contract resolution ----

    def _resolve_image(self, device):
        sv = device.software_version
        _require(sv is not None, f"{device.name} has no software_version (target PVE release)")
        _require(
            sv.status.name == "Active",
            f"SoftwareVersion {sv.version} is {sv.status.name}, not Active — "
            "the promotion gate applies to installer images too",
        )
        image = (
            sv.software_image_files.filter(default_image=True).first()
            or sv.software_image_files.first()
        )
        _require(image is not None, f"SoftwareVersion {sv.version} has no SoftwareImageFile")
        _require(image.download_url, f"Image {image.image_file_name} has no download_url")
        return image

    def _mgmt_mac(self, device):
        if device.primary_ip4 is None:
            return None
        for iface in device.interfaces.all():
            if device.primary_ip4 in iface.ip_addresses.all():
                return str(iface.mac_address) if iface.mac_address else None
        return None

    # ---- delivery paths ----

    def _install_nested(self, device, profile, image):
        carrier = resolve_hypervisor(device)
        _require(
            carrier.primary_ip4 is not None,
            f"Carrier host {carrier.name} has no primary_ip4",
        )
        vm_storage = carrier.cf.get("vm_storage")
        _require(vm_storage, f"Carrier host {carrier.name} has no vm_storage custom field")
        token_id, token_secret = resolve_proxmox_credentials(carrier)
        client = ProxmoxClient(
            host=str(carrier.primary_ip4.address.ip),
            token_id=token_id, token_secret=token_secret,
        )
        delivery = PveNestedDelivery(client, carrier.name, self.logger)
        vm_cfg = profile["delivery"].get("vm", {})

        iso_volid = delivery.ensure_iso(
            vm_cfg.get("iso_storage", "local"),
            image.image_file_name,
            image.download_url,
            image.image_file_checksum or None,
            image.hashing_algorithm or "sha256",
        )
        vmid = device.cf.get("vmid") or client.next_vmid()
        # Reinstall reconciliation: confirm=True is an explicit reinstall
        # gate, so a stale install VM under our vmid/name is removed — but a
        # FOREIGN VM owning the vmid is a hard refusal, never collateral.
        for vm in client.list_vms(carrier.name):
            if int(vm.get("vmid", -1)) == int(vmid) or vm.get("name") == device.name:
                _require(
                    vm.get("name") == device.name,
                    f"VMID {vmid} on {carrier.name} belongs to {vm.get('name')!r}, "
                    f"not {device.name} — refusing to touch it",
                )
                self.logger.info(
                    "Confirmed reinstall — destroying stale install VM %s (%s)",
                    vm["vmid"], vm.get("name"),
                )
                if vm.get("status") == "running":
                    client.stop_vm(carrier.name, int(vm["vmid"]))
                client.destroy_vm(carrier.name, int(vm["vmid"]))
                vmid = int(vm["vmid"])
        delivery.boot_installer(
            vmid=int(vmid),
            name=device.name,
            serial=device.serial,
            iso_volid=iso_volid,
            vm_storage=str(vm_storage),
            vm_cfg=vm_cfg,
            mgmt_mac=self._mgmt_mac(device),
        )
        device._custom_field_data["vmid"] = int(vmid)
        device.validated_save()
        self.logger.info(
            "Installer booted in VM %s on %s — the answer service takes it from here "
            "(identity POST -> answer.toml -> unattended install)", vmid, carrier.name,
        )
        delivery.wait_install_poweroff(int(vmid))
        # Power-off alone is not success — the installer's webhook fires
        # BEFORE its power-off, so on a real install the state flip is
        # already visible. No flip = the VM died some other way.
        deadline = time.time() + 120
        while time.time() < deadline:
            device.refresh_from_db()
            if device.cf.get("provisioning_state") == "bm_installed":
                break
            time.sleep(5)
        else:
            raise RuntimeError(
                f"VM {vmid} powered off but provisioning_state never reached "
                "bm_installed — install likely failed. ISO left attached; check "
                "the answer service log and the VM's serial console."
            )
        self.logger.info("Install confirmed (webhook landed) — detaching ISO, booting from disk")
        delivery.finalize_boot_from_disk(int(vmid))

    def _install_vmedia(self, device, image):
        xcc_iface = device.interfaces.filter(name="xcc").first()
        _require(
            xcc_iface is not None and xcc_iface.ip_addresses.exists(),
            f"{device.name} has no 'xcc' interface with an IP (contract §4 BMC address)",
        )
        try:
            username = Secret.objects.get(name=XCC_USERNAME_SECRET_NAME).get_value()
            password = Secret.objects.get(name=XCC_PASSWORD_SECRET_NAME).get_value()
        except Secret.DoesNotExist as exc:
            raise ContractViolation(
                f"XCC credential Secrets missing (need {XCC_USERNAME_SECRET_NAME!r} "
                f"and {XCC_PASSWORD_SECRET_NAME!r}): {exc}"
            )
        _require(
            image.download_url.startswith("http://"),
            "XCC1 virtual media mounts plain-HTTP ISO URLs only — publish the "
            f"prepared ISO on the plain-HTTP vhost (got {image.download_url})",
        )
        bmc_ip = str(xcc_iface.ip_addresses.first().address.ip)
        redfish = RedfishDiscovery(bmc_ip=bmc_ip, username=username, password=password)
        mount = RedfishVmediaDelivery(redfish, self.logger).boot_installer(image.download_url)
        # Remember the mount so a confirmed install can eject it — otherwise
        # stale media accumulates on the EXT slots across installs.
        self._vmedia_mount = (redfish, mount)
        self.logger.info(
            "Node is booting the installer from virtual media — the answer service "
            "takes it from here"
        )

    # ---- state watch ----

    def _watch_state_machine(self, device, timeout=1800, poll=30):
        """Follow provisioning_state -> bm_installed (webhook) and the
        credentials phone-home (secrets_group CF). Informative, not fatal —
        the install continues without us either way."""
        deadline = time.time() + timeout
        seen_installed = seen_credentials = False
        while time.time() < deadline and not (seen_installed and seen_credentials):
            device.refresh_from_db()
            state = device.cf.get("provisioning_state")
            if not seen_installed and state == "bm_installed":
                seen_installed = True
                self.logger.info("Webhook landed: provisioning_state=bm_installed")
            if not seen_credentials and device.cf.get("secrets_group"):
                seen_credentials = True
                self.logger.info(
                    "Firstboot credentials stored: SecretsGroup %r",
                    device.cf.get("secrets_group"),
                )
            if not (seen_installed and seen_credentials):
                time.sleep(poll)
        return seen_installed, seen_credentials

    def run(self, device, confirm):
        _require(confirm, "Confirmation not given — refusing to boot an installer")
        _require(
            device.cf.get("provisioning_state") == "awaiting_install",
            f"{device.name} provisioning_state is "
            f"{device.cf.get('provisioning_state')!r}, not 'awaiting_install' — "
            "set the intent in the SoT first",
        )
        _require(
            device.serial,
            f"{device.name} has no serial — the installer's identity POST matches on it",
        )
        image = self._resolve_image(device)
        profile = load_profile(device.device_type.model)
        method = profile["delivery"].get("method")

        try:
            if method == "pve-nested":
                self._install_nested(device, profile, image)
            elif method == "redfish-vmedia":
                self._install_vmedia(device, image)
            elif method == "pxe":
                raise ContractViolation(
                    f"{device.device_type.model} installs via PXE — there is no "
                    "job step. Power the machine on (netboot); the answer "
                    "service drives the install and the state machine. This "
                    "job is only needed for deliveries that must push boot "
                    "media (nested VM, BMC virtual media)."
                )
            else:
                raise ContractViolation(
                    f"Unknown delivery method {method!r} in the "
                    f"{device.device_type.model} profile"
                )
        except DeliveryError as exc:
            raise RuntimeError(f"Delivery failed: {exc}") from exc

        installed, credentials = self._watch_state_machine(device)
        # vmedia cleanup: once the webhook has confirmed the install, the
        # mounted installer media is spent — eject it (best-effort; the
        # boot-once override already cleared, so a failed eject is cosmetic).
        if installed and getattr(self, "_vmedia_mount", None):
            redfish, mount = self._vmedia_mount
            try:
                redfish.eject_iso(mount["member_path"], mount["mode"])
                self.logger.info("Installer media ejected from %s", mount["member_path"])
            except Exception as exc:
                self.logger.warning(
                    "Could not eject installer media from %s (%s) — eject it via "
                    "a discovery-job write-test run or the XCC UI",
                    mount["member_path"], exc,
                )
        if installed and credentials:
            return (
                f"{device.name}: installed, state=bm_installed, per-node API token "
                f"stored (SecretsGroup {device.cf.get('secrets_group')!r})."
            )
        return (
            f"{device.name}: installer delivered; state machine incomplete within the "
            f"watch window (webhook={'ok' if installed else 'pending'}, "
            f"credentials={'ok' if credentials else 'pending'}) — check the answer "
            "service log and re-check the device's provisioning_state."
        )


register_jobs(InstallProxmoxNode)
