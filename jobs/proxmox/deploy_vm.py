"""
Nautobot Job: deploy a VM from an Active SoftwareVersion (cloud-init class).

The Phase 2a engine core: resolves the version's default SoftwareImageFile,
ensures the image is present on the target node (checksum-verified pull from
the firmware server if absent), creates the VM via `import-from` (the only
token-compatible import mechanism), applies identity via native cloud-init,
starts it, and waits for the guest agent to report an IP.

Pre-flights (the legacy robot's sanity checks, reborn):
  - target node exists and storages are present with capacity
  - VM name collision refusal
  - oversubscription check (v1: WARNING only, thread-counted; the SE350
    policy — physical cores minus host reserve — arrives with config-context
    profiles in the site-design phase)

Credentials come from Nautobot Secrets `proxmox_token_id` /
`proxmox_token_secret` (this environment: text-file provider reading
/opt/nautobot/secrets/<name>, created via composer's add-secret.sh).

VNF profiles and the day-0 ISO builders extend this engine in Phase 2b/2c —
this class covers the cloud-init image class (Ubuntu jump host).
"""

from nautobot.apps.jobs import (
    BooleanVar,
    IntegerVar,
    Job,
    ObjectVar,
    StringVar,
    TextVar,
    register_jobs,
)
from nautobot.dcim.models import SoftwareVersion
from nautobot.extras.models import Secret

from ..lib.proxmox_client import ProxmoxClient, ProxmoxError

PROXMOX_TOKEN_ID_SECRET = "proxmox_token_id"
PROXMOX_TOKEN_SECRET_SECRET = "proxmox_token_secret"


