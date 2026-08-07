# LOAD-BEARING FILE — do not delete.
#
# Nautobot imports a Git repository's checkout as a Python package named after
# the repository slug (e.g. `proxmox`), and its job discovery walks only real
# packages: without this __init__.py at the repository ROOT, discovery finds
# nothing and the sync logs "No jobs were registered on loading the
# `<slug>.jobs` submodule" even though jobs/ is present and correct.
