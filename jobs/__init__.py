"""Nautobot jobs for the Proxmox NFV lifecycle project.

Importing the submodules here is what triggers their register_jobs() calls
when Nautobot syncs this repo as a Git Repository providing "jobs".
"""

from .baremetal import (  # noqa: F401
    apply_storage_layout,
    discover_platform,
    install_node,
    prepare_media,
    verify_host,
)
from .design import bootstrap_schema, register_image  # noqa: F401
from .proxmox import decommission_device, deploy_device, ingest_image  # noqa: F401
