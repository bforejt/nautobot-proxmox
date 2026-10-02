"""
BMC identity guard for the bare-metal jobs (fail closed).

The jobs reach a server's BMC through the IP on its Device's `xcc` interface.
A stale or mistyped IP would point every write — RAID volume creation,
virtual-media mount, boot override, power action — at SOME OTHER machine.
Before any of that, the jobs read the BMC's ComputerSystem SerialNumber and
refuse unless it is the Device's serial (trimmed, case-insensitive). A BMC
that reports no serial, or cannot be read, is refused too: identity is never
assumed.

Nautobot-free and importable by file path (tests/test_bmc_identity.py); the
Redfish client is injected — anything with RedfishDiscovery.system_info().
"""


class BmcIdentityError(RuntimeError):
    """The BMC at the Device's xcc address is not (provably) this Device."""


def _norm(serial):
    return str(serial or "").strip().upper()


def check_bmc_identity(system_info, device_name, device_serial, bmc_ip):
    """Pure check over a system_info() dict. Returns the BMC-reported serial;
    raises BmcIdentityError on any mismatch, read error, or missing value."""
    expected = _norm(device_serial)
    if not expected:
        raise BmcIdentityError(
            f"{device_name} has no serial — cannot verify that the BMC at {bmc_ip} is this "
            "Device; refusing to touch it"
        )
    info = system_info if isinstance(system_info, dict) else {}
    if info.get("error"):
        raise BmcIdentityError(
            f"could not read the system serial from the BMC at {bmc_ip} ({info['error']}) — "
            f"refusing to touch it without proving it is {device_name}"
        )
    reported = _norm(info.get("serial_number"))
    if not reported:
        raise BmcIdentityError(
            f"the BMC at {bmc_ip} reports no system serial — cannot verify it is {device_name} "
            f"(serial {device_serial!r}); refusing to touch it"
        )
    if reported != expected:
        raise BmcIdentityError(
            f"the BMC at {bmc_ip} belongs to serial {str(info.get('serial_number')).strip()!r}, "
            f"but {device_name}'s serial is {str(device_serial).strip()!r} — wrong xcc IP on the "
            "Device (or wrong serial); refusing to touch that machine"
        )
    return str(info.get("serial_number")).strip()


def verify_bmc_identity(redfish, device_name, device_serial, bmc_ip):
    """Read the BMC's system serial and check it against the Device."""
    return check_bmc_identity(redfish.system_info(), device_name, device_serial, bmc_ip)
