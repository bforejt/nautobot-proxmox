"""
Nautobot Job: host-side SE350 verification over SSH — checklist §§4/5/6/9.

Runs against a Linux-booted SE350 (a manual PVE install like the burn-in unit
is perfect) and automates the host-visible half of the verification checklist
in one pass:

  §4  Disk identity: full udev inventory of every whole disk, then the
      DeviceType profile's install disk_filter evaluated EXACTLY as the
      auto-installer would (glob match on udev properties) — PASS only if it
      selects precisely one disk. Also cross-checks the boot adapter's PCI
      address from the pinned ID_PATH against lspci.
  §5  DMI serial: reads /sys/class/dmi/id/product_serial (the value the
      installer POSTs to the answer service) and compares it to the Nautobot
      Device's serial when a Device is supplied.
  §6  X722 firmware LLDP: per-port `ethtool --show-priv-flags` presence/state
      of disable-fw-lldp on i40e interfaces.
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

from nautobot.apps.jobs import IPAddressVar, Job, ObjectVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import Secret

from ..lib.install_delivery import DeliveryError, load_profile

HOST_SSH_USERNAME_SECRET = "host_ssh_username"
HOST_SSH_PASSWORD_SECRET = "host_ssh_password"
DEFAULT_DEVICE_TYPE = "ThinkSystem SE350"


class VerifySe350Host(Job):
    class Meta:
        name = "SE350 Host Verification (SSH)"
        description = (
            "Read-only SSH pass over a Linux-booted SE350: disk inventory + "
            "install disk-filter validation (checklist §4), DMI serial vs "
            "Nautobot (§5), X722 disable-fw-lldp (§6), Secure Boot state (§9), "
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
            "echo \"SIZE $(lsblk -dn -o SIZE \"$d\" | tr -d ' ')\"; done",
        )
        if rc != 0 and not out:
            return "FAIL", "could not enumerate disks (udevadm/lsblk missing?)"
        disks, current = [], None
        for line in out.splitlines():
            if line.startswith("DEV "):
                current = {"DEVNAME": line[4:]}
                disks.append(current)
            elif current is not None and line.startswith("SIZE "):
                current["_size"] = line[5:]
            elif current is not None and "=" in line:
                key, _, value = line.partition("=")
                current[key] = value
        for d in disks:
            self.logger.info(
                "Disk %s: size=%s model=%s serial=%s path=%s",
                d.get("DEVNAME"), d.get("_size"), d.get("ID_MODEL"),
                d.get("ID_SERIAL"), d.get("ID_PATH"),
            )
        disk_filter = profile.get("install", {}).get("disk_filter", {})
        if not disk_filter:
            return "SKIP", "profile has no disk_filter to evaluate"
        matched = [
            d for d in disks
            if all(fnmatch.fnmatch(d.get(k, ""), glob) for k, glob in disk_filter.items())
        ]
        names = [f"{d['DEVNAME']} ({d.get('_size')})" for d in matched]
        if len(matched) == 1:
            return "PASS", (
                f"install disk_filter {disk_filter} selects exactly one disk: "
                f"{names[0]} — the installer cannot touch the other volume(s)"
            )
        if not matched:
            return "FAIL", (
                f"disk_filter {disk_filter} matches NO disk on this unit — an "
                "install here would fail-closed; capture the inventory above "
                "and update the profile for this hardware revision"
            )
        return "FAIL", f"disk_filter {disk_filter} is AMBIGUOUS — matches {names}"

    def _check_boot_adapter(self, client, profile):
        id_path = profile.get("install", {}).get("disk_filter", {}).get("ID_PATH", "")
        if not id_path.startswith("pci-"):
            return "SKIP", "profile filter is not ID_PATH-based"
        pci_addr = id_path.split("-ata")[0].replace("pci-", "")
        rc, out, _ = self._run(client, "lspci | grep -iE 'sata|raid|ahci'")
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

    def _check_x722_lldp(self, client):
        rc, out, _ = self._run(
            client,
            "for i in /sys/class/net/*; do n=$(basename \"$i\"); "
            "d=$(basename \"$(readlink -f \"$i/device/driver\" 2>/dev/null)\" 2>/dev/null); "
            "[ \"$d\" = i40e ] && echo \"PORT $n :: "
            "$(ethtool --show-priv-flags \"$n\" 2>/dev/null | grep -i disable-fw-lldp "
            "|| echo flag-missing)\"; done; true",
        )
        ports = [line for line in out.splitlines() if line.startswith("PORT ")]
        if not ports:
            return "SKIP", "no i40e (X722) interfaces found on this unit"
        for line in ports:
            self.logger.info("%s", line)
        missing = [p for p in ports if "flag-missing" in p]
        if missing:
            return "FAIL", (
                f"{len(missing)}/{len(ports)} X722 ports lack the disable-fw-lldp "
                "priv-flag at this NIC firmware — the firstboot LLDP step would no-op"
            )
        return "PASS", (
            f"all {len(ports)} X722 ports expose disable-fw-lldp (states logged above)"
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
            # Sanity probe: an SE350 has TWO SSH-able faces, and the wrong one
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
                ("§4 boot adapter topology", lambda: self._check_boot_adapter(client, profile)),
                ("§5 DMI serial vs SoT", lambda: self._check_dmi_serial(client, device)),
                ("§6 X722 disable-fw-lldp", lambda: self._check_x722_lldp(client)),
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
