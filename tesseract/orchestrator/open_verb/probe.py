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

from tesseract import http_client, net_guard
from tesseract.net_guard import redacted as _redacted

log = logging.getLogger(__name__)

# A redirect chain is walked by hand; this bounds it.
_MAX_REDIRECTS = 5


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


async def probe_url(
    url: str, *, timeout_s: float, blocked_networks: frozenset[str] = frozenset()
) -> UrlProbe:
    """A credential-free HEAD. On any failure the caller is told the target is
    not frameable, so a slow or hostile server becomes a browser tab rather
    than a card that never paints.

    **The address is what is checked, and the address is what is reached.**
    `net_guard` is the mechanism, so this and a channel's media fetch cannot
    drift into two answers about what a name resolves to. What they do NOT
    share is the list: the ranges `open` blocks cover link-local, including
    cloud metadata at 169.254.169.254, but deliberately not RFC1918 or
    loopback. Opening a router admin page or a NAS by address is an ordinary
    act on a local-first tool, and this probe is credential-free, cookie-free
    and reads no body, so what a private-range HEAD exposes is response
    metadata for a host that was named on purpose. Revisit that trade when
    `open` becomes reachable by untrusted input without operator intent; the
    condition is what changes it, not the mechanism.
    """
    # Every hop is checked in the loop below, the first one included, and the
    # check and the request are one act there. A second check before the client
    # is built would resolve the name a second time to answer the same
    # question, which is the shape this function is being fixed for.
    try:
        async with http_client.async_client(
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
                # The request goes to the ADDRESS this hop was checked at.
                # Checking the name and then handing the same name to the
                # client leaves it to resolve a second time, and a record with
                # a short TTL can answer public for the check and link-local
                # for the request. That gap was raised three times against
                # this function and closed on the media path only; the check
                # and the connection are one act here now.
                #
                # With nothing declared blocked there is nothing to walk past,
                # so the hop goes out by name as it always did. Pinning is the
                # answer to a list being defeated, not a list being absent.
                target, headers, sni = current, {}, ""
                if blocked_networks:
                    try:
                        target, headers, sni = net_guard.pinned_target(
                            current, blocked_networks,
                        )
                    except net_guard.Blocked as exc:
                        if exc.kind == "blocked_network":
                            log.debug(
                                "probe refused for %s: blocked network",
                                _redacted(current),
                            )
                            return UrlProbe("", False, current, 0, blocked=True)
                        # Anything else is a URL that cannot be reached rather
                        # than one that is refused, and the caller's own failure
                        # path already says so. Reporting it as blocked would
                        # tell the operator their address was turned away on
                        # policy when it was never found.
                        log.debug("probe gave up on %s: %s", _redacted(current), exc)
                        return UrlProbe("", False, current, 0)

                extensions = {"sni_hostname": sni} if sni else {}
                response = await client.head(
                    target, headers=headers, extensions=extensions,
                )
                # Some servers refuse HEAD outright. A single-byte ranged GET
                # gets the same headers without pulling the body.
                if response.status_code in {405, 501}:
                    response = await client.get(
                        target,
                        headers={**headers, "Range": "bytes=0-0"},
                        extensions=extensions,
                    )

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
