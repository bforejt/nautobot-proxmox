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
        "readiness": "guest-agent",
        "pin_smbios_uuid": False,
    },
    # PA-VM: no guest agent, no cloud-init. NIC order is pattern-based because
    # the dataplane NIC count varies per design: position 0 = mgmt, positions
    # 1..N = ethernet1/1..ethernet1/N (PAN-OS assigns by PCI-ID, lowest first,
    # with NO MAC-match fallback — which is also why the deploy pins the q35
    # machine version via the Platform CF, and why smbios UUID is pinned:
    # PA serial = VM UUID + CPUID, so a redeploy must not mint a new serial).
    "paloalto-panos": {
        "nic_pattern": {"mgmt": "mgmt", "dataplane": "ethernet1/{n}"},
        "nic_model": "virtio",
        "scsihw": "virtio-scsi-single",
        "serial_console": True,
        "guest_agent": False,
        "ostype": "l26",
        "readiness": "tcp-mgmt",
        "pin_smbios_uuid": True,
    },
    # cisco-iosxe arrives with its day-0 builder (Phase 2c): Gi1, Gi2, ...
}


def get_platform_facts(platform_name: str) -> dict:
    facts = PLATFORM_FACTS.get(platform_name)
    if facts is None:
        raise ValueError(
            f"No platform facts for {platform_name!r} — this code version supports: "
            f"{sorted(PLATFORM_FACTS)}"
        )
    return facts


def resolve_nic_order(platform_name: str, facts: dict, interface_names) -> list:
    """The ordered guest NIC names for THIS device (list position = netN index).

    Fixed-list platforms return their list unchanged (legacy semantics: extra
    device interfaces are ignored). Pattern platforms are strict/fail-closed:
    the mgmt interface must exist, dataplane indices must be contiguous from
    1, and any interface matching neither the mgmt name nor the pattern is a
    refusal — a modeled NIC that would silently never reach the VM.
    """
    if "nic_order" in facts:
        return list(facts["nic_order"])

    pattern = facts["nic_pattern"]
    mgmt_name = pattern["mgmt"]
    prefix, suffix = pattern["dataplane"].split("{n}")
    names = list(interface_names)

    indices, unmatched = [], []
    for name in names:
        if name == mgmt_name:
            continue
        if name.startswith(prefix) and name.endswith(suffix):
            middle = name[len(prefix): len(name) - len(suffix)] if suffix else name[len(prefix):]
            # Canonical digits only: "01" must not silently alias index 1 —
            # the rendered name would not match the modeled interface.
            if middle.isdigit() and str(int(middle)) == middle:
                indices.append(int(middle))
                continue
        unmatched.append(name)

    if mgmt_name not in names:
        raise ValueError(
            f"{platform_name}: interface {mgmt_name!r} (position 0, the management NIC) "
            f"is missing — device has {sorted(names)}"
        )
    if unmatched:
        raise ValueError(
            f"{platform_name}: interface(s) {sorted(unmatched)} match neither "
            f"{mgmt_name!r} nor {pattern['dataplane']!r} — they would never be "
            "rendered onto the VM (fail-closed; fix the names or remove them)"
        )
    expected = list(range(1, len(indices) + 1))
    if sorted(indices) != expected:
        raise ValueError(
            f"{platform_name}: dataplane interfaces must be contiguous from "
            f"{prefix}1{suffix} — found indices {sorted(indices)}, expected {expected}"
        )
    return [mgmt_name] + [f"{prefix}{n}{suffix}" for n in expected]
