"""
Pure helpers for reading a published image "version set" (the qcow2 +
`.sha256` sidecar + `.manifest.json` convention both image tracks publish —
docs/image-lifecycle.md). Stdlib-only and Nautobot-free so the parsing and
coherence rules are unit-testable without a Nautobot install
(tests/test_published_set.py).
"""

import json
import posixpath
import re
from urllib.parse import urlsplit

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PublishedSetError(ValueError):
    """A published version set is missing, malformed, or self-contradictory."""


def derive_set_urls(download_url):
    """From the artifact URL, derive (basename, sidecar_url, manifest_url).

    Follows the publish convention: `<name>.qcow2` + `<name>.qcow2.sha256`
    + `<name>.manifest.json` side by side.
    """
    parts = urlsplit(download_url)
    if parts.query or parts.fragment:
        raise PublishedSetError(
            "artifact URL must be a plain file URL (no query string or fragment) — "
            "the sidecar/manifest URLs are derived by suffixing it"
        )
    filename = posixpath.basename(parts.path)
    if not filename.endswith(".qcow2"):
        raise PublishedSetError(
            f"expected a .qcow2 artifact URL, got '{filename or download_url}'"
        )
    base = filename[: -len(".qcow2")]
    prefix = download_url[: len(download_url) - len(filename)]
    return filename, f"{download_url}.sha256", f"{prefix}{base}.manifest.json"


def parse_sidecar(text, expected_filename):
    """Parse sha256sum-format sidecar text; return the lowercase hash.

    Accepts the binary-mode `*` filename prefix. Fails closed if the hash is
    not 64 hex chars or the sidecar names a different file (wrong sidecar).
    """
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    fields = line.split()
    if len(fields) < 2:
        raise PublishedSetError(f"sidecar is not in 'sha256  filename' format: '{line}'")
    sha = fields[0].lower()
    named = fields[1].lstrip("*")
    if not _SHA256_RE.match(sha):
        raise PublishedSetError(f"sidecar hash is not a sha256: '{fields[0]}'")
    if named != expected_filename:
        raise PublishedSetError(
            f"sidecar names '{named}' but the artifact is '{expected_filename}' — wrong sidecar"
        )
    return sha


def parse_manifest(text):
    """Parse manifest JSON into a dict (must be a JSON object)."""
    try:
        manifest = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PublishedSetError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PublishedSetError("manifest is not a JSON object")
    return manifest


def resolve_field(label, manifest_value, input_value):
    """One value from manifest and/or operator input: both → must agree;
    neither → fail; else whichever exists. Returns the resolved value."""
    if manifest_value is not None and not isinstance(manifest_value, str):
        raise PublishedSetError(f"manifest {label} is not a string: {manifest_value!r}")
    manifest_value = (manifest_value or "").strip() or None
    input_value = (input_value or "").strip() or None
    if manifest_value and input_value and manifest_value != input_value:
        raise PublishedSetError(
            f"{label} conflict: manifest says '{manifest_value}', input says '{input_value}'"
        )
    resolved = manifest_value or input_value
    if not resolved:
        raise PublishedSetError(
            f"{label} is in neither the manifest nor the job input — provide it"
        )
    return resolved


def check_coherence(manifest, sidecar_sha, artifact_size):
    """Cross-check manifest claims against the sidecar hash and the served
    artifact size. Manifest fields are optional (template-track manifests
    carry neither); present-but-wrong is always fatal."""
    raw_sha = manifest.get("sha256")
    if raw_sha is not None and not isinstance(raw_sha, str):
        raise PublishedSetError(f"manifest sha256 is not a string: {raw_sha!r}")
    manifest_sha = (raw_sha or "").lower() or None
    if manifest_sha and manifest_sha != sidecar_sha:
        raise PublishedSetError(
            f"manifest sha256 ({manifest_sha}) != sidecar sha256 ({sidecar_sha}) — "
            "the set is self-contradictory; republish it"
        )
    manifest_size = manifest.get("size_bytes")
    if manifest_size is not None and artifact_size is not None:
        try:
            sizes_differ = int(manifest_size) != int(artifact_size)
        except (TypeError, ValueError) as exc:
            raise PublishedSetError(f"manifest size_bytes is not an integer: {manifest_size!r}") from exc
        if sizes_differ:
            raise PublishedSetError(
                f"manifest size_bytes ({manifest_size}) != served artifact size ({artifact_size}) — "
                "truncated upload or stale manifest; republish the set"
            )
