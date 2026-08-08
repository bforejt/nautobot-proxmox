"""
Nautobot Job: pre-stage a SoftwareImageFile onto a Proxmox node.

Thin wrapper over the same idempotent ensure_image() the deploy job uses:
checksum-verified `download-url` pull from the firmware server into the
node's import storage. Useful for warming a node (or a whole site's pair)
ahead of a maintenance window so deploys are copy-fast.
"""

from nautobot.apps.jobs import Job, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import SoftwareImageFile
from nautobot.extras.models import Secret

from ..lib.proxmox_client import ProxmoxClient

PROXMOX_TOKEN_ID_SECRET = "proxmox_token_id"
PROXMOX_TOKEN_SECRET_SECRET = "proxmox_token_secret"


class IngestImage(Job):
    class Meta:
        name = "Ingest Image onto Proxmox Node"
        description = (
            "Checksum-verified pull of a SoftwareImageFile from the firmware server "
            "onto a node's import storage. Idempotent — no-op if already present."
        )
        has_sensitive_variables = False
        soft_time_limit = 1500
        time_limit = 1800

    image_file = ObjectVar(model=SoftwareImageFile, label="Software Image File")
    node_api_host = StringVar(label="Proxmox API Host", default="10.40.3.253")
    node_name = StringVar(label="Node Name", default="pve")
    import_storage = StringVar(label="Import Storage", default="local")

    def run(self, image_file, node_api_host, node_name, import_storage):
        token_id = Secret.objects.get(name=PROXMOX_TOKEN_ID_SECRET).get_value()
        token_secret = Secret.objects.get(name=PROXMOX_TOKEN_SECRET_SECRET).get_value()
        client = ProxmoxClient(host=str(node_api_host), token_id=token_id, token_secret=token_secret)
        volid = client.ensure_image(
            str(node_name), str(import_storage), image_file.image_file_name,
            url=image_file.download_url, checksum=image_file.image_file_checksum,
            checksum_algorithm=image_file.hashing_algorithm or "sha256",
            logger=self.logger,
        )
        return f"Image available on {node_name}: {volid}"


register_jobs(IngestImage)
