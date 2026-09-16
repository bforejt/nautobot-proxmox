"""
Nautobot Job: host-side edge-node verification over SSH — checklist §§4/5/6/9.

Runs against a Linux-booted edge node (SE350 or SE455 V3; a manual PVE
install like the burn-in unit is perfect) and automates the host-visible half
of the verification checklist in one pass:

  §4  Disk identity: full udev inventory of every whole disk, then the
      DeviceType profile's install disk_filter evaluated EXACTLY as the
      auto-installer would (glob match on udev properties, filter_match
      any/all) — PASS only if it selects precisely the number of disks the
      profile's filesystem needs (1 for ext4/xfs/btrfs, 2 for a zfs raid1
      pair, ...). For profiles with install.data_pool / install.data_volume,
      also checks that the remaining disks leave exactly the expected data
      pair / data virtual drive. Cross-checks the
      boot adapter's PCI address from a pinned ID_PATH against lspci.
  §5  DMI serial: reads /sys/class/dmi/id/product_serial (the value the
      installer POSTs to the answer service) and compares it to the Nautobot
      Device's serial when a Device is supplied.
  §6  Firmware LLDP agents: per-port `ethtool --show-priv-flags` presence and
      state of disable-fw-lldp (i40e: X722/X710) and fw-lldp-agent (ice: E810).
  §9  Secure Boot state as the OS sees it (mokutil/bootctl) — reported, not
      judged (fleet SB standard still pending sign-off).
  +   Informational BIOS-effect readbacks: cpuidle state list (C-state
      disable check), cpufreq governor, core count.

Credentials come from Nautobot Secrets named ``host_ssh_username`` /
``host_ssh_password`` (root or sudo-capable). All commands are READ-ONLY.
Uses paramiko (present in the composer stack via the device-onboarding /
Nornir dependency chain); host keys are auto-accepted — lab tooling.
"""

import fnmatch
import re

from nautobot.apps.jobs import IPAddressVar, Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import Secret

from ..lib.install_delivery import DeliveryError, load_profile

def _human(num_bytes):
    value = float(num_bytes or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}" if unit not in ("B",) else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}T"


HOST_SSH_USERNAME_SECRET = "host_ssh_username"
HOST_SSH_PASSWORD_SECRET = "host_ssh_password"
DEFAULT_DEVICE_TYPE = "ThinkSystem SE350"


# Disks the installer needs the boot filter to select, per filesystem/raid.
# (exact, min) — None min = exact only.
_EXPECTED_BOOT_DISKS = {
    "raid0": (None, 1),
    "raid1": (2, None),
    "raid10": (None, 4),
    "raidz-1": (None, 3),
    "raidz-2": (None, 4),
    "raidz-3": (None, 5),
}


def expected_boot_disks(profile):
    """(exact, minimum) number of disks the profile's boot filter must select."""
    install = profile.get("install", {})
    fs = install.get("filesystem", "ext4")
    if fs in ("zfs", "btrfs"):
        raid = str(install.get(fs, {}).get("raid", "raid0")).lower()
        return _EXPECTED_BOOT_DISKS.get(raid, (None, 1))
    return (1, None)


def evaluate_disk_filter(disks, disk_filter, filter_match="any"):
    """The auto-installer's disk matcher: glob per udev key; `any` (default)
    selects a disk when one key matches, `all` when every key matches."""
    match_all = str(filter_match or "any").lower() == "all"
    selected = []
    for d in disks:
        hits = [fnmatch.fnmatch(d.get(k, ""), glob) for k, glob in disk_filter.items()]
        if hits and (all(hits) if match_all else any(hits)):
            selected.append(d)
    return selected


