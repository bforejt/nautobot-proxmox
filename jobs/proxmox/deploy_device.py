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
                       (native-cloudinit for Linux-class guests; pa-bootstrap
                       renders the VM-Series init-cfg/bootstrap.xml ISO and
                       attaches it as the day-0 CD — no cloud-init involved)
  NICs              <- device Interfaces in the platform's nic_order/pattern
                       (facts), pinned MACs required, VLANs -> tag=/trunks=;
                       position 0 lands on the hypervisor's mgmt_bridge CF
                       when set (two-bridge hosts), else vm_bridge
  addressing        <- interface IP => static + gateway from the DefaultGW-role
                       IP in the enclosing Prefix; no IP => DHCP; PA static
                       additionally reads DNS-role IPs from the mgmt prefix

Fail-closed: any missing contract datum is a precise refusal before anything
is touched. Write-back on success: CF vmid, status Planned -> Active.
"""

import os
import re
import socket
import tempfile
import time

from nautobot.apps.jobs import BooleanVar, Job, ObjectVar, TextVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import RelationshipAssociation, Secret, Status
from nautobot.extras.secrets.exceptions import SecretError, SecretValueNotFoundError
from nautobot.ipam.models import IPAddress

from ..lib.nautobot_helpers import resolve_proxmox_credentials
from ..lib.pa_bootstrap import (
    PaBootstrapError,
    build_bootstrap_iso,
    md5crypt,
    netmask_from_prefixlen,
    render_bootstrap_xml,
    render_init_cfg,
)
from ..lib.platform_facts import get_platform_facts, resolve_nic_order
from ..lib.proxmox_client import ProxmoxClient, ProxmoxError

# Fleet-wide console password for cloud-init guests (users log in at the
# desktop/console, never SSH). Proxmox hashes it before storing; the plaintext
# only transits the TLS API call. Rotation = update this Secret + a converge
# job that re-pushes cipassword (applies on next boot).
CONSOLE_PASSWORD_SECRET = "jumphost_console_password"

# PA-VM day-0 credential/licensing Secrets (records pre-created by bootstrap).
PA_ADMIN_PASSWORD_SECRET = "pa_admin_password"      # required for pa-bootstrap
PA_AUTHCODE_SECRET = "pa_authcode"                  # optional (BYOL)
SCM_PIN_ID_SECRET = "scm_registration_pin_id"       # required for pa_mgmt_mode=scm
SCM_PIN_VALUE_SECRET = "scm_registration_pin_value"

SUPPORTED_DAY0 = ("native-cloudinit", "pa-bootstrap")

_SIZE_RE = re.compile(r"size=(\d+)([MGT])")


def _disk_size_gb(drive_string):
    """Parse the size from a Proxmox drive config string, in GB (or None)."""
    m = _SIZE_RE.search(drive_string or "")
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    return {"M": value / 1024, "G": value, "T": value * 1024}[unit]


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
        # Budget covers a first-deploy image pull (multi-GB, up to 1800s) plus
        # upload/create/import (a 60G import-from copy takes minutes) plus an
        # appliance boot/readiness window (PA bootstrap + autocommit runs
        # 5-15 min). The readiness wait additionally self-limits to the
        # remaining soft budget. Pre-warm nodes with Ingest Image ahead of
        # windows.
        soft_time_limit = 5400
        time_limit = 6000

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
    wait_for_agent = BooleanVar(
        label="Wait for readiness",
        default=True,
        description=(
            "Cloud-init platforms: wait for the guest agent to report an IP. "
            "PA platforms: wait for mgmt HTTPS reachability — this wait also "
            "detaches and DELETES the credential-carrying bootstrap CD; skipping "
            "it leaves the CD attached (decommission sweeps it)."
        ),
    )

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

    def _render_nics(self, device, facts, bridge, mgmt_bridge=None):
        """Interfaces -> ordered Proxmox netN strings, the ipconfig for net0,
        and the net0 interface's IPAddress (the mgmt IP — PA readiness/init-cfg
        consume it). Position 0 lands on mgmt_bridge when the hypervisor sets
        it (two-bridge hosts, decision #20); everything else on vm_bridge."""
        nics, ipconfig0, mgmt_ip = {}, None, None
        names = [i.name for i in device.interfaces.all()]
        try:
            order = resolve_nic_order(device.platform.name, facts, names)
        except ValueError as exc:
            raise ContractViolation(f"Contract violation: {exc}")
        for index, if_name in enumerate(order):
            iface = device.interfaces.filter(name=if_name).first()
            if iface is None:
                raise ContractViolation(
                    f"Contract violation: interface {if_name!r} (position {index}) missing on {device.name}"
                )
            mac = _require(iface.mac_address, f"MAC address on {device.name}:{if_name}")
            # mgmt_bridge applies only to platforms with a DEDICATED mgmt NIC
            # (pattern platforms): a single-NIC jump host's eth0 is its access
            # NIC and belongs on vm_bridge even on a two-bridge host.
            dedicated_mgmt = "nic_pattern" in facts
            net_bridge = (mgmt_bridge or bridge) if (index == 0 and dedicated_mgmt and mgmt_bridge) else bridge
            net = f"{facts['nic_model']}={mac},bridge={net_bridge}"
            if iface.untagged_vlan:
                net += f",tag={iface.untagged_vlan.vid}"
            tagged = list(iface.tagged_vlans.all())
            if tagged:
                net += ",trunks=" + ";".join(str(v.vid) for v in tagged)
            nics[f"net{index}"] = net

            if index == 0:
                # Prefer primary_ip4 when it lives on this interface (a mgmt
                # interface with a secondary IP must not pick the wrong one by
                # DB ordering); multiple IPs with no primary among them is
                # ambiguous -> refuse.
                ips = list(iface.ip_addresses.all())
                primary = device.primary_ip4
                if primary is not None and any(p.pk == primary.pk for p in ips):
                    ip = primary
                elif len(ips) > 1:
                    raise ContractViolation(
                        f"Contract violation: {device.name}:{if_name} carries {len(ips)} IPs "
                        "and none is the device's primary_ip4 — ambiguous mgmt address"
                    )
                else:
                    ip = ips[0] if ips else None
                mgmt_ip = ip
                if ip is not None:
                    ipconfig0 = f"ip={ip.address},gw={self._gateway_for(ip)}"
                else:
                    ipconfig0 = "ip=dhcp"
        return nics, ipconfig0, mgmt_ip

    # ---------- pa-bootstrap day-0 ----------

    def _secret_value(self, name, purpose, required=True):
        """Resolve a Secret's value. An existing-but-EMPTY value counts as
        missing (a blank pa_admin_password would otherwise mint a valid phash
        of the empty string — the exact failure the gate exists to prevent)."""
        value = None
        try:
            value = Secret.objects.get(name=name).get_value()
        except (Secret.DoesNotExist, SecretValueNotFoundError):
            if required:
                raise ContractViolation(
                    f"{purpose} — Secret {name!r} missing or valueless "
                    f"(bootstrap creates the record; supply the value, e.g. ./add-secret.sh {name})"
                )
            self.logger.info("Optional Secret %r has no value — skipping (%s)", name, purpose)
            return None
        except SecretError as exc:  # provider error: record exists, read failed
            if required:
                raise ContractViolation(f"{purpose} — Secret {name!r} is unreadable: {exc}")
            self.logger.warning("Optional Secret %r is unreadable — proceeding without (%s): %s", name, purpose, exc)
            return None
        if value is None or not str(value).strip():
            if required:
                raise ContractViolation(
                    f"{purpose} — Secret {name!r} resolved to an EMPTY value "
                    f"(supply a real value, e.g. ./add-secret.sh {name})"
                )
            self.logger.info("Optional Secret %r is empty — skipping (%s)", name, purpose)
            return None
        return str(value)

    def _pa_render_payload(self, device, mgmt_ip):
        """SoT -> the VM-Series bootstrap package texts (init-cfg.txt,
        bootstrap.xml, optional authcodes). Fail-closed contract reads."""
        mode = (device.cf.get("pa_mgmt_mode") or "standalone").strip().lower()
        if mode not in ("standalone", "scm"):
            raise ContractViolation(
                f"pa_mgmt_mode on {device.name} is {mode!r} — must be standalone or scm"
            )
        # The mgmt interface's IP is the addressing authority; a divergent
        # primary_ip4 is a data bug, not a tiebreak (contract §3).
        if device.primary_ip4 is not None:
            if mgmt_ip is None or str(device.primary_ip4.address) != str(mgmt_ip.address):
                raise ContractViolation(
                    f"{device.name}: primary_ip4 ({device.primary_ip4.address}) does not match "
                    f"the mgmt interface IP ({mgmt_ip.address if mgmt_ip else 'none'}) — "
                    "the mgmt interface IP is the authority; reconcile them"
                )

        if mgmt_ip is not None and mgmt_ip.address.version != 4:
            raise ContractViolation(
                f"{device.name}: mgmt IP {mgmt_ip.address} is IPv6 — the pa-bootstrap "
                "builder renders IPv4 init-cfg only (IPv6 mgmt is out of contract today)"
            )

        scm_pin_id = scm_pin_value = None
        if mode == "scm":
            scm_pin_id = self._secret_value(SCM_PIN_ID_SECRET, "SCM registration")
            scm_pin_value = self._secret_value(SCM_PIN_VALUE_SECRET, "SCM registration")

        try:
            if mgmt_ip is not None:
                prefix = mgmt_ip.parent
                # Lowest address = dns-primary (contract §3) — explicit ordering
                # so the documented rule is the enforced one.
                dns = [
                    str(ip.address.ip)
                    for ip in IPAddress.objects.filter(parent=prefix, role__name="DNS").order_by("host")[:2]
                ]
                init_cfg = render_init_cfg(
                    device.name, dhcp=False,
                    ip=str(mgmt_ip.address.ip),
                    netmask=netmask_from_prefixlen(mgmt_ip.address.prefixlen),
                    gateway=self._gateway_for(mgmt_ip), dns=dns,
                    scm_pin_id=scm_pin_id, scm_pin_value=scm_pin_value,
                )
            else:
                init_cfg = render_init_cfg(
                    device.name, dhcp=True,
                    scm_pin_id=scm_pin_id, scm_pin_value=scm_pin_value,
                )
        except PaBootstrapError as exc:
            raise ContractViolation(f"Contract violation: {exc} (sot-data-contract.md §3/§4a)")

        # Admin password ships as an md5-crypt phash in a minimal bootstrap.xml
        # — never plaintext, and never admin/admin on the wire. The config
        # version tracks the image being deployed (a config newer than the
        # running PAN-OS is the unsupported direction; older is fine).
        admin_password = self._secret_value(
            PA_ADMIN_PASSWORD_SECRET, "PA admin password for bootstrap.xml"
        )
        m = re.match(r"^(\d+)\.(\d+)", device.software_version.version if device.software_version else "")
        config_version = f"{m.group(1)}.{m.group(2)}.0" if m else "10.1.0"
        bootstrap_xml = render_bootstrap_xml(md5crypt(admin_password), config_version=config_version)
        authcodes = self._secret_value(
            PA_AUTHCODE_SECRET, "BYOL authcode (unlicensed boot without it)", required=False
        )
        self.logger.info(
            "PA day-0: mode=%s, mgmt=%s, dns=%s, authcode=%s",
            mode, "static" if mgmt_ip is not None else "dhcp",
            "yes" if mgmt_ip is not None else "from-dhcp",
            "yes" if authcodes else "no (unlicensed)",
        )
        return {"init_cfg": init_cfg, "bootstrap_xml": bootstrap_xml, "authcodes": authcodes}

    def _wait_tcp(self, host, port, deadline_s, poll=15):
        """Wall-clock-bounded TCP probe (connect timeouts count against the
        deadline — a fixed-iteration loop would overrun it by ~35%)."""
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            try:
                with socket.create_connection((host, port), timeout=5):
                    return True
            except OSError:
                pass
            time.sleep(min(poll, max(0, end - time.monotonic())))
        return False

    # ---------- run ----------

    def run(self, device, ssh_pubkeys, wait_for_agent):
        t0 = time.monotonic()  # readiness waits self-limit to the soft budget
        if device.status.name != "Planned":
            raise ContractViolation(
                f"{device.name} is {device.status.name}, not Planned — "
                "redeploys/changes are separate jobs (SoT-first)"
            )

        platform = _require(device.platform, f"platform on {device.name}")
        facts = get_platform_facts(platform.name)
        day0 = _require(platform.cf.get("day0_builder"), f"day0_builder on platform {platform.name}")
        machine = _require(platform.cf.get("machine_type"), f"machine_type on platform {platform.name}")
        if day0 not in SUPPORTED_DAY0:
            raise ContractViolation(
                f"Platform {platform.name} binds day0_builder={day0!r}, which this code "
                f"version does not ship (supported: {', '.join(SUPPORTED_DAY0)})"
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

        mgmt_bridge = hyp.cf.get("mgmt_bridge") or None
        nics, ipconfig0, mgmt_ip = self._render_nics(device, facts, bridge, mgmt_bridge)
        self.logger.info(
            "Contract satisfied: %s on %s (%s) — image %s, %sc/%sMB/%sGB, machine %s, %s NIC(s), %s",
            device.name, node, api_host.address.ip, image.image_file_name,
            vcpus, memory_mb, disk_gb, machine, len(nics), ipconfig0,
        )

        # PA day-0 payload renders BEFORE anything touches the node — a
        # contract/secret refusal must cost nothing (no image pull first).
        pa_payload = self._pa_render_payload(device, mgmt_ip) if day0 == "pa-bootstrap" else None

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
            "agent": 1 if facts["guest_agent"] else 0,
            "onboot": 1, "ostype": facts["ostype"], "boot": "order=scsi0",
            "tags": "nfv;sot-driven",
        }
        if facts["serial_console"]:
            params["serial0"] = "socket"
        if facts.get("pin_smbios_uuid"):
            # Stable VM UUID across redeploys (device PK): PA's serial derives
            # from UUID+CPUID — a fresh UUID per redeploy strands the license.
            params["smbios1"] = f"uuid={device.id}"
        params.update(nics)

        iso_volid = iso_storage = None
        if day0 == "native-cloudinit":
            params["ide2"] = f"{vm_storage}:cloudinit"
        else:  # pa-bootstrap: the day-0 CD replaces the cloudinit drive
            iso_storage = import_storage
            entry = next((s for s in client.storages(node) if s.get("storage") == iso_storage), None)
            if entry is None or "iso" not in (entry.get("content") or ""):
                raise ContractViolation(
                    f"Storage {iso_storage!r} on {node} does not allow ISO content — "
                    "the bootstrap CD lives on the hypervisor's import_storage, which "
                    "must enable both Import and ISO content types"
                )
            iso_name = f"{device.name}-bootstrap.iso"
            stale = client.find_iso_volume(node, iso_storage, iso_name)
            if stale:
                # Always regenerate: a leftover ISO from a failed run carries
                # stale addressing/credentials.
                try:
                    client.delete_volume(node, iso_storage, stale)
                    self.logger.info("Deleted stale bootstrap ISO %s", stale)
                except ProxmoxError as exc:
                    self.logger.warning("Could not delete stale %s (will overwrite): %s", stale, exc)
            with tempfile.TemporaryDirectory() as tmp:  # scrubbed on exit — the ISO carries credentials
                iso_path = os.path.join(tmp, iso_name)
                build_bootstrap_iso(
                    iso_path, pa_payload["init_cfg"],
                    bootstrap_xml=pa_payload["bootstrap_xml"],
                    authcodes=pa_payload["authcodes"],
                )
                iso_volid = client.upload_file(node, iso_storage, iso_path, content="iso", filename=iso_name)
            self.logger.info("Bootstrap ISO uploaded: %s", iso_volid)
            params["ide2"] = f"{iso_volid},media=cdrom"

        self.logger.info("Creating VM %s (%s) on %s", vmid, device.name, node)
        try:
            client.create_vm(node, params)

            # Resize only when growing: appliance images already carry their
            # full virtual size (PA-VM = 60G) and shrink attempts just error.
            current_gb = _disk_size_gb(client.vm_config(node, vmid).get("scsi0", ""))
            if current_gb is None or int(disk_gb) > current_gb:
                try:
                    client.resize_disk(node, vmid, "scsi0", f"{int(disk_gb)}G")
                except ProxmoxError as exc:
                    self.logger.warning("Disk resize skipped: %s", exc)
            elif int(disk_gb) < current_gb:
                self.logger.warning(
                    "disk_gb=%s is smaller than the image's %sG virtual size — not shrinking "
                    "(set disk_gb to match the image)", disk_gb, current_gb,
                )

            if day0 == "native-cloudinit":
                ci = {"ipconfig0": ipconfig0}
                # Console login: username from the platform CF (ciuser overrides
                # only the NAME — the template's baked default_user groups/sudo
                # still apply), password from the fleet Secret.
                console_user = _require(platform.cf.get("console_user"), f"console_user on platform {platform.name}")
                ci["ciuser"] = console_user
                ci["cipassword"] = self._secret_value(  # never logged
                    CONSOLE_PASSWORD_SECRET, "Cloud-init platform needs the fleet console password"
                )
                self.logger.info("Console login: user %r, password from Secret %r", console_user, CONSOLE_PASSWORD_SECRET)
                if ssh_pubkeys:
                    ci["sshkeys"] = ProxmoxClient.encode_sshkeys(str(ssh_pubkeys))
                client.set_vm_config(node, vmid, ci)
            # pa-bootstrap: NO ci block at all — ipconfig/ciuser/sshkeys are
            # cloud-init semantics and PAN-OS reads none of them.
            client.start_vm(node, vmid)
            self.logger.info("VM %s started.", vmid)
        except Exception:
            # Roll back this run's node-side artifacts so a retry starts clean
            # (the device is still Planned; a half-created VM would trip the
            # name-collision refusal and force manual reconciliation).
            try:
                if any(v.get("vmid") == vmid for v in client.list_vms(node)):
                    client.stop_vm(node, vmid)
                    client.destroy_vm(node, vmid)
                    self.logger.warning("Rolled back half-created VM %s after failure", vmid)
            except ProxmoxError as exc:
                self.logger.warning("Could not roll back VM %s — reconcile manually: %s", vmid, exc)
            if iso_volid:
                try:
                    client.delete_volume(node, iso_storage, iso_volid)
                    self.logger.info("Cleaned up bootstrap ISO after failure")
                except ProxmoxError as exc:
                    self.logger.warning("Bootstrap ISO cleanup failed — delete %s manually: %s", iso_volid, exc)
            raise

        # Write-back: SoT reflects the deployment (vmid + lifecycle status)
        device._custom_field_data["vmid"] = vmid
        device.status = Status.objects.get(name="Active")
        device.validated_save()
        self.logger.info("SoT updated: %s -> Active, vmid=%s", device.name, vmid)

        readiness = facts.get("readiness", "guest-agent")
        if not wait_for_agent and day0 == "pa-bootstrap":
            self.logger.warning(
                "Readiness wait skipped: the credential-carrying bootstrap ISO stays "
                "attached — detach ide2 and delete %s manually once the firewall is up "
                "(decommission also sweeps it).", iso_volid,
            )
        if wait_for_agent and readiness == "guest-agent" and facts["guest_agent"]:
            ip = client.wait_agent_ipv4(node, vmid, timeout=900)
            if ip:
                self.logger.info("Guest agent up — %s reports IPv4 %s", device.name, ip)
                return f"Deployed {device.name} (vmid {vmid}) on {node} — IP {ip}"
            self.logger.warning("Agent did not report an IPv4 within 15 minutes — check console.")
        elif wait_for_agent and readiness == "tcp-mgmt":
            if mgmt_ip is None:
                self.logger.warning(
                    "DHCP mgmt: no address to probe — deployment is UNVERIFIABLE from here "
                    "(check the serial console). The bootstrap ISO stays attached; detach and "
                    "delete %s once the firewall is up.", iso_volid,
                )
            else:
                probe_ip = str(mgmt_ip.address.ip)
                # Fit the probe inside the remaining soft budget (keep 120s
                # slack for the detach/delete + write-out).
                remaining = int(self.Meta.soft_time_limit - (time.monotonic() - t0)) - 120
                deadline = max(0, min(1500, remaining))
                self.logger.info(
                    "Waiting up to %ss for mgmt HTTPS on %s:443 (bootstrap + autocommit runs "
                    "5-15 min; this proves 'mgmt reachable', NOT chassis-ready — requires "
                    "worker->mgmt routing)", deadline, probe_ip,
                )
                if deadline and self._wait_tcp(probe_ip, 443, deadline_s=deadline):
                    self.logger.info("Mgmt HTTPS answering on %s — detaching the bootstrap CD", probe_ip)
                    try:
                        client.set_vm_config(node, vmid, {"ide2": "none,media=cdrom"})
                        client.delete_volume(node, iso_storage, iso_volid)
                        self.logger.info("Bootstrap ISO detached and deleted (it carried credentials)")
                    except ProxmoxError as exc:
                        self.logger.warning(
                            "Bootstrap ISO cleanup failed — detach ide2 and delete %s manually "
                            "(needs Datastore.Allocate; see getting-started §4): %s", iso_volid, exc,
                        )
                    return f"Deployed {device.name} (vmid {vmid}) on {node} — mgmt reachable at {probe_ip}"
                self.logger.warning(
                    "Mgmt HTTPS not answering within 25 min — check the serial console. The "
                    "bootstrap ISO stays attached (it may still be mid-bootstrap); decommission "
                    "sweeps it, or delete %s manually once the firewall is up.", iso_volid,
                )
        return f"Deployed {device.name} (vmid {vmid}) on {node}"


register_jobs(DeployVnfDevice)
