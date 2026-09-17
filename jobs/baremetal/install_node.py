"""
Nautobot Job: install Proxmox VE on a bare-metal (or nested-lab) node — L0.

SoT-driven end to end: ONE input, the NFV-role Device in
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
from nautobot.extras.models import ExternalIntegration

from ..lib.answer_service import (
    INTEGRATION_NAME,
    evaluate_profile_preflight,
    fetch_info,
    profile_feature_keys,
)
from ..lib.install_delivery import (
    DeliveryError,
    PveNestedDelivery,
    RedfishVmediaDelivery,
    load_profile,
    slugify,
)
from ..lib.nautobot_helpers import (
    CredentialError,
    resolve_bmc,
    resolve_hypervisor,
    resolve_proxmox_credentials,
)
from ..lib.proxmox_client import ProxmoxClient
from ..lib.redfish_discovery import RedfishDiscovery
from ..lib.storage_layout import StorageLayoutError, apply_storage_layout, parse_storage_spec


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
            "Boots the prepared auto-installer on an NFV-role Device in "
            "provisioning_state=awaiting_install (nested lab VM or Redfish "
            "virtual media per the DeviceType profile; profiles with a "
            "`storage` section get their RAID volumes created out-of-band "
            "first) and follows the state "
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
        description="NFV-role Device in provisioning_state=awaiting_install",
        query_params={"role": "NFV"},
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

    # ---- answer-service preflight ----

    def _preflight_answer_service(self, device, profile):
        """Refuse before touching a BMC when the answer service demonstrably
        cannot answer this node: its baked-in profile list (GET /info) lacks
        the DeviceType's profile, or its build predates a feature the profile
        uses. Found through the `nfv-answer-service` ExternalIntegration (the
        media forge's plumbing); no integration or an unreachable service is a
        warning, not a refusal — the node, not this worker, must reach it."""
        integration = ExternalIntegration.objects.filter(name=INTEGRATION_NAME).first()
        if integration is None:
            self.logger.warning(
                "No ExternalIntegration %r — skipping the answer-service profile preflight",
                INTEGRATION_NAME,
            )
            return
        base = integration.remote_url.rstrip("/")
        info = fetch_info(base, verify=integration.verify_ssl)
        verdict, message = evaluate_profile_preflight(
            info, slugify(device.device_type.model), profile_feature_keys(profile), base
        )
        if verdict == "refuse":
            raise ContractViolation(message)
        (self.logger.warning if verdict == "warn" else self.logger.info)("%s", message)

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

    def _install_vmedia(self, device, profile, image):
        try:
            bmc_ip, username, password = resolve_bmc(device)
        except CredentialError as exc:
            raise ContractViolation(str(exc))
        # Image-URL schemes the BMC generation can mount (profile
        # delivery.iso_url_schemes). Default = XCC1 reality: plain HTTP only
        # (or credential-less NFS); XCC2 profiles also allow https.
        schemes = [
            str(x).lower() for x in profile.get("delivery", {}).get("iso_url_schemes", ["http"])
        ]
        _require(
            any(image.download_url.startswith(f"{scheme}://") for scheme in schemes),
            f"{device.device_type.model} virtual media mounts {'/'.join(schemes)} ISO "
            f"URLs only — publish the prepared ISO accordingly (got {image.download_url})",
        )
        redfish = RedfishDiscovery(bmc_ip=bmc_ip, username=username, password=password)
        # Out-of-band RAID layout (profile `storage`, decision #50): the
        # adapter must present the boot/data virtual drives before the
        # installer boots. Creates what is missing, keeps what exists, powers
        # the host on into UEFI Setup if it was off — refuses on any ambiguity.
        storage_spec = parse_storage_spec(profile)
        if storage_spec:
            try:
                summary = apply_storage_layout(redfish, storage_spec, self.logger)
            except StorageLayoutError as exc:
                raise ContractViolation(f"Storage layout refused: {exc}")
            self.logger.info(
                "Storage layout on %s: created %s, kept %s",
                summary["controller"], summary["created"] or "nothing", summary["kept"] or "nothing",
            )
            for warning in summary["warnings"]:
                self.logger.warning("%s", warning)
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
        self._preflight_answer_service(device, profile)
        method = profile["delivery"].get("method")

        try:
            if method == "pve-nested":
                self._install_nested(device, profile, image)
            elif method == "redfish-vmedia":
                self._install_vmedia(device, profile, image)
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
