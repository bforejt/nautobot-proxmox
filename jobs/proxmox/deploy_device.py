"""
Nautobot Job: deploy a VNF Device — fully SoT-driven (the contract consumer).

Input: ONE Device (status Planned). Everything else is read from Nautobot per
docs/sot-data-contract.md:

  hypervisor        <- Hosted On relationship (source side)
  node name         <- hypervisor.name          api host <- hypervisor.primary_ip4
  bridge/storages   <- hypervisor CFs (vm_bridge, vm_storage, import_storage)
  image             <- device.software_version (must be Active) -> default image file
  sizing            <- device CFs vcpus/memory_mb/disk_gb (required)
  day-0 mechanism   <- platform CF day0_builder     machine <- platform CF machine_type
  NICs              <- device Interfaces in the platform's nic_order (facts),
                       pinned MACs required, VLANs -> tag=/trunks=
  addressing        <- interface IP => static + gateway from the DefaultGW-role
                       IP in the enclosing Prefix; no IP => DHCP

Fail-closed: any missing contract datum is a precise refusal before anything
is touched. Write-back on success: CF vmid, status Planned -> Active.
"""

from nautobot.apps.jobs import BooleanVar, Job, ObjectVar, TextVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import RelationshipAssociation, Secret, Status
from nautobot.ipam.models import IPAddress

from ..lib.nautobot_helpers import resolve_proxmox_credentials
from ..lib.platform_facts import get_platform_facts
from ..lib.proxmox_client import ProxmoxClient, ProxmoxError

# Fleet-wide console password for cloud-init guests (users log in at the
# desktop/console, never SSH). Proxmox hashes it before storing; the plaintext
# only transits the TLS API call. Rotation = update this Secret + a converge
# job that re-pushes cipassword (applies on next boot).
CONSOLE_PASSWORD_SECRET = "jumphost_console_password"


class ContractViolation(ValueError):
    """A required SoT datum is missing — see sot-data-contract.md."""


def _require(value, description):
    if value in (None, ""):
        raise ContractViolation(f"Contract violation: {description} is not set in Nautobot")
    return value