class DeployCloudInitVm(Job):
    class Meta:
        name = "Deploy VM from Software Version (cloud-init)"
        description = (
            "Deploys a VM on a Proxmox node from an Active SoftwareVersion's image: "
            "checksum-verified pull if absent, import-from create, native cloud-init "
            "identity, autostart, guest-agent IP verification."
        )
        has_sensitive_variables = False
        soft_time_limit = 1500
        time_limit = 1800

    software_version = ObjectVar(
        model=SoftwareVersion,
        label="Software Version",
        description="Template/image version to deploy (select an Active version)",
        query_params={"status": "Active"},
    )
    vm_name = StringVar(label="VM Name", description="Also becomes the guest hostname (via cloud-init)")
    node_api_host = StringVar(label="Proxmox API Host", default="10.40.3.253")
    node_name = StringVar(label="Node Name", default="pve")
    vm_storage = StringVar(label="VM Disk Storage", default="local-lvm")
    import_storage = StringVar(label="Import Storage", default="local", description="Storage with 'import' content type")
    cores = IntegerVar(label="vCPU Cores", default=2)
    memory_mb = IntegerVar(label="Memory (MB)", default=4096)
    disk_gb = IntegerVar(label="Disk (GB)", default=32, description="Disk is grown to this size after import")
    bridge = StringVar(label="Bridge", default="vmbr0")
    vlan_tag = IntegerVar(label="VLAN Tag", required=False, description="Access VLAN for net0; empty = untagged")
    ipconfig = StringVar(label="ipconfig0", default="ip=dhcp", description='e.g. "ip=10.0.0.5/24,gw=10.0.0.1"')
    ssh_pubkeys = TextVar(label="SSH Public Key(s)", required=False)
    vmid = IntegerVar(label="VMID", required=False, description="Empty = next free ID")
    start_after = BooleanVar(label="Start after deploy", default=True)
    wait_for_agent = BooleanVar(
        label="Wait for guest agent IP",
        default=True,
        description="First boot of a desktop image can take 5-8 minutes",
    )

    def run(self, software_version, vm_name, node_api_host, node_name, vm_storage,
            import_storage, cores, memory_mb, disk_gb, bridge, vlan_tag, ipconfig,
            ssh_pubkeys, vmid, start_after, wait_for_agent):

        image = (software_version.software_image_files.filter(default_image=True).first()
                 or software_version.software_image_files.first())
        if not image:
            raise ValueError(f"{software_version} has no SoftwareImageFile records")
        self.logger.info(
            "Deploying %s %s from image %s (sha256 %s...)",
            software_version.platform.name, software_version.version,
            image.image_file_name, (image.image_file_checksum or "")[:12],
        )

        try:
            token_id = Secret.objects.get(name=PROXMOX_TOKEN_ID_SECRET).get_value()
            token_secret = Secret.objects.get(name=PROXMOX_TOKEN_SECRET_SECRET).get_value()
        except Secret.DoesNotExist as exc:
            self.logger.error(
                "Missing Secret: %s. Expected %r and %r.",
                exc, PROXMOX_TOKEN_ID_SECRET, PROXMOX_TOKEN_SECRET_SECRET,
            )
            raise

        client = ProxmoxClient(host=str(node_api_host), token_id=token_id, token_secret=token_secret)

        # ---- pre-flights ----
        node = str(node_name)
        status = client.node_status(node)
        threads = status["cpuinfo"]["cpus"]
        mem_total_mb = status["memory"]["total"] // (1024 * 1024)

        for store, need_gb in ((str(vm_storage), int(disk_gb)), (str(import_storage), 0)):
            entry = next((s for s in client.storages(node) if s["storage"] == store), None)
            if entry is None:
                raise ValueError(f"Storage {store!r} not found on node {node}")
            avail_gb = entry.get("avail", 0) / 2**30
            if need_gb and avail_gb < need_gb:
                raise ValueError(f"Storage {store!r} has {avail_gb:.1f} GiB free; need {need_gb}")

        vms = client.list_vms(node)
        if any(v.get("name") == vm_name for v in vms):
            raise ValueError(f"A VM named {vm_name!r} already exists on {node} — refusing (redeploy is a separate job)")

        defined = [v for v in vms if not v.get("template")]
        core_sum = sum(v.get("cpus") or 0 for v in defined) + int(cores)
        mem_sum = sum((v.get("maxmem") or 0) // (1024 * 1024) for v in defined) + int(memory_mb)
        if core_sum > threads or mem_sum > mem_total_mb:
            self.logger.warning(
                "Oversubscription: defined VMs would total %s vCPUs / %s MB against "
                "%s threads / %s MB on %s. (v1 warns; SE350 policy enforces "
                "physical-core budgets via config context later.)",
                core_sum, mem_sum, threads, mem_total_mb, node,
            )

        # ---- ensure image on node ----
        volid = client.ensure_image(
            node, str(import_storage), image.image_file_name,
            url=image.download_url, checksum=image.image_file_checksum,
            checksum_algorithm=image.hashing_algorithm or "sha256",
            logger=self.logger,
        )

        # ---- create ----
        new_vmid = int(vmid) if vmid else client.next_vmid()
        net0 = f"virtio,bridge={bridge}" + (f",tag={int(vlan_tag)}" if vlan_tag else "")
        self.logger.info("Creating VM %s (%s) on %s", new_vmid, vm_name, node)
        client.create_vm(node, {
            "vmid": new_vmid, "name": str(vm_name),
            "memory": int(memory_mb), "balloon": 0,
            "sockets": 1, "cores": int(cores), "cpu": "host",
            "net0": net0, "scsihw": "virtio-scsi-single",
            "scsi0": f"{vm_storage}:0,import-from={volid}",
            "ide2": f"{vm_storage}:cloudinit",
            "serial0": "socket", "agent": 1, "onboot": 1,
            "ostype": "l26", "boot": "order=scsi0",
            "tags": "nfv;cloudinit",
        })

        try:
            client.resize_disk(node, new_vmid, "scsi0", f"{int(disk_gb)}G")
            self.logger.info("Disk grown to %sG", disk_gb)
        except ProxmoxError as exc:
            self.logger.warning("Disk resize skipped: %s", exc)

        ci = {"ipconfig0": str(ipconfig)}
        if ssh_pubkeys:
            ci["sshkeys"] = ProxmoxClient.encode_sshkeys(str(ssh_pubkeys))
        client.set_vm_config(node, new_vmid, ci)

        if not start_after:
            self.logger.info("VM %s created (not started, per options).", new_vmid)
            return f"Created VM {new_vmid} ({vm_name}) on {node}"

        client.start_vm(node, new_vmid)
        self.logger.info("VM %s started.", new_vmid)

        if wait_for_agent:
            ip = client.wait_agent_ipv4(node, new_vmid, timeout=600)
            if ip:
                self.logger.info("Guest agent up — %s reports IPv4 %s", vm_name, ip)
                return f"Deployed VM {new_vmid} ({vm_name}) on {node} — IP {ip}"
            self.logger.warning("Guest agent did not report an IPv4 within 10 minutes — check console.")
            return f"Deployed VM {new_vmid} ({vm_name}) on {node} — agent IP pending"

        return f"Deployed VM {new_vmid} ({vm_name}) on {node}"


register_jobs(DeployCloudInitVm)
