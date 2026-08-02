"""Hand a target to the operating system.

This is the only code in the initiative that leaves the sandbox, and it is not
protected by `bash_security`: `os.startfile` invokes ShellExecute directly and
never forms a shell command for those 26 checks to inspect. Every control this
path gets, it gets here.

What this guarantees: we ShellExecute only a canonical existing object below
the read boundary, re-resolved immediately before invocation, whose type is on
a positive allowlist. What it cannot guarantee: what the registered handler
then does with the file. That gap is real and is stated in
`Docs/Plan/open-verb/DESIGN.md` rather than papered over.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

from tesseract.config.open_verb import FORBIDDEN_LAUNCH_EXTENSIONS
from tesseract.paths import home_dir, install_root
from tesseract.permissions.path_validator import validate_path


class LaunchRefused(PermissionError):
    """The target will not be handed to the OS, and why."""


class LaunchUnsupported(RuntimeError):
    """No OS handoff implemented for this platform."""


_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
# A drive designator is the one legitimate colon in a Windows path. Anything
# further right is an alternate data stream — `report.pdf:evil.exe` is a real
# file whose suffix check passes and whose contents are something else.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")


def _reject_alternate_data_streams(raw: str) -> None:
    body = raw[2:] if _DRIVE_PREFIX.match(raw) else raw
    if ":" in body:
        raise LaunchRefused(
            "path contains an alternate-data-stream marker, which names "
            "different content than the file being checked"
        )


def _reject_forbidden_suffix(path: Path) -> None:
    """Absolute, and evaluated before the allowlist. Config cannot widen this:
    the loader refuses these extensions too, so both layers must be edited to
    turn `open` into a launcher for arbitrary code — and neither will accept it."""
    if path.suffix.lower() in FORBIDDEN_LAUNCH_EXTENSIONS:
        raise LaunchRefused(
            f"{path.suffix} is an executable, script or indirection type and is "
            f"never launched"
        )


def _require_allowed_suffix(path: Path, allowed: frozenset[str]) -> None:
    if path.suffix.lower() not in allowed:
        raise LaunchRefused(
            f"{path.suffix or 'files with no extension'} is not on the launch "
            f"allowlist"
        )


def _reject_reparse_points(path: Path) -> None:
    """A junction or symlink anywhere in the chain can redirect the final
    target after validation resolved it. Refuse the whole shape rather than
    reason about which hop is safe."""
    if not sys.platform.startswith("win"):
        return
    for candidate in (path, *path.parents):
        try:
            attrs = candidate.lstat().st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            continue
        if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:  # type: ignore[attr-defined]
            raise LaunchRefused(
                f"{candidate} is a reparse point; the path it resolves to can "
                f"change between validation and launch"
            )


def _require_inside_read_boundary(raw: str) -> tuple[bool, str]:
    return validate_path(
        raw,
        write_root=str(home_dir()),
        read_root=str(install_root()),
        mode="read",
        resolve_symlinks=True,
    )


def launch_path(
    path: str,
    *,
    allowed_extensions: frozenset[str],
) -> Path:
    """Guard chain, in order. Each rung assumes the ones above it have run."""
    raw = str(path)
    # String-level checks first, then the boundary — so nothing outside the
    # read root is ever stat'd, and a refusal leaks no filesystem existence.
    _reject_alternate_data_streams(raw)

    candidate = Path(raw).expanduser()
    _reject_forbidden_suffix(candidate)

    ok, reason = _require_inside_read_boundary(str(candidate))
    if not ok:
        raise LaunchRefused(reason)

    _reject_reparse_points(candidate)

    if not candidate.exists():
        raise LaunchRefused(f"nothing exists at {candidate}")
    if candidate.is_dir():
        # Directories are legitimate, but they reach Explorer through the same
        # call and must not inherit the file allowlist's reasoning.
        raise LaunchRefused("use launch_directory for a folder")

    _require_allowed_suffix(candidate, allowed_extensions)

    # Re-resolve immediately before the call: everything above ran against a
    # path that could have been replaced in the meantime.
    final = candidate.resolve()
    _reject_forbidden_suffix(final)
    ok, reason = _require_inside_read_boundary(str(final))
    if not ok:
        raise LaunchRefused(f"target moved outside the read boundary: {reason}")

    _shell_execute(str(final))
    return final


def launch_directory(path: str) -> Path:
    raw = str(path)
    _reject_alternate_data_streams(raw)
    candidate = Path(raw).expanduser()

    ok, reason = _require_inside_read_boundary(str(candidate))
    if not ok:
        raise LaunchRefused(reason)
    _reject_reparse_points(candidate)
    if not candidate.is_dir():
        raise LaunchRefused(f"not a directory: {candidate}")

    final = candidate.resolve()
    ok, reason = _require_inside_read_boundary(str(final))
    if not ok:
        raise LaunchRefused(f"target moved outside the read boundary: {reason}")

    _shell_execute(str(final))
    return final


def launch_url(url: str) -> str:
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise LaunchRefused(f"{scheme or 'a scheme-less string'} is not an openable url")
    _shell_execute(url)
    return url


def _shell_execute(target: str) -> None:
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise LaunchUnsupported(
            f"no OS handoff implemented for {sys.platform}; the desktop build "
            f"targets Windows"
        )
    startfile(target)
