"""What kind of thing is this string? Pure — no network, one filesystem stat.

The ladder is deterministic and first-match-wins, so the same target always
classifies the same way given the same disk and config. Order is load-bearing:
an existing path outranks every interpretation below it, because a caller who
names a real file never meant a web search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from tesseract.config.open_verb import FORBIDDEN_LAUNCH_EXTENSIONS


class TargetKind(StrEnum):
    URL = "url"
    PATH = "path"
    DOMAIN = "domain"
    APP = "app"
    QUERY = "query"
    # Path-shaped but nothing is there. Never silently downgraded to a search:
    # a typo'd filename becoming a web query is the failure that makes a verb
    # like this feel unpredictable.
    AMBIGUOUS = "ambiguous"
    # An explicit scheme we will not act on (file:, javascript:, ms-settings:).
    REFUSED = "refused"


@dataclass(frozen=True)
class Classification:
    kind: TargetKind
    canonical: str
    reason: str


_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Schemes refused even without a `//` — each one either executes something or
# reaches a local resource. Everything not named here and not written as a URI
# continues down the ladder, because `site:`, `define:` and `filetype:` are
# search operators, not protocols.
_REFUSED_SCHEMES = frozenset(
    {
        "javascript", "vbscript", "data", "file", "about", "blob",
        "ms-settings", "ms-appinstaller", "search-ms", "shell", "res",
        "chrome", "edge", "view-source", "mailto", "tel", "callto",
        "smb", "ldap", "ftp", "sftp", "telnet", "ssh",
    }
)

# `label.label[.label]`, no spaces, no separators.
_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_HAS_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,10}$")

# Text and code suffixes that are not in `launch_extensions` (they render in
# the cockpit rather than being handed to the OS) but still mark a string as
# file-shaped rather than a hostname.
_TEXT_SUFFIXES = frozenset(
    {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".rb",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".sh", ".yaml", ".yml", ".toml",
        ".ini", ".cfg", ".json", ".xml", ".html", ".htm", ".css", ".log",
        ".tsv", ".sql", ".env",
    }
)

# `.com` is both a top-level domain and a DOS executable suffix. Everywhere
# else the two vocabularies are disjoint, so this is the single carve-out:
# `example.com` is a hostname, and a real `foo.com` executable on disk is
# caught by the existence check that runs before any of this.
_TLD_COLLISION = ".com"


def _file_shaped_suffixes(launch_extensions: frozenset[str]) -> frozenset[str]:
    suffixes = launch_extensions | FORBIDDEN_LAUNCH_EXTENSIONS | _TEXT_SUFFIXES
    return suffixes - {_TLD_COLLISION}


def _looks_like_a_path(target: str) -> bool:
    """A separator or a drive designator means the caller named a location,
    whether or not anything is there."""
    return (
        "/" in target
        or "\\" in target
        or re.match(r"^[A-Za-z]:", target) is not None
        or target.startswith("~")
    )


def classify(
    target: str,
    *,
    apps: Mapping[str, str],
    launch_extensions: frozenset[str] = frozenset(),
) -> Classification:
    raw = target.strip()
    if not raw:
        return Classification(TargetKind.REFUSED, "", "empty target")

    # 1. An explicit scheme is a statement of intent — honour it or refuse it.
    #
    #    `urlsplit` calls any leading run of scheme characters before a colon a
    #    scheme, with no `//` required, so `site:reddit.com` and `define:word`
    #    parse as schemes too. Refusing on that alone would reject ordinary
    #    search operators, so a colon only ends the ladder when the string is
    #    plainly a URI (`scheme://`) or names a scheme we specifically refuse.
    #    Anything else falls through to be classified on its shape.
    split = urlsplit(raw)
    scheme = split.scheme.lower()
    if scheme:
        if scheme in _ALLOWED_SCHEMES:
            return Classification(TargetKind.URL, raw, "explicit http(s) url")
        # A bare Windows drive (`C:\x`) parses as scheme "c" — not a scheme.
        is_drive = re.match(r"^[A-Za-z]:[\\/]", raw) is not None
        if not is_drive and (raw[len(scheme) + 1 :].startswith("//") or scheme in _REFUSED_SCHEMES):
            return Classification(
                TargetKind.REFUSED,
                raw,
                f"{scheme}: is not a scheme this can open",
            )

    # 2. Something that exists outranks every guess below.
    expanded = Path(raw).expanduser()
    try:
        if expanded.exists():
            return Classification(
                TargetKind.PATH, str(expanded.resolve()), "an existing path"
            )
    except OSError:
        # A malformed path (illegal characters, too long) is not a crash — it
        # simply is not a path, so fall through to the remaining rungs.
        pass

    # 3. Hostname shape, excluding anything that reads as a filename.
    if _DOMAIN_RE.match(raw) and not _looks_like_a_path(raw):
        suffix = raw[raw.rfind(".") :].lower()
        if suffix not in _file_shaped_suffixes(launch_extensions):
            return Classification(
                TargetKind.DOMAIN, f"https://{raw}", "a bare domain"
            )

    # 4. A launchable application, by name.
    match = _lookup_app(raw, apps)
    if match is not None:
        return Classification(TargetKind.APP, match, "a known application")

    # 5. Path-shaped but absent — say so rather than guessing.
    if _looks_like_a_path(raw) or _HAS_SUFFIX_RE.search(raw):
        return Classification(
            TargetKind.AMBIGUOUS,
            raw,
            f"{raw!r} looks like a file path but nothing is there, and it is "
            f"not a known application — say what you meant, or pass a search "
            f"phrase without a file extension",
        )

    # 6. Anything left is something to look up.
    return Classification(TargetKind.QUERY, raw, "a search phrase")


def _lookup_app(raw: str, apps: Mapping[str, str]) -> str | None:
    lowered = raw.casefold()
    for name, identity in apps.items():
        if name.casefold() == lowered:
            return identity
    return None
