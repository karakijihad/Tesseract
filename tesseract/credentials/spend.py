"""Putting a stored value into an outbound request, at the moment it is sent.

``store.resolve`` is the method that decrypts; this is the one thing above it
that a consumer calls. Git does its own version of this inside its argv,
because a credential on a remote address is not a request anyone builds with
headers. Everything that speaks HTTP comes through here instead, so that "how
is this service authenticated" is answered once, from the record the operator
approved, rather than once per tool.

Nothing in this module knows a service. It reads :class:`Injection` and does
what it says, which is what makes a tool the assistant writes for an API
nobody has heard of work with no runtime change.

**The properties below hold AT ONCE.** A later change is checked against the
list, not against whichever one prompted it:

1. **The host authenticated is the host connected to.** ``resolve`` checks the
   allowlist against the hostname parsed here, so the check and the connection
   read the same address. That is also why the caller must not follow
   redirects: a 302 replaces the destination after the check has passed.
2. **https only.** A value on a plaintext connection is a value published to
   every hop, and no allowlist entry can be a promise about that.
3. **The caller cannot pre-empt the injection point.** A request that already
   carries the header, the query parameter, or userinfo of its own is refused
   rather than overwritten, because a silent overwrite makes the model's
   version of the truth and the record's version differ with no way to tell
   which one went out.
4. **The injection point is a place a value can authenticate FROM.** A header
   that decides where a request goes or how its body is framed is not, and
   putting a value there breaks property 1 at the layer property 1 is about:
   the connection still reaches the approved host while a load balancer, a
   CDN or a shared virtual host in front of it routes on the value instead.
   ``credential_setup`` calls the header name mechanics, whose failure mode is
   a failed request, so the refusal has to live here.
5. **A host is a host, not a host and a port.** An allowlist entry cannot say
   ``:8443``, so a value must not travel to one. Anything other than the
   default https port is refused rather than quietly covered by the entry the
   operator approved.
6. **The value goes in and nothing comes back out.** The return carries the
   built request; ``safe_url`` is the address with nothing secret in it, and
   it is what a caller shows a person or writes to a log.
7. **An injection that is not an HTTP one is refused here**, not adapted.
   ``env`` describes an account handed whole to a command that makes its own
   calls (``credentials/reader.py``), and guessing that it might mean a header
   is how a value ends up somewhere the operator never approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .store import CredentialStore


class InsecureDestination(RuntimeError):
    """The address is not https, so the value would travel in the clear."""


class NotSpendableOverHttp(RuntimeError):
    """The record describes a credential that is not spent on a request."""


class DestinationAlreadyAuthenticated(RuntimeError):
    """The caller built the request with the injection point already filled."""


class InjectionPointNotAllowed(RuntimeError):
    """The record names a header that routes or frames, not one that identifies."""


# Headers that decide where a request goes or how it is read off the wire.
# A value in one of these does not authenticate anything: it re-routes, and it
# lands in the one header every intermediary logs by default.
_ROUTING_AND_FRAMING = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "trailer",
        "expect",
        ":authority",
        ":method",
        ":path",
        ":scheme",
    }
)

# The only port an allowlist entry can be read as covering.
_HTTPS_PORT = 443


@dataclass(frozen=True)
class AuthenticatedRequest:
    """A request with the value in it, and an address without one.

    ``url`` and ``headers`` go to the transport. ``safe_url`` is the only one
    of the three that may be shown, returned, or logged.
    """

    url: str
    headers: dict[str, str]
    safe_url: str


def authenticate(
    url: str,
    headers: dict[str, str],
    *,
    credential_id: str,
    store: CredentialStore | None = None,
) -> AuthenticatedRequest:
    """Build the request that spends ``credential_id`` against ``url``.

    Raises whatever the store raises for an unknown credential, one with no
    value, or a host it may not reach, plus this module's four refusals.
    Nothing is sent from here; the caller sends what it gets back.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise InsecureDestination(
            f"{url!r} is not an https address. A stored value is only ever "
            f"sent over an encrypted connection, so nothing was sent."
        )
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise InsecureDestination(
            f"{url!r} names no host, so there is nothing to check against the "
            f"account's allowlist and nothing was sent."
        )
    if parts.port is not None and parts.port != _HTTPS_PORT:
        raise InsecureDestination(
            f"{url!r} names port {parts.port}. An account lists hosts, and a "
            f"host is not a promise about every port on it, so nothing was "
            f"sent."
        )
    if parts.username or parts.password:
        raise DestinationAlreadyAuthenticated(
            "the address already carries a username or password. Give the "
            "plain address and name the account instead; the runtime puts the "
            "value in. Nothing was sent."
        )

    active = store or CredentialStore()
    # The kind is read BEFORE anything is decrypted, so a record that belongs
    # to the reader is refused with the reader's name rather than with a
    # complaint about a field it deliberately does not have: `env` records
    # carry no spend field at all, and `resolve` would report that as a
    # missing value. Decrypting four secrets and then declining to use them
    # would also be a worse version of the same answer.
    if active.injection_of(credential_id).kind == "env":
        raise NotSpendableOverHttp(
            f"{credential_id} is recorded as being read from a command's "
            f"environment, which is how a program is given one, not how a "
            f"request carries one. Nothing was sent. command_run is what "
            f"hands this account to code that makes its own calls; if the "
            f"record is wrong instead, credential_setup is where that is "
            f"corrected."
        )

    injection, value = active.resolve(credential_id, host)

    if injection.kind == "header":
        if injection.name.strip().lower() in _ROUTING_AND_FRAMING:
            raise InjectionPointNotAllowed(
                f"{credential_id} is recorded as being sent in the "
                f"{injection.name} header, which decides where a request goes "
                f"and how it is read rather than who it is from. A value there "
                f"authenticates nothing and changes the destination, so nothing "
                f"was sent. credential_setup is where the header name is "
                f"corrected."
            )
        clash = next(
            (k for k in headers if k.strip().lower() == injection.name.lower()), None
        )
        if clash is not None:
            raise DestinationAlreadyAuthenticated(
                f"the request already sets the {clash} header, which is where "
                f"{credential_id} goes. Leave it out and the runtime fills it "
                f"in. Nothing was sent."
            )
        return AuthenticatedRequest(
            url=url,
            headers={**headers, injection.name: f"{injection.prefix}{value}"},
            safe_url=url,
        )

    if injection.kind == "query":
        existing = parse_qsl(parts.query, keep_blank_values=True)
        if any(key == injection.name for key, _ in existing):
            raise DestinationAlreadyAuthenticated(
                f"the address already sets the {injection.name} query "
                f"parameter, which is where {credential_id} goes. Leave it out "
                f"and the runtime fills it in. Nothing was sent."
            )
        # Appended, not round-tripped. `urlencode(parse_qsl(...))` does not
        # give back what it was handed: a bare flag comes out as `flag=`, and
        # an encoding the caller chose is re-chosen. The parse above is a read
        # for the clash check and nothing else.
        addition = urlencode([(injection.name, f"{injection.prefix}{value}")])
        query = f"{parts.query}&{addition}" if parts.query else addition
        return AuthenticatedRequest(
            url=urlunsplit(parts._replace(query=query)),
            headers=dict(headers),
            safe_url=url,
        )

    if injection.kind == "url":
        # Percent-encoded because userinfo has a grammar and a token does not:
        # a value holding `@` or `/` would otherwise re-parse into a different
        # address than the one the allowlist was checked against.
        userinfo = f"{quote(injection.name, safe='')}:{quote(value, safe='')}"
        netloc = f"{userinfo}@{parts.netloc}"
        return AuthenticatedRequest(
            url=urlunsplit(parts._replace(netloc=netloc)),
            headers=dict(headers),
            safe_url=url,
        )

    raise NotSpendableOverHttp(
        f"{credential_id} is recorded as {injection.kind}, which this does not "
        f"know how to put into a request. Nothing was sent. A kind added to "
        f"the store and not taught to spend.py lands here rather than being "
        f"guessed at."
    )


__all__ = [
    "AuthenticatedRequest",
    "DestinationAlreadyAuthenticated",
    "InjectionPointNotAllowed",
    "InsecureDestination",
    "NotSpendableOverHttp",
    "authenticate",
]
