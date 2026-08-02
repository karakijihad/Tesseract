"""Ask a target what it is.

For URLs this is a `HEAD` — content type and whether the page permits framing.
For paths it is a stat. Both answers are *routing* input only: a probe result
never authorizes anything, because the headers a server sends now do not bind
what it serves on the next request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UrlProbe:
    content_type: str
    frameable: bool
    final_url: str
    status: int


@dataclass(frozen=True)
class PathProbe:
    exists: bool
    is_dir: bool
    suffix: str


def probe_path(path: Path) -> PathProbe:
    try:
        return PathProbe(
            exists=path.exists(),
            is_dir=path.is_dir(),
            suffix=path.suffix.lower(),
        )
    except OSError:
        return PathProbe(exists=False, is_dir=False, suffix="")


def _frameable(headers: httpx.Headers) -> bool:
    """Both mechanisms are one-way: they can only forbid framing. Absent
    headers mean the page has expressed no objection, which is the only case
    where the cockpit gets to try."""
    xfo = headers.get("x-frame-options", "").strip().lower()
    if xfo in {"deny", "sameorigin"} or xfo.startswith("allow-from"):
        return False

    csp = headers.get("content-security-policy", "").lower()
    for directive in csp.split(";"):
        directive = directive.strip()
        if not directive.startswith("frame-ancestors"):
            continue
        sources = directive.split()[1:]
        # A wildcard is the only permissive form we can verify from here. A
        # specific origin list will not contain the Mirror, so it is a refusal.
        return "*" in sources
    return True


async def probe_url(url: str, *, timeout_s: float) -> UrlProbe:
    """A credential-free HEAD. On any failure the caller is told the target is
    not frameable, so a slow or hostile server becomes a browser tab rather
    than a card that never paints."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_s,
            # A fresh client carries no cookie jar; state it so a later edit
            # does not quietly attach the operator's session to a probe.
            cookies=None,
        ) as client:
            response = await client.head(url)
            # Some servers refuse HEAD outright. A single-byte ranged GET gets
            # the same headers without pulling the body.
            if response.status_code in {405, 501}:
                response = await client.get(url, headers={"Range": "bytes=0-0"})
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("probe failed for %s: %s", url, exc)
        return UrlProbe(content_type="", frameable=False, final_url=url, status=0)

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    ok = 200 <= response.status_code < 300
    return UrlProbe(
        content_type=content_type,
        frameable=ok and _frameable(response.headers),
        final_url=str(response.url),
        status=response.status_code,
    )
