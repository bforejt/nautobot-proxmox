"""
PA-VM day-0 bootstrap payload builder (the `pa-bootstrap` day0_builder).

Renders the VM-Series bootstrap package — init-cfg.txt (+ minimal
bootstrap.xml setting the admin password, + optional /license/authcodes) —
and masters it into the CD-ROM ISO PAN-OS reads on factory-default first
boot. Package layout per Palo Alto's spec: the four directories /config,
/license, /software, /content are MANDATORY even when empty.

Pure stdlib except `build_bootstrap_iso`, which lazy-imports pycdlib — a
deliberate, recorded exception to the "requests-only" dependency rule
(decision log): pycdlib ships in the nautobot-composer image; a stack whose
image predates it gets a precise refusal, not an ImportError mid-deploy.

Everything above the ISO step is unit-testable without Nautobot or pycdlib
(tests/test_pa_bootstrap.py).

Security note: the rendered ISO carries credential material (admin password
HASH, optionally license authcodes). Callers must build it under a scrubbed
tempdir and delete the node-side copy after first boot.
"""

import hashlib
import ipaddress
import re
import secrets as _secrets

# PAN-OS hostname: <=31 chars from [0-9A-Za-z._-]. This same set is safe for
# the PVE upload filename (PVE normalizes anything outside [A-Za-z0-9_.-]) and
# cannot inject init-cfg lines — one gate covers all three consumers.
_HOSTNAME_RE = re.compile(r"^[0-9A-Za-z._-]{1,31}$")


class PaBootstrapError(ValueError):
    """The PA day-0 payload cannot be rendered from the given SoT data."""


# ---------------------------------------------------------------- init-cfg

def netmask_from_prefixlen(prefixlen):
    """CIDR prefix length -> dotted-quad netmask (init-cfg wants dotted)."""
    return str(ipaddress.IPv4Network((0, int(prefixlen))).netmask)


