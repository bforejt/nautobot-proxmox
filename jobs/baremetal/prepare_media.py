"""
Nautobot Job: prepare installer media via the answer service's media forge.

The job is deliberately thin — a trigger and a monitor. The answer service
does the work server-side (decision #44, option 3): it downloads the stock
PVE ISO (checksum-verified, cached), runs `proxmox-auto-install-assistant`
against ITS OWN runtime identity (URL + cert fingerprint — never job inputs,
so mismatched media is structurally impossible), publishes to the firmware
storage, and registers a **Staged** SoftwareVersion + ImageFile. A human
promotes Staged -> Active after a validation install, same as every image.

The service is found via an **ExternalIntegration** named
``nfv-answer-service`` (remote URL + a SecretsGroup carrying the admin
bearer as Generic/Token). The forge is DISABLED by default
(ADMIN_ENABLED=false) — enable it only on the lab/build instance.
"""

import time

import requests

from nautobot.apps.jobs import BooleanVar, Job, StringVar, register_jobs
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
from nautobot.extras.models import ExternalIntegration

INTEGRATION_NAME = "nfv-answer-service"


class PrepareInstallerMedia(Job):
    class Meta:
        name = "Prepare Installer Media (Media Forge)"
        description = (
            "Asks the answer service to prepare (and publish + register, "
            "Staged) installer media bound to its own URL/cert identity. "
            "Needs the ExternalIntegration 'nfv-answer-service' and a forge "
            "with ADMIN_ENABLED=true (lab instances only)."
        )
        has_sensitive_variables = False
        soft_time_limit = 3600
        time_limit = 4200

    release = StringVar(
        label="PVE release",
        description="Stock installer release, e.g. 9.2-1 (fetched from the "
                    "official ISO mirror, SHA256SUMS-verified, cached)",
        default="9.2-1",
    )
    include_pxe = BooleanVar(
        label="Also produce the PXE/iPXE artifact set",
        default=False,
    )
    version_override = StringVar(
        label="SoftwareVersion override",
        description="Optional; default is '<release>-auto'. Registration "
                    "fail-closes on an existing version rather than "
                    "re-pointing it — use this to mint a new one.",
        default="",
        required=False,
    )

    def _forge(self):
        integration = ExternalIntegration.objects.filter(name=INTEGRATION_NAME).first()
        if integration is None:
            raise RuntimeError(
                f"ExternalIntegration {INTEGRATION_NAME!r} not found. Create it: "
                "Extensibility -> External Integrations -> Add — remote URL = the "
                "answer service base (e.g. https://svc:8800), verify SSL off for "
                "a self-signed cert, and a Secrets Group carrying the admin "
                "bearer as Access type Generic / Secret type Token."
            )
        if integration.secrets_group is None:
            raise RuntimeError(
                f"{INTEGRATION_NAME!r} has no Secrets Group — attach one with "
                "the admin bearer (Generic/Token)."
            )
        token = integration.secrets_group.get_secret_value(
            access_type=SecretsGroupAccessTypeChoices.TYPE_GENERIC,
            secret_type=SecretsGroupSecretTypeChoices.TYPE_TOKEN,
        )
        session = requests.Session()
        session.verify = integration.verify_ssl
        session.headers["Authorization"] = f"Bearer {token}"
        return integration.remote_url.rstrip("/"), session

    def run(self, release, include_pxe, version_override):
        base, session = self._forge()

        info = session.get(f"{base}/info", timeout=15).json()
        self.logger.info(
            "Forge identity: answers at %s, fingerprint %s",
            info.get("public_url"), (info.get("cert_fingerprint") or "(none)")[:16],
        )
        if not info.get("admin_enabled"):
            raise RuntimeError(
                "This answer service has the media forge DISABLED "
                "(ADMIN_ENABLED=false — the correct posture for field "
                "instances). Enable it on the lab/build instance and re-run."
            )

        payload = {"release": release.strip(), "pxe": bool(include_pxe)}
        if (version_override or "").strip():
            payload["version"] = version_override.strip()
        resp = session.post(f"{base}/admin/prepare", json=payload, timeout=30)
        if resp.status_code == 401:
            raise RuntimeError("Forge refused the admin token — check the "
                              "integration's Secrets Group value.")
        resp.raise_for_status()
        task = resp.json()["task"]
        self.logger.info("Prepare task %s started on the forge", task[:8])

        seen = 0
        deadline = time.time() + 3300
        while time.time() < deadline:
            status = session.get(f"{base}/admin/prepare/{task}", timeout=30).json()
            for line in status["progress"][seen:]:
                self.logger.info("forge: %s", line)
            seen = len(status["progress"])
            if status["state"] == "error":
                raise RuntimeError(f"Forge task failed: {status['error']}")
            if status["state"] == "success":
                result = status["result"]
                for f in result["files"]:
                    self.logger.info(
                        "artifact %s  sha256=%s  %s",
                        f["name"], f["sha256"][:16],
                        f.get("download_url", "(not published)"),
                    )
                note = result.get("register_note", "")
                if result.get("registered"):
                    return (
                        f"{note}. Promote Staged -> Active in the lab and "
                        "validate one install (rollback = flip the previous "
                        "version back to Active)."
                    )
                self.logger.warning("Not auto-registered: %s", note)
                return (
                    f"Media prepared and {'published' if result['published'] else 'left on the forge'}; "
                    f"registration: {note}"
                )
            time.sleep(10)
        raise RuntimeError("Timed out waiting for the forge task — check the "
                          "answer service log; the task may still complete.")


register_jobs(PrepareInstallerMedia)
