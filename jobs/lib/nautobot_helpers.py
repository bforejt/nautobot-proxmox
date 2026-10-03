"""
Shared Nautobot-side resolution helpers for the deploy/decommission/ingest jobs.

Kept out of proxmox_client.py (which stays Nautobot-free); this module is the
Nautobot-coupled glue. Centralizes credential resolution so every job resolves
a hypervisor's Proxmox API token the same way.
"""

import os

from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import (
    RelationshipAssociation,
    Secret,
    SecretsGroup,
    SecretsGroupAssociation,
)

from .host_baseline import node_token_names

# Single-host / quickstart fallback: one global token pair (the current lab
# pattern). Multi-host environments (a real pair) set a per-hypervisor
# SecretsGroup instead — see resolve_proxmox_credentials.
GLOBAL_TOKEN_ID_SECRET = "proxmox_token_id"
GLOBAL_TOKEN_SECRET_SECRET = "proxmox_token_secret"

# Fleet BMC login (Lenovo XCC/XCC2), one pair for every physical node.
XCC_USERNAME_SECRET_NAME = "xcc_username"
XCC_PASSWORD_SECRET_NAME = "xcc_password"


class CredentialError(RuntimeError):
    pass


def resolve_hypervisor(device):
    """Return the hypervisor Device hosting `device` via the Hosted On relationship."""
    assoc = RelationshipAssociation.objects.filter(
        relationship__key="hosted_on", destination_id=device.id
    ).first()
    if assoc is None:
        raise CredentialError(
            f"{device.name} has no 'Hosted On' relationship to a hypervisor"
        )
    return assoc.source


def resolve_proxmox_credentials(hypervisor):
    """Return (token_id, token_secret) for a hypervisor Device.

    Per-host **SecretsGroup** if the hypervisor's `secrets_group` custom field
    names one (the correct multi-host model — each standalone node has its own
    token); otherwise the **global Secret pair** (zero-config single-host
    quickstart). SecretsGroup layout: Generic/Username = token id
    (user@realm!name), Generic/Secret = the token UUID.
    """
    group_name = hypervisor.cf.get("secrets_group")
    if group_name:
        try:
            group = SecretsGroup.objects.get(name=group_name)
        except SecretsGroup.DoesNotExist:
            raise CredentialError(
                f"Hypervisor {hypervisor.name} references SecretsGroup "
                f"{group_name!r}, which does not exist"
            )
        token_id = group.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            SecretsGroupSecretTypeChoices.TYPE_USERNAME,
            obj=hypervisor,
        )
        token_secret = group.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            SecretsGroupSecretTypeChoices.TYPE_SECRET,
            obj=hypervisor,
        )
        return token_id, token_secret

    try:
        token_id = Secret.objects.get(name=GLOBAL_TOKEN_ID_SECRET).get_value()
        token_secret = Secret.objects.get(name=GLOBAL_TOKEN_SECRET_SECRET).get_value()
    except Secret.DoesNotExist:
        raise CredentialError(
            f"No per-host SecretsGroup on {hypervisor.name} and no global "
            f"Secrets ({GLOBAL_TOKEN_ID_SECRET!r}/{GLOBAL_TOKEN_SECRET_SECRET!r}) — "
            "configure one (getting-started.md)"
        )
    return token_id, token_secret


def resolve_bmc(device):
    """(bmc_ip, username, password) for a physical Device: the IP on its `xcc`
    interface (contract §4) plus the fleet XCC credential Secrets."""
    xcc_iface = device.interfaces.filter(name="xcc").first()
    if xcc_iface is None or not xcc_iface.ip_addresses.exists():
        raise CredentialError(
            f"{device.name} has no 'xcc' interface with an IP (contract §4 BMC address)"
        )
    try:
        username = Secret.objects.get(name=XCC_USERNAME_SECRET_NAME).get_value()
        password = Secret.objects.get(name=XCC_PASSWORD_SECRET_NAME).get_value()
    except Secret.DoesNotExist as exc:
        raise CredentialError(
            f"XCC credential Secrets missing (need {XCC_USERNAME_SECRET_NAME!r} "
            f"and {XCC_PASSWORD_SECRET_NAME!r}): {exc}"
        )
    return str(xcc_iface.ip_addresses.first().address.ip), username, password


# Where node API tokens live as text-file Secrets, as the Nautobot containers
# see them. The answer service writes the firstboot deploy token there
# (<slug>_proxmox_token_*); the Host Baseline job writes the service-account
# tokens beside them — composer mounts this directory read-write into the
# Celery worker only (nautobot-composer: host-baseline-support).
NODE_SECRETS_DIR = os.environ.get("NFV_NODE_SECRETS_DIR", "/opt/nautobot/secrets/nodes")


def _write_secret_file(path, content):
    """Atomic, mode 0640 from the first byte (the text-file provider reads it
    as the worker's own uid) — a reader never sees a torn token."""
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def store_node_token(device, account, token_id, token_value, secrets_dir=NODE_SECRETS_DIR):
    """Store a node's API token the way the answer service's firstboot
    phone-home stores the deploy token (bmc/answer_service/app.py
    _firstboot_credentials_impl): two text-file Secrets over files in the
    shared node-secrets directory, joined in the SecretsGroup <node>-<account>
    as Generic/username (token id) and Generic/secret (value). Names come from
    host_baseline.node_token_names() — the same scheme, account "proxmox"
    there. Returns the group name. The value is never logged."""
    names = node_token_names(device.name, account)
    files = {
        "username": os.path.join(secrets_dir, names["file_id"]),
        "secret": os.path.join(secrets_dir, names["file_secret"]),
    }
    _write_secret_file(files["username"], token_id + "\n")
    _write_secret_file(files["secret"], token_value + "\n")
    secrets = {}
    for kind, secret_name in (("username", names["secret_username"]), ("secret", names["secret_secret"])):
        secret = Secret.objects.filter(name=secret_name).first() or Secret(name=secret_name)
        secret.provider = "text-file"
        secret.parameters = {"path": files[kind]}
        secret.validated_save()  # saved on every store: last_updated marks the rotation
        secrets[kind] = secret
    group = SecretsGroup.objects.filter(name=names["group"]).first()
    if group is None:
        group = SecretsGroup(name=names["group"])
        group.validated_save()
    for kind, secret in secrets.items():
        assoc = SecretsGroupAssociation.objects.filter(
            secrets_group=group,
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=kind,
        ).first()
        if assoc is None:
            assoc = SecretsGroupAssociation(
                secrets_group=group,
                access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
                secret_type=kind,
                secret=secret,
            )
        else:
            assoc.secret = secret
        assoc.validated_save()
    return names["group"]


def stored_node_token(device, account):
    """(token_id, value) from the SecretsGroup <node>-<account>, or (None, None)
    when the group, a slot, or a readable value is missing."""
    group = SecretsGroup.objects.filter(name=node_token_names(device.name, account)["group"]).first()
    if group is None:
        return None, None
    try:
        token_id = group.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            SecretsGroupSecretTypeChoices.TYPE_USERNAME,
            obj=device,
        )
        value = group.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            SecretsGroupSecretTypeChoices.TYPE_SECRET,
            obj=device,
        )
    except Exception:  # missing association, missing file, provider error
        return None, None
    return (token_id or None), (value or None)
