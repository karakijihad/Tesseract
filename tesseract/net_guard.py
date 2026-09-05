"""Whether the runtime may fetch a URL it was handed.

The check is on the ADDRESSES the host resolves to, never on the name: a name
the runtime is given can point anywhere, and `metadata.internal` and
`169.254.169.254` are the same machine. So the name is resolved and every
address it returns is measured against the caller's blocked ranges.

**A resolution failure is not a block.** An unreachable host fails on its own
through the caller's normal error path, and treating "I could not look it up"
as "it is dangerous" would refuse a working URL every time DNS hiccups.

**Which ranges are blocked is the CALLER's, and the two callers differ on
purpose.** `open` shows a page on the operator's own screen after they asked
for it, so loopback and private ranges stay reachable: opening
`http://localhost:5173` or a NAS by address is an ordinary act on a local-first
tool. A channel's media fetch is the opposite case. The URL can come from text
the assistant is processing rather than from the operator, and the bytes do not
stay on this machine, they are forwarded to a chat. There, reaching the
loopback interface is exfiltration of whatever is listening on it.

**Checking a name and then letting the client resolve it again closes
nothing.** A name whose DNS answer changes between the two, which costs an
attacker a short TTL record and nothing else, passes the check as a public
address and connects to a blocked one. So `pinned_target` hands back the exact
address that was checked, and the caller connects to THAT, carrying the
original host in the `Host` header and in SNI so the request and the
certificate check are still about the name. The address that was validated is
the address that is reached.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

#: The schemes a fetch may use. Everything else, `file:`, `data:`, `ftp:`,
#: reaches something that is not a web server and has no business behind a
#: URL the runtime was handed.
FETCHABLE_SCHEMES = frozenset({"http", "https"})


def redacted(url: str) -> str:
    """A URL safe to put in a message a person will read.

    A URL can carry `user:password@` in its authority, and a fetch failure is
    reported through a tool result the assistant reads back and the operator
    sees, so the raw address would be written out with the credential in it.
    Query and fragment go too: either can carry a token.

    It cannot raise. This is called while reporting a failure and must never
    become the failure: `.port` parses lazily and throws on a malformed port,
    and an unclosed bracket throws inside `urlsplit` itself. Rebuilt from
    `netloc` rather than host and port, which keeps the brackets an IPv6
    literal needs.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if not parts.netloc:
        return "<no host>"
    # Only the LAST `@` separates userinfo from the host; one may legally
    # appear inside the userinfo itself.
    authority = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, authority, parts.path, "", ""))


def _blocks(address: "ipaddress._BaseAddress", networks: list[Any]) -> bool:
    """Is `address` inside any of `networks`, however it is written?

    **An address is measured in both of its forms.** `::ffff:172.16.0.1` and
    `172.16.0.1` are the same machine, and a resolver may answer with either,
    so a list naming `172.16.0.0/12` has to catch both or the range it declares
    is not the range it blocks. The config used to carry a hand-written
    `::ffff:` twin for each v4 entry, and one of the four was missing: a second
    list to keep in step is a list that falls out of step. Deriving the twin
    here means a range added to the config is blocked in both forms the day it
    is added.
    """
    forms = [address]
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        forms.append(mapped)
    return any(
        form in network
        for form in forms
        for network in networks
        if form.version == network.version
    )


def resolves_into_blocked_network(url: str, blocked: Iterable[str]) -> bool:
    """Does `url`'s host resolve into any of `blocked`?

    False when nothing is blocked, when the URL carries no host, or when the
    name cannot be resolved. See the module docstring for why the last one is
    not a refusal.
    """
    networks = [ipaddress.ip_network(entry, strict=False) for entry in blocked]
    if not networks:
        return False
    host = urlsplit(url).hostname
    if not host:
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _blocks(address, networks):
            return True
    return False


def refuse_reason(url: str, blocked: Iterable[str]) -> str | None:
    """Why this URL may not be fetched, or None if it may.

    A sentence, because every caller of this puts it in front of a person.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in FETCHABLE_SCHEMES:
        return (
            f"'{scheme or 'that'}' is not a web address the runtime will "
            "fetch. Only http and https are."
        )
    if not urlsplit(url).hostname:
        return "that address names no host, so there is nothing to fetch"
    if not list(blocked):
        # The same answer `pinned_target` gives, because the two disagreeing
        # is how a caller comes to show "this may be sent" over a guard that
        # then refuses it. An empty denylist is a denylist that lost its
        # contents, and going quiet is the one failure it must not have.
        return (
            "there is no list of addresses the runtime may not fetch, so it "
            "will not fetch anything"
        )
    if resolves_into_blocked_network(url, blocked):
        return (
            "that address is on this machine or its private network, and "
            "what is fetched from it would be forwarded off the machine"
        )
    return None


class Blocked(Exception):
    """This URL may not be fetched. The message is written for a person.

    `kind` says WHICH refusal it was, because the callers do not treat them
    alike: a name that resolves into a blocked range is a refusal to report as
    one, and a name that could not be looked up is an unreachable host, which
    already has its own error path. Reporting the second as the first tells the
    operator their own address was rejected on policy when it was never found.
    """

    def __init__(self, message: str, kind: str = "refused") -> None:
        super().__init__(message)
        self.kind = kind


def pinned_target(url: str, blocked: Iterable[str]) -> tuple[str, dict[str, str], str]:
    """`(url_to_request, headers, sni_hostname)` for a URL that may be fetched.

    Raises :class:`Blocked` with the reason otherwise, so a caller cannot use
    the result without having passed the check.

    The returned URL names the resolved ADDRESS rather than the host, which is
    what makes the check binding: the connection can only reach what was
    validated. The host travels in the `Host` header and in SNI, so the server
    still sees the request it would have seen and the certificate is still
    checked against the name.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in FETCHABLE_SCHEMES:
        raise Blocked(
            f"'{scheme or 'that'}' is not a web address the runtime will "
            "fetch. Only http and https are.",
            "scheme",
        )
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise Blocked(
            "that address names no host, so there is nothing to fetch", "no_host",
        )

    networks = [ipaddress.ip_network(entry, strict=False) for entry in blocked]
    if not networks:
        raise Blocked(
            "there is no list of addresses the runtime may not fetch, so it "
            "will not fetch anything",
            "no_denylist",
        )
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise Blocked(
            f"that address could not be looked up ({exc})", "lookup_failed",
        ) from exc

    chosen: str | None = None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            continue
        if _blocks(address, networks):
            raise Blocked(
                "that address is on this machine or its private network, and "
                "what is fetched from it would be forwarded off the machine",
                "blocked_network",
            )
        if chosen is None:
            chosen = address.compressed if address.version == 4 else f"[{address.compressed}]"
    if chosen is None:
        raise Blocked(
            "that address did not resolve to anything reachable", "lookup_failed",
        )

    authority = f"{chosen}:{parts.port}" if parts.port else chosen
    pinned = urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))
    # Built from the host and the port, never from `netloc`, which carries any
    # userinfo with it: `https://user:secret@example.com/` sent a `Host` of
    # `user:secret@example.com`, which is not a host, and put a credential in
    # a header while the pinned URL had already dropped it from the request.
    literal = f"[{host}]" if ":" in host else host
    sent_host = f"{literal}:{parts.port}" if parts.port else literal
    return pinned, {"Host": sent_host}, host


__all__ = [
    "FETCHABLE_SCHEMES",
    "Blocked",
    "pinned_target",
    "redacted",
    "refuse_reason",
    "resolves_into_blocked_network",
]
