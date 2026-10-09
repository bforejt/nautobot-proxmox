"""
Nautobot Job: pre-stage a SoftwareImageFile onto a hypervisor's import storage.

Device-driven (SSoT-first): pick the image file + the hypervisor Device; the
node name, API host, import storage, and credentials all resolve from the SoT.
Idempotent checksum-verified `download-url` pull — no-op if already present
(matched by filename); refuses a SoftwareImageFile with no checksum.
Useful to warm a node (or a whole pair) ahead of a maintenance window.
"""

from nautobot.apps.jobs import Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device, SoftwareImageFile

from ..lib.nautobot_helpers import resolve_proxmox_credentials
from ..lib.proxmox_client import ProxmoxClient, require_image_checksum

# Nautobot groups jobs by this module attribute; every job in this repo shares one section.
name = "Proxmox Automation"


class IngestImage(Job):
    class Meta:
        name = "Ingest Image onto Proxmox Node"
        description = (
            "Checksum-verified pull of a SoftwareImageFile from its download_url onto "
            "a hypervisor's import storage. Device-driven; idempotent; refuses an "
            "image record without a checksum."
        )
        has_sensitive_variables = False
        soft_time_limit = 1500
        time_limit = 1800

    image_file = ObjectVar(model=SoftwareImageFile, label="Software Image File")
    hypervisor = ObjectVar(
        model=Device,
        label="Hypervisor",
        description="Target node (API host from primary_ip4, import storage from its CF)",
        query_params={"role": "NFV"},
    )

    def run(self, image_file, hypervisor):
        # Fail closed before any node call: a record without a checksum would
        # be pulled unverified and then reused by filename forever.
        checksum, checksum_algorithm = require_image_checksum(
            image_file.image_file_name, image_file.image_file_checksum, image_file.hashing_algorithm)
        if hypervisor.primary_ip4 is None:
            raise ValueError(f"Hypervisor {hypervisor.name} has no primary_ip4")
        import_storage = hypervisor.cf.get("import_storage")
        if not import_storage:
            raise ValueError(f"Hypervisor {hypervisor.name} has no import_storage custom field")

        token_id, token_secret = resolve_proxmox_credentials(hypervisor)
        client = ProxmoxClient(
            host=str(hypervisor.primary_ip4.address.ip), token_id=token_id, token_secret=token_secret,
            logger=self.logger,
        )
        volid = client.ensure_image(
            hypervisor.name, str(import_storage), image_file.image_file_name,
            url=image_file.download_url, checksum=checksum,
            checksum_algorithm=checksum_algorithm,
            logger=self.logger,
        )
        return f"Image available on {hypervisor.name}: {volid}"


register_jobs(IngestImage)
