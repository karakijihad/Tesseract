"""ANSI-strip + secret-scrub utilities shared by the PTY viewer substrate.

P4 prune (2026-07-04): the end-of-turn detector state machines
(``ClaudeStreamJsonDetector`` / ``CodexPromptIdleDetector`` /
``ClaudePromptIdleDetector`` / ``PermissionPromptWatcher``) were removed
with the TARS-drives-PTY tools (``pty_open`` / ``pty_collect_result`` /
the ``_PtyLaneAdapter`` lane transport) — nothing drives a PTY pane
autonomously anymore, so nothing needs to detect a turn's end.
``strip_ansi`` survives: `mirror/server/pty_manager.py` uses it to decode
the same byte stream the operator sees, matching
``brain/observation_transcript.py``.

Phase 5 Task 3 (2026-07-05) — ``scrub_secrets`` added alongside it. Both
are pure PTY-byte-stream transforms with no state, so both live here
rather than a new sibling module; `pty_manager.py::_forward_to_observer`
calls ``scrub_secrets`` at the observer CAPTURE point (before the chunk
ever leaves pty_manager), same import site as ``strip_ansi``.
"""

from __future__ import annotations

import re

from tesseract.lib.secret_patterns import CREDENTIAL_PATTERNS

# Mirrors `brain/observation_transcript.py` so observer + PTY substrate
# decode the same byte stream identically. Drift here would mean the
# operator's terminal view and the observer see different text.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")
_ANSI_OTHER_RE = re.compile(r"\x1b[@-_]")


def strip_ansi(text: str) -> str:
    out = _ANSI_OSC_RE.sub("", text)
    out = _ANSI_CSI_RE.sub("", out)
    out = _ANSI_OTHER_RE.sub("", out)
    return out


_REDACTED = "[redacted]"

# Provider-prefixed API keys / tokens — whole match dropped since the
# prefix itself is already the secret signal. Shared with the production-tree
# secret scanners (see `tesseract/lib/secret_patterns.py` docstring).
_SECRET_PREFIX_RES: tuple[re.Pattern[str], ...] = CREDENTIAL_PATTERNS
# `Bearer <token>` — keep the scheme name as a hint, drop the token.
_BEARER_RE = re.compile(r"\b(Bearer)\s+\S+")
# `key=value` / `token: value` / `password ...` assignments — keep the
# key name + separator, drop the value.
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|secret|password|passwd|pwd|token|key)"
    r"(\s*[:=]\s*)\S+"
)
# Long high-entropy base64/hex runs (hex is a subset of the base64
# alphabet, so one pattern covers both) — single-line minimal scrub, not
# a real entropy calculation. Known false-positive tradeoff: this also
# redacts benign long hex/base64-shaped output (full git SHAs, Docker
# digests) — accepted per the brief's "minimal best-effort" scope.
_HIGH_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")


def scrub_secrets(text: str) -> str:
    """Best-effort redaction of common secret shapes.

    Applied at the observer CAPTURE point (`pty_manager.py::
    _forward_to_observer`) before raw PTY text reaches
    `observer.observe_incremental`. Not a substitute for not leaking
    secrets into a terminal in the first place — covers provider-
    prefixed API keys, AWS access key ids, Bearer auth headers, key/
    token/password assignments, and long high-entropy base64/hex runs.
    """
    if not text:
        return text
    out = text
    for pattern in _SECRET_PREFIX_RES:
        out = pattern.sub(_REDACTED, out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", out)
    out = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", out)
    out = _HIGH_ENTROPY_RE.sub(_REDACTED, out)
    return out


__all__ = ["strip_ansi", "scrub_secrets"]
