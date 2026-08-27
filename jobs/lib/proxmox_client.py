"""
Minimal Proxmox VE API client for the NFV lifecycle jobs.

Deliberately built on `requests` (a Nautobot core dependency) rather than
proxmoxer: Git-synced jobs cannot declare pip dependencies, and the API
surface these jobs need is small. Token auth only (PVEAPIToken header),
privilege-separated service account expected (see docs/plan-of-attack.md §3).

No Nautobot imports — testable standalone, same separation as the other libs.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import requests


class ProxmoxError(RuntimeError):
    pass


class ProxmoxTaskError(ProxmoxError):
    pass


@dataclass
class ProxmoxClient:
    host: str
    token_id: str        # e.g. "svc-nfv@pve!deploy"
    token_secret: str
    port: int = 8006
    verify_tls: bool = False
    timeout: int = 60

    def __post_init__(self) -> None:
        self.base_url = f"https://{self.host}:{self.port}/api2/json"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"PVEAPIToken={self.token_id}={self.token_secret}"
        self.session.verify = self.verify_tls
        if not self.verify_tls:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    # ---------- low level ----------

    def _req(self, method: str, path: str, data: Optional[dict] = None) -> Any:
        r = self.session.request(method, f"{self.base_url}{path}", data=data, timeout=self.timeout)
        if r.status_code >= 400:
            raise ProxmoxError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json().get("data")

    def get(self, path: str) -> Any:
        return self._req("GET", path)

    def post(self, path: str, data: Optional[dict] = None) -> Any:
        return self._req("POST", path, data or {})

    def put(self, path: str, data: Optional[dict] = None) -> Any:
        return self._req("PUT", path, data or {})

    def delete(self, path: str) -> Any:
        return self._req("DELETE", path)

    # ---------- tasks ----------

    def wait_task(self, node: str, upid: str, timeout: int = 600, poll: int = 3) -> None:
        """Block until the task finishes; raise ProxmoxTaskError on failure."""
        waited = 0
        while waited <= timeout:
            status = self.get(f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status")
            if status.get("status") == "stopped":
                exitstatus = status.get("exitstatus", "")
                if exitstatus != "OK":
                    raise ProxmoxTaskError(f"Task {upid} failed: {exitstatus}")
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
            r = self.session.post(
                f"{self.base_url}/nodes/{node}/storage/{storage}/upload",
                data={"content": content}, files={"filename": (filename, fh)},
                timeout=timeout,
            )
        if r.status_code >= 400:
            raise ProxmoxError(f"upload {filename} -> {r.status_code}: {r.text[:300]}")
        upid = r.json().get("data")
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
        """First non-loopback IPv4 the guest agent reports, or None."""
        try:
            result = self.get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
        except ProxmoxError:
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
