"""
Nautobot Job: decommission a VNF Device — the SoT-true reverse of deploy.

Input: ONE Device (status Active with a recorded vmid). The job verifies the
VM on the hypervisor matches the record (vmid AND name — never destroys a
mismatch), stops and destroys it, then writes the SoT back: vmid cleared,
status Active -> Planned. The record never lies: after this job, Nautobot
says "intended but not deployed", which is exactly true — and re-running
DeployVnfDevice recreates it (the redeploy primitive, in two halves).
"""

from nautobot.apps.jobs import Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import RelationshipAssociation, Status

from ..lib.nautobot_helpers import resolve_proxmox_credentials
from ..lib.proxmox_client import ProxmoxClient


class DecommissionVnfDevice(Job):
    class Meta:
        name = "Decommission VNF Device (SoT-driven)"
        description = (
            "Destroys the VM behind an Active VNF device after verifying vmid AND "
            "name match the record, then writes the SoT back: vmid cleared, status "
            "Active -> Planned. Re-running the deploy job recreates it."
        )
        has_sensitive_variables = False
        soft_time_limit = 600
        time_limit = 900

    device = ObjectVar(
        model=Device,
        label="VNF Device",
        description="An Active VNF device with a recorded vmid",
        query_params={"status": "Active"},
    )

    def run(self, device):
        if device.status.name != "Active":
            raise ValueError(f"{device.name} is {device.status.name}, not Active — nothing to decommission")
        vmid = device.cf.get("vmid")
        if not vmid:
            raise ValueError(f"{device.name} has no recorded vmid — SoT does not know of a deployed VM")

        assoc = RelationshipAssociation.objects.filter(
            relationship__key="hosted_on", destination_id=device.id
        ).first()
        if assoc is None:
            raise ValueError(f"{device.name} has no 'Hosted On' relationship — cannot locate its hypervisor")
        hyp = assoc.source
        if hyp.primary_ip4 is None:
            raise ValueError(f"Hypervisor {hyp.name} has no primary_ip4")

        token_id, token_secret = resolve_proxmox_credentials(hyp)
        client = ProxmoxClient(host=str(hyp.primary_ip4.address.ip), token_id=token_id, token_secret=token_secret)

        node = hyp.name
        vm = next((v for v in client.list_vms(node) if v.get("vmid") == int(vmid)), None)
        if vm is None:
            self.logger.warning(
                "VM %s not found on %s — reality already matches the intended end "
                "state; updating SoT only.", vmid, node,
            )
        else:
            if vm.get("name") != device.name:
                raise ValueError(
                    f"SAFETY REFUSAL: vmid {vmid} on {node} is named {vm.get('name')!r}, "
                    f"not {device.name!r} — record/reality mismatch, reconcile first"
                )
            if vm.get("status") == "running":
                self.logger.info("Stopping VM %s (%s)", vmid, device.name)
                client.stop_vm(node, int(vmid))
            self.logger.info("Destroying VM %s (%s) on %s", vmid, device.name, node)
            client.destroy_vm(node, int(vmid))

        device._custom_field_data["vmid"] = None
        device.status = Status.objects.get(name="Planned")
        device.validated_save()
        self.logger.info("SoT updated: %s -> Planned, vmid cleared", device.name)
        return f"Decommissioned {device.name} (was vmid {vmid}) on {node}"


register_jobs(DecommissionVnfDevice)
