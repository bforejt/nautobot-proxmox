"""
Minimal Proxmox VE API client for the NFV lifecycle jobs.

Deliberately built on `requests` (a Nautobot core dependency) rather than
proxmoxer: Git-synced jobs cannot declare pip dependencies, and the API
surface these jobs need is small. Token auth only (PVEAPIToken header),
privilege-separated service account expected (see docs/plan-of-attack.md §3).

No Nautobot imports — testable standalone, same separation as the other libs.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import requests


class ProxmoxError(RuntimeError):
    """Any Proxmox API failure. `status_code` is the HTTP status when the
    failure was an HTTP error response (None for transport/body errors)."""

    def __init__(self, *args: Any, status_code: Optional[int] = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


class ProxmoxTaskError(ProxmoxError):
    pass


class ProxmoxUnreachableError(ProxmoxError):
    """The API could not be observed: transport failure (connection refused or
    reset, timeout, TLS), an HTTP 5xx (incl. pveproxy's 596), or a body that is
    not the JSON {"data": ...} envelope. A ProxmoxError subclass, so every
    best-effort `except ProxmoxError` guard also covers a node that went away;
    wait_task treats it as transient and retries the status poll."""


class ProxmoxAgentPermissionError(ProxmoxError):
    """The guest-agent probe was refused (401/403): the token lacks
    VM.GuestAgent.Audit. Raised instead of reporting "agent not ready"."""


_log = logging.getLogger(__name__)


def task_exit_outcome(exitstatus: Any) -> tuple[bool, int]:
    """Classify a stopped PVE task's `exitstatus` -> (succeeded, warning_count).

    PVE (pve-common RESTEnvironment fork_worker) ends a task that completed
    but emitted log_warn() lines with `TASK WARNINGS: <n>` and exit code 0,
    so its exitstatus is "WARNINGS: <n>" -- a success, not a failure. Only
    "OK" and "WARNINGS..." are successes; anything else (an error message,
    "unexpected status", an empty/missing value) is a failure. A "WARNINGS"
    status whose count does not parse is still a success, reported as at
    least one warning so it is never silently dropped.
    """
    if not isinstance(exitstatus, str):
        return False, 0
    status = exitstatus.strip()
    if status == "OK":
        return True, 0
    if status.startswith("WARNINGS"):
        _, _, tail = status.partition(":")
        try:
            count = int(tail.strip())
        except ValueError:
            count = 1
        return True, max(count, 1)
    return False, 0


@dataclass
class ProxmoxClient:
    host: str
    token_id: str        # e.g. "svc-nfv@pve!deploy"
    token_secret: str
    port: int = 8006
    verify_tls: bool = False
    timeout: int = 60
    logger: Any = None   # job logger (self.logger) so task warnings reach the JobResult

    def __post_init__(self) -> None:
        self.base_url = f"https://{self.host}:{self.port}/api2/json"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"PVEAPIToken={self.token_id}={self.token_secret}"
        self.session.verify = self.verify_tls
        if not self.verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    # ---------- low level ----------

    @staticmethod
    def _data(what: str, r: Any) -> Any:
        """HTTP status check + JSON envelope unwrap for one response, mapping
        every failure to a ProxmoxError (never a raw requests/ValueError)."""
        if r.status_code >= 500:
            raise ProxmoxUnreachableError(
                f"{what} -> {r.status_code}: {r.text[:300]}", status_code=r.status_code
            )
        if r.status_code >= 400:
            raise ProxmoxError(f"{what} -> {r.status_code}: {r.text[:300]}", status_code=r.status_code)
        try:
            body = r.json()
        except ValueError as exc:  # requests' JSONDecodeError is a ValueError
            raise ProxmoxUnreachableError(
                f"{what} -> {r.status_code}: response is not JSON: {r.text[:120]!r}"
            ) from exc
        if not isinstance(body, dict):
            raise ProxmoxUnreachableError(
                f"{what} -> {r.status_code}: unexpected JSON body (no 'data' envelope)"
            )
        return body.get("data")

    def _req(self, method: str, path: str, data: Optional[dict] = None) -> Any:
        try:
            r = self.session.request(method, f"{self.base_url}{path}", data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProxmoxUnreachableError(
                f"{method} {path} -> transport error: {type(exc).__name__}: {exc}"
            ) from exc
        return self._data(f"{method} {path}", r)

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(self, path: str, data: Optional[dict] = None) -> Any:
        return self._req("POST", path, data or {})

    def put(self, path: str, data: Optional[dict] = None) -> Any:
        return self._req("PUT", path, data or {})

    def delete(self, path: str) -> Any:
        return self._req("DELETE", path)

    # ---------- tasks ----------

    # Consecutive failed status polls wait_task tolerates before giving up on
    # observing a task (a pveproxy restart or a dropped connection must not
    # abandon a multi-minute import-from create that is still running).
    TASK_POLL_MAX_FAILURES = 5
    TASK_POLL_BACKOFF_CAP = 30  # seconds between polls while failing

    def wait_task(self, node: str, upid: str, timeout: int = 600, poll: int = 3) -> None:
        """Block until the task finishes; raise ProxmoxTaskError on failure.

        A task that finished with warnings ("WARNINGS: n") succeeded; the
        count is logged at warning level (see task_exit_outcome).

        A status poll that could not observe the task (ProxmoxUnreachableError:
        transport error, 5xx, non-JSON) is logged and retried with a growing
        delay; a successful poll resets the count. After TASK_POLL_MAX_FAILURES
        consecutive failures it raises ProxmoxUnreachableError -- "could not
        observe the task", distinct from ProxmoxTaskError "the task failed".
        A 4xx (e.g. permission, unknown UPID) is not transient and raises at
        once. `timeout` still bounds the summed poll intervals."""
        log = self.logger or _log
        waited = 0
        failures = 0
        while waited <= timeout:
            try:
                status = self.get(f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status")
                if not isinstance(status, dict):
                    raise ProxmoxUnreachableError(
                        f"task status for {upid} is not an object: {type(status).__name__}"
                    )
            except ProxmoxUnreachableError as exc:
                failures += 1
                if failures >= self.TASK_POLL_MAX_FAILURES:
                    raise ProxmoxUnreachableError(
                        f"Lost contact with task {upid} on {node} after {failures} consecutive "
                        f"failed status polls - the task may still be running on the node "
                        f"(check its Tasks panel): {exc}"
                    ) from exc
                delay = min(poll * failures, max(poll, self.TASK_POLL_BACKOFF_CAP))
                log.warning(
                    "Proxmox task %s on %s: status poll failed (%d/%d consecutive), "
                    "retrying in %ss: %s",
                    upid, node, failures, self.TASK_POLL_MAX_FAILURES, delay, exc,
                )
                time.sleep(delay)
                waited += delay
                continue
            failures = 0
            if status.get("status") == "stopped":
                exitstatus = status.get("exitstatus", "")
                ok, warnings = task_exit_outcome(exitstatus)
                if not ok:
                    raise ProxmoxTaskError(f"Task {upid} failed: {exitstatus}")
                if warnings:
                    log.warning(
                        "Proxmox task %s on %s succeeded with %d warning(s) - "
                        "see the task log on the node (Tasks panel) for the WARN lines",
                        upid, node, warnings,
                    )
                return
            time.sleep(poll)
            waited += poll
        raise ProxmoxTaskError(f"Task {upid} did not finish within {timeout}s")

    # ---------- inventory ----------

    def version(self) -> dict:
        return self.get("/version")

    def node_status(self, node: str) -> dict:
        return self.get(f"/nodes/{node}/status")

    def next_vmid(self) -> int:
        return int(self.get("/cluster/nextid"))

    def list_vms(self, node: str) -> list[dict]:
        return self.get(f"/nodes/{node}/qemu")

    def vm_config(self, node: str, vmid: int) -> dict:
        return self.get(f"/nodes/{node}/qemu/{vmid}/config")

    def storages(self, node: str) -> list[dict]:
        return self.get(f"/nodes/{node}/storage")

    def storage_content(self, node: str, storage: str, content: Optional[str] = None) -> list[dict]:
        suffix = f"?content={content}" if content else ""
        return self.get(f"/nodes/{node}/storage/{storage}/content{suffix}")

    # ---------- images ----------

    def find_import_volume(self, node: str, storage: str, filename: str) -> Optional[str]:
        for item in self.storage_content(node, storage, "import"):
            if item.get("volid", "").endswith(f"/{filename}"):
                return item["volid"]
        return None

    def download_url(self, node: str, storage: str, url: str, filename: str,
                     content: str = "import", checksum: Optional[str] = None,
                     checksum_algorithm: str = "sha256", timeout: int = 1800) -> str:
        """Pull a file onto node storage; returns the resulting volid."""
        params = {"url": url, "content": content, "filename": filename}
        if checksum:
            params["checksum"] = checksum
            params["checksum-algorithm"] = checksum_algorithm
        upid = self.post(f"/nodes/{node}/storage/{storage}/download-url", params)
        self.wait_task(node, upid, timeout=timeout, poll=5)
        return f"{storage}:{content}/{filename}"

    def ensure_image(self, node: str, storage: str, filename: str, url: str,
                     checksum: Optional[str], checksum_algorithm: str = "sha256",
                     logger=None) -> str:
        """Idempotent: return the import volid, pulling from `url` if absent."""
        volid = self.find_import_volume(node, storage, filename)
        if volid:
            if logger:
                logger.info("Image already present on %s: %s", node, volid)
            return volid
        if logger:
            logger.info("Image not on node - pulling %s from %s (checksum-verified)", filename, url)
        return self.download_url(node, storage, url, filename,
                                 checksum=checksum, checksum_algorithm=checksum_algorithm)

    def find_iso_volume(self, node: str, storage: str, filename: str) -> Optional[str]:
        for item in self.storage_content(node, storage, "iso"):
            if item.get("volid", "").endswith(f"/{filename}"):
                return item["volid"]
        return None

    def upload_file(self, node: str, storage: str, local_path: str,
                    content: str = "iso", filename: Optional[str] = None,
                    timeout: int = 600) -> str:
        """Multipart upload to node storage (API-accepted content types only:
        iso/vztmpl/import — snippets are NOT uploadable, a PVE limitation).
        Overwrites an existing same-named file. Returns the volid."""
        import os
        filename = filename or os.path.basename(local_path)
        with open(local_path, "rb") as fh:
            try:
                r = self.session.post(
                    f"{self.base_url}/nodes/{node}/storage/{storage}/upload",
                    data={"content": content}, files={"filename": (filename, fh)},
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                raise ProxmoxUnreachableError(
                    f"upload {filename} -> transport error: {type(exc).__name__}: {exc}"
                ) from exc
        upid = self._data(f"upload {filename}", r)
        if isinstance(upid, str) and upid.startswith("UPID"):
            self.wait_task(node, upid, timeout=timeout)
        return f"{storage}:{content}/{filename}"

    def delete_volume(self, node: str, storage: str, volid: str, timeout: int = 300) -> None:
        """Delete loose storage content (e.g. a bootstrap ISO). Needs
        Datastore.Allocate on the storage — see the NFVAutomation role."""
        result = self.delete(
            f"/nodes/{node}/storage/{storage}/content/{urllib.parse.quote(volid, safe='')}"
        )
        if isinstance(result, str) and result.startswith("UPID"):
            self.wait_task(node, result, timeout=timeout)

    # ---------- VM lifecycle ----------

    def create_vm(self, node: str, params: dict, timeout: int = 900) -> None:
        upid = self.post(f"/nodes/{node}/qemu", params)
        self.wait_task(node, upid, timeout=timeout, poll=5)

    def set_vm_config(self, node: str, vmid: int, params: dict, timeout: int = 300) -> None:
        """Apply VM config; some changes (e.g. import-from disks) return a task."""
        result = self.put(f"/nodes/{node}/qemu/{vmid}/config", params)
        if isinstance(result, str) and result.startswith("UPID"):
            self.wait_task(node, result, timeout=timeout)

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        self.put(f"/nodes/{node}/qemu/{vmid}/resize", {"disk": disk, "size": size})

    def start_vm(self, node: str, vmid: int, timeout: int = 300) -> None:
        upid = self.post(f"/nodes/{node}/qemu/{vmid}/status/start")
        self.wait_task(node, upid, timeout=timeout)

    def stop_vm(self, node: str, vmid: int, timeout: int = 300) -> None:
        upid = self.post(f"/nodes/{node}/qemu/{vmid}/status/stop")
        self.wait_task(node, upid, timeout=timeout)

    def destroy_vm(self, node: str, vmid: int, timeout: int = 300) -> None:
        upid = self.delete(f"/nodes/{node}/qemu/{vmid}?purge=1&destroy-unreferenced-disks=1")
        self.wait_task(node, upid, timeout=timeout)

    def agent_ipv4(self, node: str, vmid: int) -> Optional[str]:
        """First non-loopback IPv4 the guest agent reports, or None.

        "Not ready yet" (agent not running -- PVE answers 500 -- or any other
        failed probe) is None, so wait_agent_ipv4 keeps polling. A 401/403 is
        NOT "not ready": the token can never see the agent (PVE 8.2+/9.x gate
        network-get-interfaces on VM.GuestAgent.Audit), so polling would only
        burn the whole wait and end in a misleading "agent never came up".
        That raises ProxmoxAgentPermissionError at once.
        """
        try:
            result = self.get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except ProxmoxError as exc:
            if exc.status_code in (401, 403):
                raise ProxmoxAgentPermissionError(
                    f"guest-agent probe on VM {vmid} refused ({exc.status_code}): the API token "
                    "lacks VM.GuestAgent.Audit -- add it to the NFVAutomation role "
                    f"(pveum role modify, see getting-started section 4). Detail: {exc}",
                    status_code=exc.status_code,
                ) from exc
            return None
        for iface in (result or {}).get("result", []):
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses", []):
                ip = addr.get("ip-address", "")
                if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                    return ip
        return None

    def wait_agent_ipv4(self, node: str, vmid: int, timeout: int = 600, poll: int = 10) -> Optional[str]:
        waited = 0
        while waited <= timeout:
            ip = self.agent_ipv4(node, vmid)
            if ip:
                return ip
            time.sleep(poll)
            waited += poll
        return None

    @staticmethod
    def encode_sshkeys(keys: str) -> str:
        """PVE quirk: the sshkeys config value must itself be percent-encoded."""
        return urllib.parse.quote(keys.strip(), safe="")
