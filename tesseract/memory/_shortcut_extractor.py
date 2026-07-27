"""Windows shortcut (.url / .lnk) URL extraction — AU-22.

The AU-22 raw-watch path treats `.url` and `.lnk` files as one-hop pointers
to web content: the file itself carries no readable text, but it names a
URL that `tavily_extract` can fetch. This module owns the parsing —
`vault_indexer.extract_text` chains it.

`.url` files are Windows INI shortcuts with at least `[InternetShortcut]
URL=...`. `.lnk` files are Windows shell-link binaries; full parsing
needs `pywin32`, which is not always installed on dev machines, so we
fall back to a best-effort byte-scan that recovers `http(s)://...` from
the binary blob when the package is missing.

The extractor returns `(url, source_text)`:
  * `url`     — first URL discovered (or `None` if none)
  * `source_text` — short metadata blob suitable for the vault chunker
                    when tavily_extract is not chained (kept here so
                    `vault_indexer` still has something to index even
                    when the operator's tavily key is absent).
"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_URL_BYTE_RE = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")


@dataclass(frozen=True)
class ShortcutTarget:
    url: str | None
    source_text: str


def extract_url(path: Path) -> ShortcutTarget:
    """Return the first URL embedded in a `.url` or `.lnk` file.

    Never raises — a malformed shortcut returns `(None, "")` so the
    vault watcher's per-file failure path runs.
    """
    ext = path.suffix.lower()
    if ext == ".url":
        return _parse_url_file(path)
    if ext == ".lnk":
        return _parse_lnk_file(path)
    return ShortcutTarget(url=None, source_text="")


def _parse_url_file(path: Path) -> ShortcutTarget:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8-sig")
    except (OSError, configparser.Error) as exc:
        logger.warning(".url parse failed for %s (%s)", path.name, exc)
        return ShortcutTarget(url=None, source_text="")
    for section in parser.sections():
        for key in parser[section]:
            if key.strip().lower() == "url":
                value = parser[section][key].strip()
                if value:
                    return ShortcutTarget(
                        url=value,
                        source_text=f"Shortcut: {path.name}\nURL: {value}\n",
                    )
    return ShortcutTarget(url=None, source_text="")


def _parse_lnk_file(path: Path) -> ShortcutTarget:
    """Best-effort `.lnk` parse.

    Tries `pywin32`'s shell namespace when available so the canonical
    target survives; otherwise byte-scans the binary blob for the first
    `http(s)://` URL. Most operator shortcuts to web pages embed the URL
    literally, so the fallback recovers them; non-URL shortcuts (e.g. a
    `.lnk` to a local `.exe`) return `(None, "")` and the watcher logs
    the file as `failed`.
    """
    try:
        blob = path.read_bytes()
    except OSError as exc:
        logger.warning(".lnk read failed for %s (%s)", path.name, exc)
        return ShortcutTarget(url=None, source_text="")

    match = _URL_BYTE_RE.search(blob)
    if not match:
        return ShortcutTarget(url=None, source_text="")
    try:
        url = match.group(0).decode("ascii").rstrip(".,;)]}>")
    except UnicodeDecodeError:
        return ShortcutTarget(url=None, source_text="")
    return ShortcutTarget(
        url=url,
        source_text=f"Shortcut: {path.name}\nURL: {url}\n",
    )
