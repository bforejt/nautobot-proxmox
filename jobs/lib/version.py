"""
The one repo version both halves carry (decision #56), and the strict parser
the handshake uses.

The jobs reach Nautobot through the Git sync; the answer service reaches the
composer as a published image pinned by tag. Nothing forces the two to move
together, so each half states what it is and the oldest counterpart it
accepts, and the jobs refuse before touching a BMC when the pair is out of
step (jobs/lib/answer_service.py `version_handshake`). Keep JOBS_VERSION
equal to bmc/answer_service/VERSION — the tag workflow refuses a tag whose
two copies differ.

Stdlib only; importable by file path.
"""

import re

JOBS_VERSION = "0.1.0"                 # == bmc/answer_service/VERSION (one repo version)
MIN_ANSWER_SERVICE_VERSION = "0.1.0"   # oldest service these jobs accept

# Strict on purpose: an optional leading `v`, three integers, nothing else.
# `-dev` / `+build` suffixes are NOT versions here — unparseable means too
# old (fail closed), never "probably fine". `\Z`, not `$`: `$` also matches
# before a trailing newline, which would let "0.1.0\n" through (and into the
# log line) — same anchor as app.py's own VERSION check.
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)\Z")


def parse_version(text):
    """-> (major, minor, patch), or None when `text` is not a usable version
    (not a string, empty, a suffix, `latest`, ...). None = fail closed."""
    if not isinstance(text, str):
        return None
    match = VERSION_RE.match(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())