class VerifySe350Host(Job):
    class Meta:
        name = "SE350 Host Verification (SSH)"
        description = (
            "Read-only SSH pass over a Linux-booted edge node (SE350 / SE455 V3): "
            "disk inventory + install disk-filter validation with the installer's "
            "matching rules (checklist §4), data-pool preflight, DMI serial vs "
            "Nautobot (§5), firmware LLDP flags on i40e/ice (§6), Secure Boot state (§9), "
            "BIOS-effect readbacks. Secrets: host_ssh_username/host_ssh_password."
        )
        has_sensitive_variables = False
        soft_time_limit = 300
        time_limit = 420

    host_ip = IPAddressVar(
        label="Host IP address",
        description="SSH target — the booted SE350's management IP",
    )
    device = ObjectVar(
        model=Device,
        label="Nautobot Device (optional)",
        description=(
            "Enables the §5 serial cross-check and selects the install profile "
            "from the DeviceType; omitted = report-only serial + the default "
            f"{DEFAULT_DEVICE_TYPE} profile"
        ),
        required=False,
    )

    # ---- ssh plumbing ----

    def _connect(self, host):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "paramiko is not installed in this worker — it ships with the "
                "composer stack's device-onboarding/Nornir dependencies"
            ) from exc
        try:
            username = Secret.objects.get(name=HOST_SSH_USERNAME_SECRET).get_value()
            password = Secret.objects.get(name=HOST_SSH_PASSWORD_SECRET).get_value()
        except Secret.DoesNotExist as exc:
            raise RuntimeError(
                f"Required Secret not found ({exc}). Expected Secrets named "
                f"{HOST_SSH_USERNAME_SECRET!r} and {HOST_SSH_PASSWORD_SECRET!r}."
            )
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # lab tooling
        client.connect(host, username=username, password=password, timeout=15,
                       look_for_keys=False, allow_agent=False)
        return client

    def _run(self, client, command, timeout=30):
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        rc = stdout.channel.recv_exit_status()
        return rc, out, err

    # ---- checks (each returns (verdict, detail); verdict in PASS/FAIL/INFO/SKIP) ----

    def _check_disks(self, client, profile):
        rc, out, _ = self._run(
            client,
            "for d in /dev/sd? /dev/nvme?n1; do [ -b \"$d\" ] || continue; "
            "echo \"DEV $d\"; udevadm info --query=property \"$d\" "
            "| grep -E '^(ID_MODEL|ID_SERIAL|ID_PATH)='; "
            "echo \"SIZE $(lsblk -dbn -o SIZE \"$d\" | tr -d ' ')\"; "
            "echo \"PARTS $(($(lsblk -n -o NAME \"$d\" | wc -l) - 1))\"; done",
        )
        if rc != 0 and not out:
            return "FAIL", "could not enumerate disks (udevadm/lsblk missing?)"
        disks, current = [], None
        for line in out.splitlines():
            if line.startswith("DEV "):
                current = {"DEVNAME": line[4:]}
                disks.append(current)
            elif current is not None and line.startswith("SIZE "):
                try:
                    current["_bytes"] = int(line[5:])
                except ValueError:
                    current["_bytes"] = 0
                current["_size"] = _human(current["_bytes"])
            elif current is not None and line.startswith("PARTS "):
                current["_parts"] = int(line[6:] or 0)
            elif current is not None and "=" in line:
                key, _, value = line.partition("=")
                current[key] = value
        for d in disks:
            self.logger.info(
                "Disk %s: size=%s model=%s serial=%s path=%s partitions=%s",
                d.get("DEVNAME"), d.get("_size"), d.get("ID_MODEL"),
                d.get("ID_SERIAL"), d.get("ID_PATH"), d.get("_parts", "?"),
            )
        self._disks = disks
        install = profile.get("install", {})
        disk_filter = install.get("disk_filter", {})
        if not disk_filter:
            return "SKIP", "profile has no disk_filter to evaluate"
        filter_match = install.get("filter_match", "any")
        matched = evaluate_disk_filter(disks, disk_filter, filter_match)
        self._boot_disks = matched
        names = [f"{d['DEVNAME']} ({d.get('_size')})" for d in matched]
        exact, minimum = expected_boot_disks(profile)
        want = f"exactly {exact}" if exact else f"at least {minimum}"
        if not matched:
            return "FAIL", (
                f"disk_filter {disk_filter} matches NO disk on this unit — an "
                "install here would fail-closed; capture the inventory above "
                "and update the profile for this hardware revision"
            )
        ok = (len(matched) == exact) if exact else (len(matched) >= minimum)
        if ok and len({d.get("_bytes") for d in matched}) > 1 and len(matched) > 1:
            return "FAIL", (
                f"disk_filter {disk_filter} selects {names} — a mirror over unequal "
                "sizes; the boot pair must be the matching (smaller) disks"
            )
        if ok:
            return "PASS", (
                f"install disk_filter {disk_filter} (match={filter_match}) selects "
                f"{want} disk(s) as the {install.get('filesystem', 'ext4')} layout "
                f"needs: {names} — the installer cannot touch the other volume(s)"
            )
        return "FAIL", (
            f"disk_filter {disk_filter} (match={filter_match}) selects {len(matched)} "
            f"disk(s) {names}; the profile's layout needs {want}"
        )

    def _check_data_pool(self, profile):
        """install.data_pool preflight: after the boot filter takes its disks,
        the largest remaining unused disks must be exactly `count` equal-sized
        ones >= min_size_gib — the same rule the firstboot hook applies."""
        spec = profile.get("install", {}).get("data_pool")
        if not spec:
            return "SKIP", "profile has no install.data_pool"
        disks = getattr(self, "_disks", None) or []
        boot = {d["DEVNAME"] for d in (getattr(self, "_boot_disks", None) or [])}
        count = int(spec.get("count", 2))
        min_bytes = int(spec.get("min_size_gib", 0)) * 1024 ** 3
        rest = [d for d in disks if d["DEVNAME"] not in boot and d.get("_bytes", 0) >= min_bytes]
        if not rest:
            return "FAIL", (
                f"no disks >= {spec.get('min_size_gib', 0)} GiB remain after the boot "
                f"filter — data pool {spec.get('name')!r} cannot be built"
            )
        top = max(d["_bytes"] for d in rest)
        pair = [d for d in rest if d["_bytes"] == top]
        names = [f"{d['DEVNAME']} ({d.get('_size')})" for d in pair]
        in_use = [d["DEVNAME"] for d in pair if d.get("_parts", 0)]
        if len(pair) != count:
            return "FAIL", (
                f"data pool {spec.get('name')!r} needs exactly {count} equal-sized "
                f"disks at the largest remaining size; found {len(pair)}: {names}"
            )
        note = (
            f" (NOTE: {in_use} already carry partitions — firstboot only creates "
            "the pool on signature-free disks; a same-named pool is imported)"
            if in_use else ""
        )
        return "PASS", (
            f"data pool {spec.get('name')!r} would mirror {names}{note}"
        )

    def _check_data_volume(self, profile):
        """install.data_volume preflight (RAID-adapter boxes): after the boot
        filter takes its disk, exactly one unused disk >= min_size_gib must be
        the largest remaining one — the firstboot LVM-thin rule."""
        spec = profile.get("install", {}).get("data_volume")
        if not spec:
            return "SKIP", "profile has no install.data_volume"
        disks = getattr(self, "_disks", None) or []
        boot = {d["DEVNAME"] for d in (getattr(self, "_boot_disks", None) or [])}
        min_bytes = int(spec.get("min_size_gib", 0)) * 1024 ** 3
        rest = [d for d in disks if d["DEVNAME"] not in boot and d.get("_bytes", 0) >= min_bytes]
        if not rest:
            return "FAIL", (
                f"no disk >= {spec.get('min_size_gib', 0)} GiB remains after the boot "
                f"filter — data volume {spec.get('vg')!r} cannot be built"
            )
        top = max(d["_bytes"] for d in rest)
        cands = [d for d in rest if d["_bytes"] == top]
        names = [f"{d['DEVNAME']} ({d.get('_size')})" for d in cands]
        if len(cands) != 1:
            return "FAIL", (
                f"data volume {spec.get('vg')!r} needs exactly one disk at the largest "
                f"remaining size; found {len(cands)}: {names}"
            )
        note = (
            " (NOTE: it already carries partitions — firstboot reuses an existing "
            f"volume group named {spec.get('vg')!r} and creates nothing otherwise)"
            if cands[0].get("_parts", 0) else ""
        )
        return "PASS", f"data volume {spec.get('vg')!r} would use {names[0]}{note}"

    def _check_boot_adapter(self, client, profile):
        id_path = profile.get("install", {}).get("disk_filter", {}).get("ID_PATH", "")
        if not id_path.startswith("pci-"):
            return "SKIP", "profile filter is not ID_PATH-based"
        # pci-0000:05:00.0-ata-1.0 / -nvme-1 / -scsi-0:2:0:0 / -sas-... -> 0000:05:00.0
        pci_addr = re.split(r"-(?:ata|nvme|scsi|sas)", id_path, maxsplit=1)[0].replace("pci-", "")
        rc, out, _ = self._run(client, "lspci | grep -iE 'sata|raid|ahci|nvme|sas'")
        present = any(line.startswith(pci_addr.replace("0000:", "")) for line in out.splitlines())
        detail = f"storage controllers: {out or '(none reported)'}"
        if present:
            return "PASS", f"boot adapter {pci_addr} present — {detail}"
        return "FAIL", f"boot adapter {pci_addr} NOT in lspci — {detail}"

    def _check_dmi_serial(self, client, device):
        rc, out, _ = self._run(client, "cat /sys/class/dmi/id/product_serial")
        serial = out.strip()
        if rc != 0 or not serial:
            return "FAIL", "could not read DMI product_serial"
        if device is None:
            return "INFO", (
                f"DMI serial = {serial!r} (this is the answer-service lookup key; "
                "no Device supplied to compare against)"
            )
        if serial == device.serial:
            return "PASS", f"DMI serial {serial!r} matches Device {device.name}"
        return "FAIL", (
            f"DMI serial {serial!r} != Device.serial {device.serial!r} — the "
            "answer service would refuse this machine"
        )

    def _check_fw_lldp(self, client):
        """Firmware LLDP agents that eat LLDP frames before the OS sees them:
        Intel 700-series (i40e: X722/X710) expose `disable-fw-lldp` (want on),
        Intel 800-series (ice: E810, the SE455 V3's OCP adapter) expose
        `fw-lldp-agent` (want off). Report-only — the firstboot LLDP step is
        plan Phase 3."""
        rc, out, _ = self._run(
            client,
            "for i in /sys/class/net/*; do n=$(basename \"$i\"); "
            "d=$(basename \"$(readlink -f \"$i/device/driver\" 2>/dev/null)\" 2>/dev/null); "
            "case \"$d\" in i40e|ice) echo \"PORT $n $d :: "
            "$(ethtool --show-priv-flags \"$n\" 2>/dev/null | grep -iE 'disable-fw-lldp|fw-lldp-agent' "
            "|| echo flag-missing)\";; esac; done; true",
        )
        ports = [line for line in out.splitlines() if line.startswith("PORT ")]
        if not ports:
            return "SKIP", "no i40e (X722/X710) or ice (E810) interfaces found on this unit"
        for line in ports:
            self.logger.info("%s", line)
        missing = [p for p in ports if "flag-missing" in p]
        if missing:
            return "FAIL", (
                f"{len(missing)}/{len(ports)} Intel ports lack the firmware-LLDP priv-flag "
                "at this NIC firmware — the LLDP step would no-op"
            )
        return "PASS", (
            f"all {len(ports)} Intel ports expose their firmware-LLDP flag (states logged "
            "above; i40e wants disable-fw-lldp ON, ice wants fw-lldp-agent OFF)"
        )

    def _check_secure_boot(self, client):
        rc, out, _ = self._run(
            client,
            "mokutil --sb-state 2>/dev/null || bootctl status 2>/dev/null "
            "| grep -i 'secure boot' || echo unknown",
        )
        return "INFO", f"Secure Boot (host view): {out or 'unknown'}"

    def _check_bios_effects(self, client):
        rc, out, _ = self._run(
            client,
            "echo cpuidle: $(cat /sys/devices/system/cpu/cpu0/cpuidle/state*/name "
            "2>/dev/null | tr '\\n' ' '); "
            "echo governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor "
            "2>/dev/null); echo cores: $(grep -c ^processor /proc/cpuinfo)",
        )
        return "INFO", f"BIOS-effect readbacks — {'; '.join(out.splitlines())}"

    def run(self, host_ip, device):
        model = device.device_type.model if device else DEFAULT_DEVICE_TYPE
        try:
            profile = load_profile(model)
        except DeliveryError as exc:
            raise RuntimeError(str(exc))
        client = self._connect(str(host_ip))
        try:
            # Sanity probe: an edge node has TWO SSH-able faces, and the wrong one
            # (the XCC's own CLI, prompt "system>") accepts logins but rejects
            # every Linux command. Catch that with one precise error instead
            # of six baffling per-check failures.
            _, probe, _ = self._run(client, "uname -s")
            if "Linux" not in probe:
                raise RuntimeError(
                    "This SSH endpoint is not a Linux host — it answers like a "
                    f"BMC/XCC management CLI (got: {probe[:120]!r}). Use the "
                    "HOST operating-system IP (the booted Proxmox/Linux "
                    "management address), not the XCC IP; and the "
                    f"{HOST_SSH_USERNAME_SECRET}/{HOST_SSH_PASSWORD_SECRET} "
                    "Secrets must hold the HOST login, not the XCC login."
                )
            checks = [
                ("§4 disk inventory + install filter", lambda: self._check_disks(client, profile)),
                ("§4 data-pool preflight", lambda: self._check_data_pool(profile)),
                ("§4 data-volume preflight", lambda: self._check_data_volume(profile)),
                ("§4 boot adapter topology", lambda: self._check_boot_adapter(client, profile)),
                ("§5 DMI serial vs SoT", lambda: self._check_dmi_serial(client, device)),
                ("§6 firmware LLDP (i40e/ice)", lambda: self._check_fw_lldp(client)),
                ("§9 Secure Boot state", lambda: self._check_secure_boot(client)),
                ("BIOS-effect readbacks", lambda: self._check_bios_effects(client)),
            ]
            failures, summary = [], []
            for name, check in checks:
                try:
                    verdict, detail = check()
                except Exception as exc:  # one broken check must not hide the rest
                    verdict, detail = "FAIL", f"check crashed: {exc}"
                line = f"{verdict}: {name} — {detail}"
                summary.append(line)
                log = self.logger.error if verdict == "FAIL" else self.logger.info
                log("%s", line)
                if verdict == "FAIL":
                    failures.append(name)
        finally:
            client.close()
        if failures:
            raise RuntimeError(
                f"{len(failures)} check(s) failed: {', '.join(failures)} — see log"
            )
        return "All host-side checks passed:\n" + "\n".join(summary)


register_jobs(VerifySe350Host)
