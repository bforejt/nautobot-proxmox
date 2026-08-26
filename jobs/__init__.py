"""Nautobot jobs for the Proxmox NFV lifecycle project.

Importing the submodules here is what triggers their register_jobs() calls
when Nautobot syncs this repo as a Git Repository providing "jobs".
"""

from .baremetal import discover_platform, install_node, prepare_media, verify_host  # noqa: F401
from .design import bootstrap_schema  # noqa: F401
from .proxmox import decommission_device, deploy_device, ingest_image  # noqa: F401
