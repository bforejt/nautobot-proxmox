"""Nautobot jobs for the Proxmox NFV lifecycle project.

Importing the submodules here is what triggers their register_jobs() calls
when Nautobot syncs this repo as a Git Repository providing "jobs".
"""

from .baremetal import discover_platform  # noqa: F401
from .proxmox import deploy_vm, ingest_image  # noqa: F401
