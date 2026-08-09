"""Authenticode signature verification for executables we are about to run.

The one place this repo downloads something and then EXECUTES it is the
Ollama installer (`scripts/ensure_ollama.py`). Every other download is
pinned by sha256 (`lib/pinned_fetch.py`), which is not available here: the
installer URL is deliberately unversioned so a machine installing months
from now gets a current Ollama, and a digest would go stale on exactly
that schedule. A publisher signature is the pin that survives the vendor
shipping new builds.

Verification runs through PowerShell's `Get-AuthenticodeSignature` rather
than `WinVerifyTrust` via ctypes. Both answer "is the signature trusted";
only the former also hands back the signer's certificate subject without a
second round of `CryptQueryObject` marshalling, and the signer identity is
half the check — a validly-signed binary from someone else is exactly the
attack this is here to refuse.

Fails CLOSED. Every uncertain outcome — PowerShell missing, a timeout,
unparseable output, no configured expected signer — is a refusal, because
the caller's next step is running the file.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT_S = 60.0

# The path is handed over as an environment variable, never interpolated
# into the script text: it is the one attacker-influenced value in this
# call, and `-Command` would otherwise parse whatever it contained.
_PATH_ENV = "TESSERACT_AUTHENTICODE_TARGET"

_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    f"$sig = Get-AuthenticodeSignature -LiteralPath $env:{_PATH_ENV}; "
    "[pscustomobject]@{ "
    "status = $sig.Status.ToString(); "
    "subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }; "
    "thumbprint = if ($sig.SignerCertificate) { $sig.SignerCertificate.Thumbprint } else { '' } "
    "} | ConvertTo-Json -Compress"
)


@dataclass(frozen=True)
class SignatureVerdict:
    """`trusted` is the answer; the rest is what to say in the log.

    `status` is Authenticode's own verdict string (`Valid`, `NotSigned`,
    `HashMismatch`, `UnknownError`…) and `subject` the signer certificate's
    full distinguished name, both carried so a refusal names which half
    failed instead of just declining."""

    trusted: bool
    status: str
    subject: str
    reason: str


def _refuse(reason: str, status: str = "", subject: str = "") -> SignatureVerdict:
    return SignatureVerdict(trusted=False, status=status, subject=subject, reason=reason)


def _probe(path: Path) -> tuple[str, str] | SignatureVerdict:
    """Ask Windows about the file's signature. Returns (status, subject)."""
    env = {**os.environ, _PATH_ENV: str(path)}
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _refuse(f"could not run Get-AuthenticodeSignature: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        return _refuse(f"Get-AuthenticodeSignature failed: {detail}")
    try:
        parsed = json.loads(proc.stdout or "")
    except ValueError:
        return _refuse("Get-AuthenticodeSignature returned unreadable output")
    if not isinstance(parsed, dict):
        return _refuse("Get-AuthenticodeSignature returned an unexpected shape")
    return str(parsed.get("status") or ""), str(parsed.get("subject") or "")


def _split_rdns(subject: str) -> list[str]:
    """Split a distinguished name on its top-level commas.

    Values containing commas arrive quoted (`O="Foo, Bar"`) or escaped
    (`O=Foo\\, Bar`), and a naive `split(",")` would tear those in half —
    which is how a value ends up looking like a separate field.
    """
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in subject:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _subject_field(subject: str, key: str) -> str | None:
    """The value of one RDN out of a certificate subject.

    Returns None when the field is absent. Only the first occurrence is
    considered: a subject carrying two `O=` fields is malformed, and
    picking whichever one matched would be the bug this function exists
    to prevent.
    """
    wanted = key.strip().casefold()
    for part in _split_rdns(subject):
        name, sep, value = part.partition("=")
        if sep and name.strip().casefold() == wanted:
            return value.strip()
    return None


def verify_signed_by(path: Path, expected_signer: str) -> SignatureVerdict:
    """Is `path` validly signed, by `expected_signer`?

    `expected_signer` is one RDN of the certificate subject, written as
    `<field>=<value>` (`O=Ollama`). That field is parsed out of the subject
    and compared for equality, case-insensitively.

    Matching one parsed field rather than searching the subject string is
    the difference between a check and a suggestion. A raw substring test
    passes on `O=Ollama Software Solutions LLC`, and passes again on
    `CN=Attacker, OU=O=Ollama, O=Attacker LLC` — the literal text can be
    smuggled into any free-text field the issuing CA never verified.

    Pinning the field rather than the whole DN is still deliberate: issuers
    reorder RDNs and reissue certificates, and a config pinned to an exact
    distinguished name would refuse a genuine installer at the first
    renewal. An organisation *renaming* itself should stop the install and
    ask the operator, which is what this does.
    """
    if sys.platform != "win32":
        return _refuse("Authenticode is Windows-only")
    wanted = (expected_signer or "").strip()
    if not wanted:
        # Config-is-authority: an absent signer means nobody decided who
        # may sign this, and the answer to that is not "anyone".
        return _refuse("no expected signer configured")
    if not path.is_file():
        return _refuse(f"{path} is not a file")

    probed = _probe(path)
    if isinstance(probed, SignatureVerdict):
        return probed
    status, subject = probed

    if status != "Valid":
        return _refuse(
            f"signature status is {status or 'unknown'}, not Valid", status, subject
        )

    field, sep, wanted_value = wanted.partition("=")
    if not sep or not field.strip() or not wanted_value.strip():
        return _refuse(
            f"expected signer {wanted!r} is not of the form '<field>=<value>'",
            status,
            subject,
        )
    actual = _subject_field(subject, field)
    if actual is None:
        return _refuse(
            f"certificate subject has no {field.strip()} field: {subject or 'empty'}",
            status,
            subject,
        )
    if actual.casefold() != wanted_value.strip().casefold():
        return _refuse(
            f"{field.strip()} is {actual!r}, not {wanted_value.strip()!r}",
            status,
            subject,
        )
    return SignatureVerdict(trusted=True, status=status, subject=subject, reason="")
