"""
Shared Nautobot-side resolution helpers for the deploy/decommission/ingest jobs.

Kept out of proxmox_client.py (which stays Nautobot-free); this module is the
Nautobot-coupled glue. Centralizes credential resolution so every job resolves
a hypervisor's Proxmox API token the same way.
"""

from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import RelationshipAssociation, Secret, SecretsGroup

# Single-host / quickstart fallback: one global token pair (the current lab
# pattern). Multi-host environments (a real pair) set a per-hypervisor
# SecretsGroup instead — see resolve_proxmox_credentials.
GLOBAL_TOKEN_ID_SECRET = "proxmox_token_id"
GLOBAL_TOKEN_SECRET_SECRET = "proxmox_token_secret"


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
