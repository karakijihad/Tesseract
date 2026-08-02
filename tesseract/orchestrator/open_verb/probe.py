"""Ask a target what it is.

For URLs this is a `HEAD` — content type and whether the page permits framing.
For paths it is a stat. Both answers are *routing* input only: a probe result
never authorizes anything, because the headers a server sends now do not bind
what it serves on the next request.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from pathlib import Path

from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

# A redirect chain is walked by hand; this bounds it.
_MAX_REDIRECTS = 5


def _redacted(url: str) -> str:
    """A URL can carry `user:password@` in its authority, and a probe failure
    would otherwise write it to the log verbatim."""
    try:
        parts = urlsplit(url)
        if not parts.hostname:
            return "<no host>"
        # `.port` parses lazily and raises on a malformed port, so it belongs
        # inside the guard — a redaction helper must never be the thing that
        # raises while reporting an error.
        authority = parts.hostname + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, authority, parts.path, "", ""))
    except ValueError:
        return "<unparseable url>"


@dataclass(frozen=True)
class UrlProbe:
    content_type: str
    frameable: bool
    final_url: str
    status: int
    # A target the network policy refuses is NOT the same as one that failed to
    # answer: an unreachable host may still be opened in the browser, but a
    # blocked one must not be. Collapsing the two would block reconnaissance
    # while permitting the act, which is worse than not blocking at all.
    blocked: bool = False


@dataclass(frozen=True)
class PathProbe:
    is_dir: bool
    suffix: str


def probe_path(path: Path) -> PathProbe:
    try:
        return PathProbe(is_dir=path.is_dir(), suffix=path.suffix.lower())
    except OSError:
        return PathProbe(is_dir=False, suffix="")


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


def _resolves_into_blocked_network(url: str, blocked: frozenset[str]) -> bool:
    """Resolve the host and test every address it answers with.

    Checking the hostname string would be trivially defeated by a name that
    resolves to a blocked address, so the resolved addresses are what matter.
    A resolution failure is not a block — an unreachable host already ends as
    `frameable=False` through the normal error path.
    """
    if not blocked:
        return False
    host = urlsplit(url).hostname
    if not host:
        return False

    networks = [ipaddress.ip_network(entry, strict=False) for entry in blocked]
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if any(address in network for network in networks):
            return True
    return False


async def probe_url(
    url: str, *, timeout_s: float, blocked_networks: frozenset[str] = frozenset()
) -> UrlProbe:
    """A credential-free HEAD. On any failure the caller is told the target is
    not frameable, so a slow or hostile server becomes a browser tab rather
    than a card that never paints."""
    # Checked before a client is even constructed, and again for every redirect
    # hop below — a public URL that 302s into a blocked range would otherwise
    # walk straight past the check that just passed.
    if _resolves_into_blocked_network(url, blocked_networks):
        log.debug("probe refused for %s: blocked network", _redacted(url))
        return UrlProbe("", False, url, 0, blocked=True)

    try:
        async with httpx.AsyncClient(
            # Redirects are walked by hand, NOT followed automatically: a
            # public URL that 302s to 169.254.169.254 would otherwise carry the
            # probe straight past the network check that just passed. Every hop
            # is re-checked before it is requested.
            follow_redirects=False,
            timeout=timeout_s,
            # A fresh client carries no cookie jar; state it so a later edit
            # does not quietly attach the operator's session to a probe.
            cookies=None,
        ) as client:
            current = url
            for _ in range(_MAX_REDIRECTS + 1):
                if _resolves_into_blocked_network(current, blocked_networks):
                    log.debug("probe refused for %s: blocked network", _redacted(current))
                    return UrlProbe("", False, current, 0, blocked=True)

                response = await client.head(current)
                # Some servers refuse HEAD outright. A single-byte ranged GET
                # gets the same headers without pulling the body.
                if response.status_code in {405, 501}:
                    response = await client.get(current, headers={"Range": "bytes=0-0"})

                location = response.headers.get("location")
                if not (response.is_redirect and location):
                    break
                current = str(httpx.URL(current).join(location))
            else:
                log.debug("probe gave up on %s: too many redirects", _redacted(url))
                return UrlProbe("", False, current, 0)
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("probe failed for %s: %s", _redacted(url), exc)
        return UrlProbe(content_type="", frameable=False, final_url=url, status=0)

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    ok = 200 <= response.status_code < 300
    return UrlProbe(
        content_type=content_type,
        frameable=ok and _frameable(response.headers),
        final_url=current,
        status=response.status_code,
    )
