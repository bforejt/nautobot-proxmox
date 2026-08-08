"""
Immutable platform FACTS — behavior of each guest OS that the deploy code
interprets. Deliberately code, not SoT data (decision #40): these change only
when the code that renders them changes, and every possible "edit" would be a
broken deploy. TUNABLES (day0 builder binding, machine pin) live on the
Platform object in Nautobot.

nic_order is the name<->netN authority (sot-data-contract.md §3): Proxmox
netN index -> PCI slot order -> guest enumeration order is deterministic, and
each OS names interfaces to that order by these fixed rules.
"""

PLATFORM_FACTS = {
    "ubuntu-jumphost": {
        "nic_order": ["eth0"],
        "nic_model": "virtio",
        "scsihw": "virtio-scsi-single",
        "serial_console": True,
        "guest_agent": True,
        "ostype": "l26",
    },
    # paloalto-panos / cisco-iosxe arrive with their day-0 builders (Phase 2b/2c):
    # PA: first NIC = mgmt, then ethernet1/1... ; IOS-XE: Gi1, Gi2, ...
}


def get_platform_facts(platform_name: str) -> dict:
    facts = PLATFORM_FACTS.get(platform_name)
    if facts is None:
        raise ValueError(
            f"No platform facts for {platform_name!r} — this code version supports: "
            f"{sorted(PLATFORM_FACTS)}"
        )
    return facts
