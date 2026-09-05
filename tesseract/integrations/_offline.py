"""Saying something from a process that never built a channel.

The channel registry is populated by the Mirror backend as it starts each
bridge. Two things that need to speak are not the backend: a scheduler run with
no session open, and the supervisor at the moment it stops trying to restart
the backend at all. The second is the one that matters most, because the thing
that would normally do the telling is the thing that has gone.

A channel says it can do this by shipping an `offline` module beside its
adapter, with a `build()` that returns something with `list_users` and
`send_text`. Found by name rather than by a registry: a `whatsapp` package with
an `offline.py` works without a line being changed here, which is the same
promise the routing table makes.

Where a kind goes is still `routing.yaml`, and who hears it is still
`_outbound.notify_operators`. Only the way the adapter is obtained differs.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)

#: How long the whole attempt gets. This runs where something has already gone
#: wrong, sometimes in a process that is on its way out, so it is bounded
#: rather than allowed to hang on a network that may be the reason it is
#: being called.
SEND_TIMEOUT_SECONDS = 20.0


def offline_adapter(channel: str) -> Any | None:
    """The channel's own no-bridge adapter, or None if it has none."""
    if not channel or not channel.isidentifier():
        return None
    try:
        module = importlib.import_module(f"tesseract.integrations.{channel}.offline")
    except ModuleNotFoundError:
        return None
    except Exception:
        log.exception("offline: %s could not be loaded", channel)
        return None
    build = getattr(module, "build", None)
    if build is None:
        return None
    try:
        return build()
    except Exception:
        log.exception("offline: %s could not be built", channel)
        return None


async def notify_offline(kind: str, context: dict[str, Any]) -> dict[str, int]:
    """Compose `kind` and send it to every channel routed for it that can.

    Returns counts rather than raising. Every caller is already handling
    something that went wrong, and failing to say so must not become a second
    failure on top of the first.
    """
    from tesseract.integrations._outbound import notify_operators, reached_anyone
    from tesseract.orchestrator.autonomy.message import compose
    from tesseract.orchestrator.autonomy.outbound_routing import load_outbound_routing

    try:
        targets = load_outbound_routing().destinations(kind)
    except Exception:
        log.exception("offline: could not read where %s goes", kind)
        return {"sent": 0, "errors": 1}
    if not targets:
        return {"sent": 0, "errors": 0}

    message = compose(kind, context)
    sent = 0
    errors = 0
    delivered: list[str] = []

    async def one(channel: str) -> tuple[str, int, int, bool]:
        """Send to one channel and close it, whatever happened."""
        adapter = offline_adapter(channel)
        if adapter is None:
            return channel, 0, 0, False
        try:
            result = await asyncio.wait_for(
                notify_operators(adapter, message), timeout=SEND_TIMEOUT_SECONDS,
            )
            return (
                channel,
                int(result.get("sent") or 0),
                int(result.get("errors") or 0),
                reached_anyone(result),
            )
        except Exception:
            log.exception("offline: sending %s to %s failed", kind, channel)
            return channel, 0, 1, False
        finally:
            close = getattr(adapter, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    log.exception("offline: closing %s failed", channel)

    # Together, not one after another. This path runs while something is
    # already going wrong and the supervisor's use of it is the last thing it
    # does before exiting, so a channel that sits on its 20 second timeout must
    # not spend the next one's budget as well.
    outcomes = await asyncio.gather(
        *(one(channel) for channel in targets), return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            log.exception("offline: sending %s failed", kind, exc_info=outcome)
            errors += 1
            continue
        channel, channel_sent, channel_errors, arrived = outcome
        sent += channel_sent
        errors += channel_errors
        # A crash-storm warning that arrived in part is one the operator has
        # in front of them, and the record has to say so: this is the last
        # thing the supervisor does before it stops.
        if arrived:
            delivered.append(channel)
    if delivered:
        # The panel's record of what the runtime last said. Without this the
        # most important message the runtime sends, the one saying it has
        # stopped restarting itself, was the one message the Channels room
        # could never show.
        try:
            from tesseract.orchestrator.autonomy.message import render_plain
            from tesseract.orchestrator.autonomy.outbound import record_sent

            # Never queued for. Every caller of this is handling something
            # that already went wrong, and the supervisor's is the last thing
            # it does before exiting.
            record_sent(
                kind, render_plain(message), delivered, wait_for_lock=False,
            )
        except Exception:  # noqa: BLE001 — the send already happened
            log.exception("offline: could not record what was sent")
    return {"sent": sent, "errors": errors}


def notify_offline_blocking(kind: str, context: dict[str, Any]) -> dict[str, int]:
    """`notify_offline` for a caller with no event loop.

    The supervisor is a synchronous loop and this is the last thing it does
    before exiting, so it runs the send to completion rather than leaving it
    to a loop that is about to stop existing.
    """
    try:
        return asyncio.run(notify_offline(kind, context))
    except Exception:
        log.exception("offline: could not send %s", kind)
        return {"sent": 0, "errors": 1}


__all__ = [
    "SEND_TIMEOUT_SECONDS",
    "notify_offline",
    "notify_offline_blocking",
    "offline_adapter",
]
