"""
Nautobot Job: register a published image version set as a Staged
SoftwareVersion + SoftwareImageFile (decision #37's register-vendor-image
job — works for any published set following the version-set convention,
vendor-sealed appliance images and Ubuntu template builds alike).

The job takes the artifact URL and reads the REST of the truth from the
published set itself — the `.sha256` sidecar (hash authority) and
`manifest.json` (platform/version/size metadata where the track provides
them) — so the error-prone values (64-char checksum, byte size) are never
hand-typed. It verifies the artifact is actually being served (size check
against the server; optional full re-hash) before creating anything.

Create-only and idempotent: an existing matching registration is a no-op; a
same-name registration with a DIFFERENT checksum or URL is a hard refusal
(version-label collision or corruption — same fail-closed rule as the media
forge). Existing records' status is never touched (an Active version stays
Active).
"""

import hashlib

import requests
from nautobot.apps.jobs import BooleanVar, Job, ObjectVar, StringVar, register_jobs
from nautobot.dcim.models import Platform, SoftwareImageFile, SoftwareVersion
from nautobot.extras.models import Status

from ..lib.published_set import (
    PublishedSetError,
    check_coherence,
    derive_set_urls,
    parse_manifest,
    parse_sidecar,
    resolve_field,
)

HTTP_TIMEOUT = 30
HASH_CHUNK = 4 * 1024 * 1024