class DeployVnfDevice(Job):
    class Meta:
        name = "Deploy VNF Device (SoT-driven)"
        description = (
            "Deploys one Planned VNF Device reading everything from Nautobot per the "
            "SoT data contract: hypervisor via Hosted On, image via software_version "
            "(Active-gated), sizing/tunables from device+platform fields, NICs from "
            "Interface records. Fail-closed on any missing datum. Flips the device "
            "to Active and records the VMID on success."
        )
        has_sensitive_variables = False
        soft_time_limit = 1800
        time_limit = 2100

    device = ObjectVar(
        model=Device,
        label="VNF Device",
        description="A Planned VNF device with a Hosted On relationship to its hypervisor",
        query_params={"status": "Planned"},
    )
    ssh_pubkeys = TextVar(
        label="SSH Public Key(s)",
        required=False,
        description="Interim operator access for cloud-init guests (until user management enters the contract)",
    )
    wait_for_agent = BooleanVar(label="Wait for guest agent IP", default=True)

    # ---------- contract reads ----------

    def _resolve_hypervisor(self, device):
        assoc = RelationshipAssociation.objects.filter(
            relationship__key="hosted_on", destination_id=device.id
        ).first()
        if assoc is None:
            raise ContractViolation(
                f"Contract violation: {device.name} has no 'Hosted On' relationship to a hypervisor"
            )
        return assoc.source

    def _gateway_for(self, ip):
        prefix = ip.parent
        if prefix is None:
            raise ContractViolation(f"IP {ip} has no parent Prefix")
        gw = IPAddress.objects.filter(parent=prefix, role__name="DefaultGW").first()
        if gw is None:
            raise ContractViolation(
                f"Contract violation: prefix {prefix} has no DefaultGW-role IP address"
            )
        return str(gw.address.ip)

    def _render_nics(self, device, facts, bridge):
        """Interfaces -> ordered Proxmox netN strings + the ipconfig for net0."""
        nics, ipconfig0 = {}, None
        for index, if_name in enumerate(facts["nic_order"]):
            iface = device.interfaces.filter(name=if_name).first()
            if iface is None:
                raise ContractViolation(
                    f"Contract violation: interface {if_name!r} (position {index}) missing on {device.name}"
                )
            mac = _require(iface.mac_address, f"MAC address on {device.name}:{if_name}")
            net = f"{facts['nic_model']}={mac},bridge={bridge}"
            if iface.untagged_vlan:
                net += f",tag={iface.untagged_vlan.vid}"
            tagged = list(iface.tagged_vlans.all())
            if tagged:
                net += ",trunks=" + ";".join(str(v.vid) for v in tagged)
            nics[f"net{index}"] = net

            if index == 0:
                ip = iface.ip_addresses.first()
                if ip is not None:
                    ipconfig0 = f"ip={ip.address},gw={self._gateway_for(ip)}"
                else:
                    ipconfig0 = "ip=dhcp"
        return nics, ipconfig0

    # ---------- run ----------

    def run(self, device, ssh_pubkeys, wait_for_agent):
        if device.status.name != "Planned":
            raise ContractViolation(
                f"{device.name} is {device.status.name}, not Planned — "
                "redeploys/changes are separate jobs (SoT-first)"
            )

        platform = _require(device.platform, f"platform on {device.name}")
        facts = get_platform_facts(platform.name)
        day0 = _require(platform.cf.get("day0_builder"), f"day0_builder on platform {platform.name}")
        machine = _require(platform.cf.get("machine_type"), f"machine_type on platform {platform.name}")
        if day0 != "native-cloudinit":
            raise ContractViolation(
                f"Platform {platform.name} binds day0_builder={day0!r}, which this code "
                "version does not ship (supported: native-cloudinit)"
            )

        sv = _require(device.software_version, f"software_version on {device.name}")
        if sv.status.name != "Active":
            raise ContractViolation(
                f"{device.name} wants software version {sv.version!r} with status "
                f"{sv.status.name!r} — only Active versions deploy (promotion gate)"
            )
        image = (sv.software_image_files.filter(default_image=True).first()
                 or sv.software_image_files.first())
        _require(image, f"SoftwareImageFile on version {sv.version}")

        vcpus = _require(device.cf.get("vcpus"), f"vcpus CF on {device.name}")
        memory_mb = _require(device.cf.get("memory_mb"), f"memory_mb CF on {device.name}")
        disk_gb = _require(device.cf.get("disk_gb"), f"disk_gb CF on {device.name}")

        hyp = self._resolve_hypervisor(device)
        api_host = _require(hyp.primary_ip4, f"primary_ip4 on hypervisor {hyp.name}")
        node = hyp.name
        bridge = _require(hyp.cf.get("vm_bridge"), f"vm_bridge CF on hypervisor {hyp.name}")
        vm_storage = _require(hyp.cf.get("vm_storage"), f"vm_storage CF on hypervisor {hyp.name}")
        import_storage = _require(hyp.cf.get("import_storage"), f"import_storage CF on hypervisor {hyp.name}")

        nics, ipconfig0 = self._render_nics(device, facts, bridge)
        self.logger.info(
            "Contract satisfied: %s on %s (%s) — image %s, %sc/%sMB/%sGB, machine %s, %s NIC(s), %s",
            device.name, node, api_host.address.ip, image.image_file_name,
            vcpus, memory_mb, disk_gb, machine, len(nics), ipconfig0,
        )

        token_id, token_secret = resolve_proxmox_credentials(hyp)
        client = ProxmoxClient(host=str(api_host.address.ip), token_id=token_id, token_secret=token_secret)

        # Idempotency / collision checks against reality
        existing = [v for v in client.list_vms(node) if v.get("name") == device.name]
        if existing:
            raise ContractViolation(
                f"A VM named {device.name!r} already exists on {node} (vmid "
                f"{existing[0]['vmid']}) while the device is Planned — reconcile first"
            )

        volid = client.ensure_image(
            node, import_storage, image.image_file_name,
            url=image.download_url, checksum=image.image_file_checksum,
            checksum_algorithm=image.hashing_algorithm or "sha256",
            logger=self.logger,
        )

        vmid = client.next_vmid()
        params = {
            "vmid": vmid, "name": device.name,
            "memory": int(memory_mb), "balloon": 0,
            "sockets": 1, "cores": int(vcpus), "cpu": "host",
            "machine": machine, "scsihw": facts["scsihw"],
            "scsi0": f"{vm_storage}:0,import-from={volid}",
            "ide2": f"{vm_storage}:cloudinit",
            "agent": 1 if facts["guest_agent"] else 0,
            "onboot": 1, "ostype": facts["ostype"], "boot": "order=scsi0",
            "tags": "nfv;sot-driven",
        }
        if facts["serial_console"]:
            params["serial0"] = "socket"
        params.update(nics)
        self.logger.info("Creating VM %s (%s) on %s", vmid, device.name, node)
        client.create_vm(node, params)

        try:
            client.resize_disk(node, vmid, "scsi0", f"{int(disk_gb)}G")
        except ProxmoxError as exc:
            self.logger.warning("Disk resize skipped: %s", exc)

        ci = {"ipconfig0": ipconfig0}
        if day0 == "native-cloudinit":
            # Console login: username from the platform CF (ciuser overrides
            # only the NAME — the template's baked default_user groups/sudo
            # still apply), password from the fleet Secret.
            console_user = _require(platform.cf.get("console_user"), f"console_user on platform {platform.name}")
            try:
                console_password = Secret.objects.get(name=CONSOLE_PASSWORD_SECRET).get_value()
            except Secret.DoesNotExist:
                raise ContractViolation(
                    f"Cloud-init platform needs the fleet console password — Secret "
                    f"{CONSOLE_PASSWORD_SECRET!r} not found"
                )
            ci["ciuser"] = console_user
            ci["cipassword"] = console_password  # never logged
            self.logger.info("Console login: user %r, password from Secret %r", console_user, CONSOLE_PASSWORD_SECRET)
        if ssh_pubkeys:
            ci["sshkeys"] = ProxmoxClient.encode_sshkeys(str(ssh_pubkeys))
        client.set_vm_config(node, vmid, ci)
        client.start_vm(node, vmid)
        self.logger.info("VM %s started.", vmid)

        # Write-back: SoT reflects the deployment (vmid + lifecycle status)
        device._custom_field_data["vmid"] = vmid
        device.status = Status.objects.get(name="Active")
        device.validated_save()
        self.logger.info("SoT updated: %s -> Active, vmid=%s", device.name, vmid)

        if wait_for_agent and facts["guest_agent"]:
            ip = client.wait_agent_ipv4(node, vmid, timeout=900)
            if ip:
                self.logger.info("Guest agent up — %s reports IPv4 %s", device.name, ip)
                return f"Deployed {device.name} (vmid {vmid}) on {node} — IP {ip}"
            self.logger.warning("Agent did not report an IPv4 within 15 minutes — check console.")
        return f"Deployed {device.name} (vmid {vmid}) on {node}"


register_jobs(DeployVnfDevice)
