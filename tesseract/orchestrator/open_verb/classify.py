"""What kind of thing is this string? Pure — no network, one filesystem stat.

The ladder is deterministic and first-match-wins, so the same target always
classifies the same way given the same disk and config. Order is load-bearing:
an existing path outranks every interpretation below it, because a caller who
names a real file never meant a web search.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping
from urllib.parse import SplitResult, urlsplit, urlunsplit

from tesseract.config.open_verb import FORBIDDEN_LAUNCH_EXTENSIONS
from tesseract.orchestrator.open_verb import suffixes


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

# Every suffix the cockpit can render, so a filename is never mistaken for
# a hostname. Shared with `resolve` — maintaining two copies is how they
# drifted before.
_TEXT_SUFFIXES = suffixes.RENDERABLE

# `.com` is both a top-level domain and a DOS executable suffix. Everywhere
# else the two vocabularies are disjoint, so this is the single carve-out:
# `example.com` is a hostname, and a real `foo.com` executable on disk is
# caught by the existence check that runs before any of this.
_TLD_COLLISION = ".com"


def _file_shaped_suffixes(launch_extensions: frozenset[str]) -> frozenset[str]:
    known = launch_extensions | FORBIDDEN_LAUNCH_EXTENSIONS | _TEXT_SUFFIXES
    return known - {_TLD_COLLISION}


def _without_userinfo(raw: str, split: SplitResult) -> str:
    """Drop `user:password@` from a URL's authority, keeping everything else.

    Rebuilt from `netloc` rather than `hostname`: the latter lowercases and
    strips the brackets an IPv6 literal needs, so `https://[::1]:8080/` would
    come back as the unparseable `https://::1:8080/`.
    """
    netloc = split.netloc
    if "@" not in netloc:
        return raw
    # Only the last `@` separates userinfo from the host; one may legally
    # appear inside the userinfo itself.
    host_part = netloc.rsplit("@", 1)[1]
    return urlunsplit(
        (split.scheme, host_part, split.path, split.query, split.fragment)
    )


_DRIVE_REMOTE = 4


def _is_network_drive(target: str) -> bool:
    """True when `target` starts with a drive letter mapped to a remote share.

    Windows-only and deliberately fail-open: if the API is unavailable or
    errors, this returns False and the target is handled as an ordinary path.
    Failing closed would refuse every local drive on any platform where the
    call does not exist.
    """
    if not sys.platform.startswith("win"):
        return False
    if not re.match(r"^[A-Za-z]:[\\/]", target):
        return False
    try:
        import ctypes

        drive = f"{target[0]}:\\"
        return ctypes.windll.kernel32.GetDriveTypeW(drive) == _DRIVE_REMOTE
    except Exception:  # noqa: BLE001 — an unavailable API is not a refusal
        return False


def _looks_like_a_path(target: str) -> bool:
    """A separator or a drive designator means the caller named a location,
    whether or not anything is there."""
    return (
        "/" in target
        or "\\" in target
        or re.match(r"^[A-Za-z]:", target) is not None
        or target.startswith("~")
    )


class Intent(StrEnum):
    """How the caller wants the target read.

    The ladder below is first-match-wins and one of its rungs is "something
    exists at this name", so the same string can mean different things on
    different days: a file called `bbc.co.uk` in the working directory makes
    that string a document rather than a site, and deleting it makes it a
    site again. A caller that already knows what it meant should not have to
    win a guessing game against the filesystem.

    `AUTO` is the ladder, unchanged. Every other value pins one rung. None
    of them can reach past the safety rungs — a pinned intent chooses among
    readings, it never turns a refusal into an open.
    """

    AUTO = "auto"
    PATH = "path"
    URL = "url"
    APP = "app"
    SEARCH = "search"


def _as_url(raw: str) -> Classification:
    """Read `raw` as a web address, adding the scheme a bare domain omits."""
    split = urlsplit(raw)
    if split.scheme.lower() in _ALLOWED_SCHEMES:
        if not split.hostname:
            return Classification(
                TargetKind.REFUSED, raw, f"{raw!r} has no host to open"
            )
        return Classification(
            TargetKind.URL, _without_userinfo(raw, split), "explicit http(s) url"
        )
    if split.scheme:
        return Classification(
            TargetKind.REFUSED,
            raw,
            f"{split.scheme}: is not a scheme this can open",
        )
    if not _DOMAIN_RE.match(raw):
        return Classification(
            TargetKind.REFUSED, raw, f"{raw!r} is not shaped like a web address"
        )
    return Classification(TargetKind.DOMAIN, f"https://{raw}", "a bare domain")


def _pinned(
    intent: Intent,
    raw: str,
    *,
    apps: Mapping[str, str],
) -> Classification:
    """Classify against one rung, chosen by the caller instead of guessed."""
    if intent is Intent.SEARCH:
        # Unconditional: the whole point is that a phrase which happens to
        # name a file on this machine is still a phrase.
        return Classification(TargetKind.QUERY, raw, "a search phrase")

    if intent is Intent.URL:
        return _as_url(raw)

    if intent is Intent.APP:
        match = _lookup_app(raw, apps)
        if match is None:
            return Classification(
                TargetKind.REFUSED,
                raw,
                f"{raw!r} is not a configured application — add it to "
                f"`open_verb.yaml::apps` or open it as a path",
            )
        return Classification(TargetKind.APP, match, "a known application")

    expanded = Path(raw).expanduser()
    try:
        exists = expanded.exists()
    except OSError:
        exists = False
    if not exists:
        # Same sentence the ladder gives, for the same reason: a path that
        # is not there is worth saying out loud rather than reinterpreting.
        return Classification(
            TargetKind.AMBIGUOUS,
            raw,
            f"{raw!r} was opened as a path but nothing is there",
        )
    return Classification(TargetKind.PATH, str(expanded.resolve()), "an existing path")


def classify(
    target: str,
    *,
    apps: Mapping[str, str],
    launch_extensions: frozenset[str] = frozenset(),
    intent: Intent = Intent.AUTO,
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
            # `http://` and `https:///a` carry no host. Left to the probe they
            # surface as an httpx error rather than as something the operator
            # can act on, so they end here with a sentence instead.
            if not split.hostname:
                return Classification(
                    TargetKind.REFUSED, raw, f"{raw!r} has no host to open"
                )
            # `user:password@` is stripped here, once, so no credential reaches
            # a persisted surface, the tool metadata or the audit record.
            # Nothing is lost: browsers have dropped userinfo in URLs, so it
            # would not have authenticated anything anyway.
            return Classification(
                TargetKind.URL, _without_userinfo(raw, split), "explicit http(s) url"
            )
        # A bare Windows drive (`C:\x`) parses as scheme "c" — not a scheme.
        is_drive = re.match(r"^[A-Za-z]:[\\/]", raw) is not None
        if not is_drive and (raw[len(scheme) + 1 :].startswith("//") or scheme in _REFUSED_SCHEMES):
            return Classification(
                TargetKind.REFUSED,
                raw,
                f"{scheme}: is not a scheme this can open",
            )

    # A UNC path must never be stat'd. `Path("\\\\host\\share").exists()` opens
    # an SMB connection, and Windows will hand the operator's NTLM credentials
    # to whatever answers — so a target that merely *reads* like a share is a
    # credential-theft primitive when it arrives from tool-supplied content.
    if raw.startswith("\\\\") or raw.startswith("//"):
        return Classification(
            TargetKind.REFUSED, raw, "network paths are not opened"
        )

    # A mapped drive letter reads like a local volume but resolves over SMB, so
    # stat'ing it authenticates exactly as a UNC path would. `GetDriveTypeW` is
    # a local API call against the mount table — it answers without touching
    # the network, which is what makes this checkable at all.
    if _is_network_drive(raw):
        return Classification(
            TargetKind.REFUSED, raw, "network paths are not opened"
        )

    # Every rung above is a refusal, and they all run before the intent is
    # consulted. A caller may say how to READ a target; it may not say that a
    # UNC path, a mapped drive or a `javascript:` scheme is fine after all.
    if intent is not Intent.AUTO:
        return _pinned(intent, raw, apps=apps)

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