class RegisterVendorImage(Job):
    class Meta:
        name = "Register Image from Published Set"
        description = (
            "Reads a published version set (qcow2 + .sha256 + manifest.json) from the "
            "firmware server and registers it as a Staged SoftwareVersion + "
            "SoftwareImageFile. Checksum/size come from the set, never hand-typed. "
            "Create-only, idempotent, refuses checksum collisions."
        )
        has_sensitive_variables = False
        # full_verify streams the whole artifact through the worker (minutes
        # for multi-GB images) — same budget as the ingest job's node pull.
        soft_time_limit = 1500
        time_limit = 1800

    download_url = StringVar(
        label="Artifact URL",
        description="Full URL of the published .qcow2 (its .sha256 and .manifest.json siblings are read automatically)",
    )
    platform = ObjectVar(
        model=Platform,
        label="Platform",
        required=False,
        description="Only needed when the manifest carries no platform (template-track sets); must match when both exist",
    )
    version = StringVar(
        label="Version label",
        required=False,
        description="Only needed when the manifest carries no version_label (template-track sets); must match when both exist",
    )
    full_verify = BooleanVar(
        label="Full verify (re-hash the artifact)",
        default=False,
        description="Stream the artifact and recompute its sha256 against the sidecar (minutes for multi-GB images). Off = size check only.",
    )

    def _fetch_text(self, url, what):
        try:
            resp = requests.get(url, timeout=HTTP_TIMEOUT)
        except requests.exceptions.SSLError as exc:
            raise ValueError(
                f"TLS verification failed fetching the {what} ({url}): {exc} — "
                "the worker must trust the firmware server's certificate (or serve over http)"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ValueError(f"could not fetch the {what} ({url}): {exc}") from exc
        if resp.status_code != 200:
            raise ValueError(
                f"{what} not found at {url} (HTTP {resp.status_code}) — "
                "the published set is incomplete; republish all three files"
            )
        return resp.text

    def _artifact_size(self, url):
        try:
            resp = requests.head(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
        except requests.exceptions.SSLError as exc:
            raise ValueError(
                f"TLS verification failed reaching the artifact ({url}): {exc} — "
                "the worker must trust the firmware server's certificate (or serve over http)"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ValueError(f"could not reach the artifact ({url}): {exc}") from exc
        if resp.status_code != 200:
            raise ValueError(f"artifact not found at {url} (HTTP {resp.status_code})")
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None

    def _stream_hash(self, url):
        sha = hashlib.sha256()
        try:
            with requests.get(url, timeout=HTTP_TIMEOUT, stream=True) as resp:
                if resp.status_code != 200:
                    raise ValueError(f"artifact not found at {url} (HTTP {resp.status_code})")
                for chunk in resp.iter_content(chunk_size=HASH_CHUNK):
                    sha.update(chunk)
        except requests.exceptions.SSLError as exc:
            raise ValueError(
                f"TLS verification failed streaming the artifact ({url}): {exc} — "
                "the worker must trust the firmware server's certificate (or serve over http)"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ValueError(f"full verify aborted mid-stream ({url}): {exc}") from exc
        return sha.hexdigest()

    def run(self, download_url, platform, version, full_verify):
        download_url = download_url.strip()
        try:
            filename, sidecar_url, manifest_url = derive_set_urls(download_url)
            sidecar_sha = parse_sidecar(self._fetch_text(sidecar_url, "checksum sidecar"), filename)
            manifest = parse_manifest(self._fetch_text(manifest_url, "manifest"))

            resolved_platform_name = resolve_field(
                "platform", manifest.get("platform"), platform.name if platform else None
            )
            resolved_version = resolve_field("version", manifest.get("version_label"), version)

            size = self._artifact_size(download_url)
            check_coherence(manifest, sidecar_sha, size)
        except PublishedSetError as exc:
            raise ValueError(str(exc)) from exc
        if size is None:
            self.logger.warning("Server returned no Content-Length; registering without a size check")
        self.logger.info(
            "Published set is coherent: %s sha256=%s size=%s platform=%s version=%s (provenance: %s)",
            filename, sidecar_sha, size, resolved_platform_name, resolved_version,
            manifest.get("checksum_provenance", "n/a"),
        )

        if full_verify:
            self.logger.info("Full verify: streaming and re-hashing the artifact…")
            actual = self._stream_hash(download_url)
            if actual != sidecar_sha:
                raise ValueError(
                    f"FULL VERIFY FAILED: served artifact hashes to {actual}, sidecar says "
                    f"{sidecar_sha} — corrupt upload; republish before registering"
                )
            self.logger.info("Full verify passed")

        platform_obj = platform or Platform.objects.filter(name=resolved_platform_name).first()
        if platform_obj is None:
            raise ValueError(
                f"Platform '{resolved_platform_name}' does not exist — run Bootstrap NFV Data Model first"
            )
        # Stock Nautobot ships a "Staged" status NOT scoped to software models;
        # only the bootstrap job adds those content types. Check the scoping
        # here so a fresh environment gets the friendly pointer instead of a
        # raw StatusField limit_choices_to ValidationError at save time.
        staged = Status.objects.filter(name="Staged").first()
        software_cts = {
            ct.model for ct in (staged.content_types.all() if staged else []) if ct.app_label == "dcim"
        }
        if staged is None or not {"softwareversion", "softwareimagefile"} <= software_cts:
            raise ValueError(
                "Status 'Staged' is missing or not scoped to software versions/image files — "
                "run Bootstrap NFV Data Model first"
            )

        sv = SoftwareVersion.objects.filter(platform=platform_obj, version=resolved_version).first()
        sv_created = sv is None
        if sv_created:
            sv = SoftwareVersion(platform=platform_obj, version=resolved_version, status=staged)
            sv.validated_save()
            self.logger.info("Created SoftwareVersion %s %s (Staged)", platform_obj.name, resolved_version)
        else:
            self.logger.info(
                "SoftwareVersion %s %s already exists (status %s) — leaving it alone",
                platform_obj.name, resolved_version, sv.status.name,
            )
            if sv.status.name != "Staged":
                self.logger.warning(
                    "Registering a new image file onto a %s version — only the image file "
                    "will be Staged; the version's status is untouched", sv.status.name,
                )

        # The DB uniqueness is per-version (image_file_name, software_version) —
        # so a mislabelled republish (same filename, different checksum, NEW
        # version label) would create a second record without this check, and
        # node-side ensure_image is keyed by filename without re-verifying, so
        # a warmed node would silently serve the wrong bytes.
        clash = (
            SoftwareImageFile.objects.filter(image_file_name=filename)
            .exclude(software_version=sv)
            .first()
        )
        if clash is not None and (clash.image_file_checksum or "").lower() != sidecar_sha:
            raise ValueError(
                f"REFUSED: {filename} is already registered on version "
                f"{clash.software_version.version} with a DIFFERENT checksum "
                f"({clash.image_file_checksum}) — filenames must be unique per content "
                "(nodes cache by filename); republish under a distinct filename"
            )

        existing = sv.software_image_files.filter(image_file_name=filename).first()
        if existing is not None:
            if (existing.image_file_checksum or "").lower() != sidecar_sha:
                raise ValueError(
                    f"REFUSED: {filename} is already registered on {resolved_version} with checksum "
                    f"{existing.image_file_checksum} but the published set says {sidecar_sha} — "
                    "version-label collision or corrupt republish; register a NEW version instead"
                )
            if existing.download_url != download_url:
                raise ValueError(
                    f"REFUSED: {filename} is already registered with download_url "
                    f"{existing.download_url} — refusing to silently repoint it to {download_url}"
                )
            return f"Already registered and matching: {resolved_version} / {filename} — nothing to do"

        make_default = not sv.software_image_files.filter(default_image=True).exists()
        image = SoftwareImageFile(
            software_version=sv,
            image_file_name=filename,
            image_file_checksum=sidecar_sha,
            hashing_algorithm="sha256",
            download_url=download_url,
            default_image=make_default,
            status=staged,
        )
        if size is not None:
            image.image_file_size = size
        image.validated_save()
        if not make_default:
            self.logger.warning(
                "Version %s already had a default image — %s registered as non-default",
                resolved_version, filename,
            )
        where = (
            f"new SoftwareVersion {platform_obj.name} {resolved_version} (Staged)"
            if sv_created
            else f"existing SoftwareVersion {platform_obj.name} {resolved_version} (status {sv.status.name})"
        )
        result = f"Registered {filename} ({'default' if make_default else 'non-default'}, Staged) on {where}."
        if sv_created or sv.status.name == "Staged":
            result += (
                " Promote Staged -> Active in the lab and validate one deploy — "
                "the deploy job refuses non-Active versions (that IS the gate)."
            )
        return result


register_jobs(RegisterVendorImage)
