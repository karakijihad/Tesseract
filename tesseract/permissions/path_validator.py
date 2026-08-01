"""Path validator — 12 attack vector validation.

Two-pass validation: string containment checks (fast, no I/O) then
Path.resolve() for symlink/normalization (requires filesystem access).

Returns (is_valid, violation_message). On failure, the message says
WHAT was blocked but not HOW to bypass it.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

# Windows reserved device names (case-insensitive)
_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

_MAX_PATH_LENGTH = 32767  # Windows MAX_PATH extended
_MAX_COMPONENT_LENGTH = 255


def _is_within(candidate: str, root: str) -> bool:
    """Segment-aware containment.

    A bare `startswith` accepts a sibling whose name merely extends the root's
    (`…/homework` under `…/home`). The install layout puts `app/`, `home/`, and
    `runtime/` side by side under one parent, so that near-miss is now a way
    past the write boundary rather than a curiosity.
    """
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def validate_path(
    raw_path: str,
    *,
    write_root: str,
    read_root: str,
    mode: Literal["read", "write"] = "write",
    resolve_symlinks: bool = True,
) -> tuple[bool, str]:
    """Validate a path against 12 attack vectors under two boundaries.

    `read_root` is the install root — `app/`, `home/`, and `runtime/` are all
    readable, because a sealed tree should still be legible to the agent that
    runs inside it. `write_root` is `home/`, so the seal on `app/` needs no
    special case: it is simply outside write authority.

    Returns (True, "") if valid, (False, reason) if blocked.
    """
    # --- Pass 1: String-level checks (no I/O) ---

    # Vector 1: Null bytes
    if "\x00" in raw_path:
        return False, "null byte in path"

    # Vector 2: URL-encoded traversals
    decoded = unquote(raw_path)
    if decoded != raw_path and (".." in decoded or "/" in decoded.replace(raw_path, "")):
        return False, "URL-encoded path components"

    # Vector 3: Double-encoding
    double_decoded = unquote(decoded)
    if double_decoded != decoded and ".." in double_decoded:
        return False, "double-encoded path traversal"

    # Vector 4: Unicode normalization attacks
    normalized = unicodedata.normalize("NFC", raw_path)
    if normalized != raw_path:
        # Check if normalization changes path semantics
        if ".." in normalized or "/" in normalized.replace("\\", "/"):
            return False, "unicode normalization changes path meaning"

    # Check for unicode whitespace that could be confused with separators
    for char in raw_path:
        if unicodedata.category(char) in ("Zs", "Zl", "Zp") and char != " ":
            return False, "unicode whitespace in path"

    # Vector 5: UNC paths (Windows)
    if raw_path.startswith("\\\\") or raw_path.startswith("//"):
        return False, "UNC path blocked"

    # Vector 6: Tilde expansion
    if raw_path.startswith("~"):
        return False, "tilde expansion blocked"

    # Vector 7: Path length limits
    if len(raw_path) > _MAX_PATH_LENGTH:
        return False, f"path exceeds {_MAX_PATH_LENGTH} characters"

    # Check component lengths
    parts = Path(raw_path).parts
    for part in parts:
        if len(part) > _MAX_COMPONENT_LENGTH:
            return False, f"path component exceeds {_MAX_COMPONENT_LENGTH} characters"

    # Vector 8: Windows reserved device names
    if os.name == "nt":
        for part in parts:
            stem = Path(part).stem.upper()
            if stem in _WINDOWS_RESERVED:
                return False, f"Windows reserved device name: {stem}"

    # --- Pass 2: Filesystem-level checks ---

    # Which root applies depends on what the caller is about to do with the
    # path — including how a relative path is anchored.
    boundary = write_root if mode == "write" else read_root

    try:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(boundary) / path

        if resolve_symlinks:
            resolved = path.resolve()
        else:
            resolved = path.absolute()

        boundary_resolved = Path(boundary).resolve()
    except (OSError, ValueError) as e:
        return False, f"path resolution error: {e}"

    # Vector 9: Relative path traversal (../)
    # Checked after resolution — catches symlinks that escape
    resolved_str = str(resolved)
    boundary_str = str(boundary_resolved)

    # Vector 10: Root or near-root paths
    if len(resolved.parts) <= 2:
        # e.g., C:\ or / — too close to root
        return False, "path too close to filesystem root"

    # Vector 11: Windows drive-root
    if os.name == "nt":
        drive_root = re.match(r"^[A-Za-z]:\\?$", resolved_str)
        if drive_root:
            return False, "Windows drive root blocked"

    # Vector 12: Boundary check (the critical one)
    if not _is_within(resolved_str, boundary_str):
        return False, f"path outside {mode} boundary"

    # Vector 9b: Symlink resolution — double-check after resolve. Compares
    # against the same boundary, so a link inside `home/` pointing into `app/`
    # cannot smuggle a write past the seal.
    if resolve_symlinks and path.exists():
        try:
            real = path.resolve(strict=True)
            if not _is_within(str(real), boundary_str):
                return False, f"symlink resolves outside {mode} boundary"
        except OSError:
            pass  # File doesn't exist yet (write target), allow

    return True, ""