def render_init_cfg(hostname, *, dhcp, ip=None, netmask=None, gateway=None,
                    dns=(), scm_pin_id=None, scm_pin_value=None):
    """Render init-cfg.txt. Static mode requires ip/netmask/gateway and at
    least one DNS server (the firewall cannot license or fetch content
    without resolution); DHCP mode forbids them (the lease provides them).
    SCM registration (panorama-server=cloud + PIN pair) is added when the
    PIN is given — decision #2: per-VM attribute, not a code branch."""
    if not _HOSTNAME_RE.match(hostname or ""):
        raise PaBootstrapError(
            f"device name {hostname!r} is not a valid PA hostname "
            "(1-31 chars from letters/digits/._-)"
        )
    lines = []
    if dhcp:
        if ip or netmask or gateway:
            raise PaBootstrapError("dhcp mode: ip/netmask/gateway must not be set")
        lines += ["type=dhcp-client", "dhcp-send-hostname=yes", "dhcp-send-client-id=yes"]
    else:
        if not (ip and netmask and gateway):
            raise PaBootstrapError("static mode requires ip, netmask, and gateway")
        if not dns:
            raise PaBootstrapError(
                "static mode requires at least one DNS server (DNS-role IP in the mgmt prefix)"
            )
        lines += ["type=static", f"ip-address={ip}", f"netmask={netmask}",
                  f"default-gateway={gateway}"]
    lines.append(f"hostname={hostname}")
    dns = list(dns)
    if dns:
        lines.append(f"dns-primary={dns[0]}")
    if len(dns) > 1:
        lines.append(f"dns-secondary={dns[1]}")
    if (scm_pin_id is None) != (scm_pin_value is None):
        raise PaBootstrapError("SCM registration needs BOTH pin id and pin value")
    if scm_pin_id is not None:
        lines += [
            "panorama-server=cloud",
            f"vm-series-auto-registration-pin-id={scm_pin_id}",
            f"vm-series-auto-registration-pin-value={scm_pin_value}",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- md5-crypt

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _md5crypt_b64(data):
    # md5-crypt's custom base64 (crypt-md5.c reference): big-endian pack of
    # the byte triplets (0,6,12)(1,7,13)(2,8,14)(3,9,15)(4,10,5) + byte 11,
    # emitted low-6-bits-first.
    order = [(0, 6, 12), (1, 7, 13), (2, 8, 14), (3, 9, 15), (4, 10, 5), (11,)]
    out = []
    for group in order:
        value = 0
        for idx in group:
            value = (value << 8) | data[idx]
        for _ in range(len(group) + 1):
            out.append(_ITOA64[value & 0x3F])
            value >>= 6
    return "".join(out)


def md5crypt(password, salt=None):
    """MD5-crypt ($1$) phash, as `openssl passwd -1` / PAN-OS
    `request password-hash` produce for bootstrap.xml. Pure hashlib —
    Python's `crypt` module is gone in 3.13 and platform-dependent before
    that. Verified against openssl vectors in tests/test_pa_bootstrap.py."""
    if salt is None:
        salt = "".join(_secrets.choice(_ITOA64[2:]) for _ in range(8))
    if not (1 <= len(salt) <= 8) or "$" in salt:
        raise PaBootstrapError("md5-crypt salt must be 1-8 chars, no '$'")
    pw = password.encode()
    sb = salt.encode()

    alt = hashlib.md5(pw + sb + pw).digest()
    ctx = hashlib.md5(pw + b"$1$" + sb)
    for i in range(len(pw)):
        ctx.update(alt[i % 16:i % 16 + 1])
    i = len(pw)
    while i:
        ctx.update(b"\x00" if i & 1 else pw[:1])
        i >>= 1
    result = ctx.digest()
    for i in range(1000):
        ctx = hashlib.md5()
        ctx.update(pw if i & 1 else result)
        if i % 3:
            ctx.update(sb)
        if i % 7:
            ctx.update(pw)
        ctx.update(result if i & 1 else pw)
        result = ctx.digest()
    return f"$1${salt}${_md5crypt_b64(result)}"


# ------------------------------------------------------------ bootstrap.xml

def render_bootstrap_xml(admin_phash, config_version="10.1.0"):
    """Minimal bootstrap.xml: set the admin password (as a phash — never
    plaintext) so the firewall never answers on mgmt with admin/admin and
    the API is usable post-autocommit. PAN-OS loads bootstrap.xml as the
    candidate config and autocommits; unspecified sections take defaults.
    config_version must not be NEWER than the running PAN-OS (older is
    migrated up automatically) — callers derive it from the SoT software
    version. [lab-verify: minimal mgt-config-only file accepted by 11.2]"""
    if not admin_phash.startswith("$1$"):
        raise PaBootstrapError("admin_phash must be an md5-crypt ($1$) phash")
    if not re.match(r"^\d+\.\d+\.\d+$", config_version):
        raise PaBootstrapError(f"config_version {config_version!r} is not X.Y.Z")
    return (
        '<?xml version="1.0"?>\n'
        f'<config version="{config_version}" urldb="paloaltonetworks">\n'
        "  <mgt-config>\n"
        "    <users>\n"
        '      <entry name="admin">\n'
        f"        <phash>{admin_phash}</phash>\n"
        "        <permissions>\n"
        "          <role-based>\n"
        "            <superuser>yes</superuser>\n"
        "          </role-based>\n"
        "        </permissions>\n"
        "      </entry>\n"
        "    </users>\n"
        "  </mgt-config>\n"
        "</config>\n"
    )


# ------------------------------------------------------------------- ISO

# (iso9660 8.3 name, Rock Ridge / Joliet long name) per package file. The
# firewall's kernel mounts via Rock Ridge/Joliet, so the long names are what
# matter; the 8.3 forms just have to be valid ISO identifiers.
_PACKAGE_DIRS = ("config", "license", "software", "content")


def build_bootstrap_iso(dest_path, init_cfg, bootstrap_xml=None, authcodes=None):
    """Master the bootstrap package ISO at dest_path. All four package
    directories are created even when empty (spec requirement)."""
    try:
        import pycdlib
    except ImportError as exc:
        raise PaBootstrapError(
            "pycdlib is not installed in this Nautobot image — PA deploys need it "
            "for bootstrap-ISO mastering. Rebuild the stack from a nautobot-composer "
            "checkout that lists pycdlib in its requirements (git-synced jobs cannot "
            "install packages themselves)."
        ) from exc

    files = {("config", "INITCFG.TXT", "init-cfg.txt"): init_cfg.encode()}
    if bootstrap_xml is not None:
        files[("config", "BOOTSTRP.XML", "bootstrap.xml")] = bootstrap_xml.encode()
    if authcodes is not None:
        # Exact filename "authcodes", no extension (a documented PA gotcha).
        files[("license", "AUTHCODE.", "authcodes")] = authcodes.encode()

    import io

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="PABOOTSTRAP")
    try:
        for d in _PACKAGE_DIRS:
            iso.add_directory(f"/{d.upper()}", rr_name=d, joliet_path=f"/{d}")
        for (d, iso_name, long_name), payload in files.items():
            iso.add_fp(
                io.BytesIO(payload), len(payload),
                f"/{d.upper()}/{iso_name};1", rr_name=long_name,
                joliet_path=f"/{d}/{long_name}",
            )
        iso.write(dest_path)
    finally:
        iso.close()
    return dest_path
