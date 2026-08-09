"""Who owns a lane, and which names are allowed to claim ownership.

A principal is the configured MCP client identity — ``operator``,
``lane-claude``, ``lane-codex``, ``terminal-manual``. It is durable across
reconnects, which is what makes it the right thing to persist on a lane
record; the MCP session id is not (it changes every reconnect) and the turn id
is provenance, not authority.

The roster comes from ``mcp.yaml``, the same file the bearer tokens resolve
against, so a principal the daemon accepts is by construction one some client
could authenticate as. Read fresh on every call, like every other consumer of
that file: caching it would let a client the operator has just added
authenticate at the hub and then be refused by the daemon until a restart,
which reads as a broken client rather than as stale config.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

OPERATOR_PRINCIPAL = "operator"


def known_principals() -> frozenset[str]:
    """The configured client roster, or an empty set when it cannot be read.

    Empty means every attested-caller check refuses — fail closed, which is
    the right direction for an authorization roster. It returns rather than
    raises because the callers are IPC message handlers: an exception there
    becomes an unhandled dispatch error on a connection that was asking a
    perfectly ordinary question, where a refusal is both accurate and
    actionable.
    """
    from tesseract.config.mcp import load_mcp_config

    try:
        return frozenset(client.name for client in load_mcp_config().clients)
    except Exception:  # noqa: BLE001 — an unreadable roster refuses everyone
        log.warning(
            "principals: mcp.yaml could not be read; every caller principal "
            "will be refused until it can be",
            exc_info=True,
        )
        return frozenset()


def is_known_principal(name: str) -> bool:
    """Whether ``name`` is an identity the runtime will accept.

    The operator is always one, whether or not `mcp.yaml` can be read. It is
    the runtime's own identity — the fallback every non-hub caller resolves to
    — rather than something the MCP client roster confers. Gating it on that
    file would take the operator's own cockpit down whenever MCP config
    breaks, which is a bigger failure than the one being guarded against, and
    an unrelated one.
    """
    if name == OPERATOR_PRINCIPAL:
        return True
    return bool(name) and name in known_principals()


def may_reach(
    *, caller: str | None, owner: str, shared_with: list[str] | tuple[str, ...]
) -> bool:
    """Whether ``caller`` may operate on a resource owned by ``owner``.

    ``caller=None`` means the call carries no principal at all — the daemon's
    own recovery and cleanup paths, which act on every lane by definition.
    That is NOT what an IPC message resolves to: a message naming no caller is
    refused at the handler, before anything here is consulted.

    The operator keeps cross-scope administration on purpose. These are one
    operator's collaborating workers, not mutually distrustful tenants, and an
    operator locked out of a lane it can see in the cockpit is a worse failure
    than the one being fixed.
    """
    if caller is None or caller == OPERATOR_PRINCIPAL:
        return True
    return caller == owner or caller in shared_with


__all__ = [
    "OPERATOR_PRINCIPAL",
    "is_known_principal",
    "known_principals",
    "may_reach",
]
