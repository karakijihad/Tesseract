"""What can reach you, what last did, and whether the door is open.

``GET /api/autonomy/channels`` is the Channels room. It replaces the
Notifications pane, which answered one of those three questions: which kinds
were muted and how close each was to its cap. A room called Notifications
listed the settings; a room called Channels says whether the operator is
actually being reached, which is the question the panel exists to answer.

**Three bands, three producers, one join here.** The catalog and the mute
state are `notifications.py`'s, called rather than re-derived. Where each kind
goes is `outbound_routing.py`'s table, which is the file that decides it. What
was last SENT is the notifier's own record, which exists because a room that
cannot show the message it just sent is asking to be believed.

**A kind routed nowhere is not a fault and does not read as one.** The table
allows an empty list deliberately: everything here reaches the panel by the
thing that produced it, and routing only decides what leaves the machine.

Anonymous-readable, like the other dashboard feeds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes._bands import band_for

from tesseract.mirror.server.routes import notifications as notifications_route
from tesseract.mirror.server.routes._isotime import iso as _iso
from tesseract.mirror.server.routes._isotime import parse as _parse
from tesseract.orchestrator.autonomy.outbound import (
    CATEGORIES,
    EXEMPT_CATEGORIES,
    RateLedger,
    read_recent_sent,
    read_runtime_mutes,
)
from tesseract.orchestrator.autonomy.outbound_routing import load_outbound_routing
from tesseract.orchestrator.liveness import OperationalState, label_of

log = logging.getLogger(__name__)

# One unwrap for this room's producers, named so a warning says which surface
# went thin. Four rooms had four copies of it.
_band = band_for("channels")

# The channel the panel reports on. One today, and the room is built around
# the routing table rather than around this name, so a second one appears in
# `where` without anything here changing.
CHANNEL = "telegram"


def _said(
    destinations: tuple[str, ...],
    *,
    muted: bool,
    exempt: bool,
    used: int,
    cap: int | None,
) -> str:
    """What is true of this kind, said as a consequence rather than a setting.

    A reader of this room wants to know whether it will reach them, so every
    branch answers that: `muted` is not a state, it is a message they will not
    get.
    """
    where = ", ".join(sorted(destinations))
    # Exempt first, because that is the order `OutboundNotifier._deliver` uses:
    # a kind that must be seen is sent whatever the mute file says. Saying
    # "muted by you, so it is not sent" over a kind the runtime still delivers
    # is the room telling the operator the opposite of what happens.
    if exempt and destinations:
        return f"sent to {where}, and always gets through however much is muted"
    if muted:
        return (
            f"muted by you, so it is not sent to {where} any more"
            if destinations
            else "muted by you, and it was not sent anywhere anyway"
        )
    if not destinations:
        return "not sent off this machine, so it appears here and nowhere else"
    if cap is None:
        return (
            f"sent to {where}, and how many an hour it is allowed could not be "
            "read, so whether the next one is held back is not known here"
        )
    if cap > 0 and used >= cap:
        return f"sent to {where}, and the rest of this hour is held back"
    return f"sent to {where}, {used} of {cap} in the last hour"


def kinds(app: Any, now: datetime) -> list[dict[str, Any]]:
    """Every kind the runtime can send, with what it does and what holds it.

    The state is a reading and never a fault: a kind you muted expects nothing
    of itself, and a kind at its cap is working below what it promised because
    the next one is held back. Neither is broken.
    """
    try:
        routing = load_outbound_routing()
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("channels route: the routing table could not be read")
        routing = None

    notifier = app.get("outbound_notifier") if hasattr(app, "get") else None
    ledger = notifier.ledger if notifier is not None else RateLedger()
    muted_runtime = set(read_runtime_mutes().get(CHANNEL, []))

    last_at: dict[str, datetime] = {}
    for row in read_recent_sent():
        when = _parse(row.get("at"))
        category = str(row.get("category") or "")
        if when is not None and category and category not in last_at:
            last_at[category] = when

    out: list[dict[str, Any]] = []
    for category in CATEGORIES:
        exempt = category in EXEMPT_CATEGORIES
        destinations = tuple(routing.destinations(category)) if routing else ()
        muted = category in muted_runtime
        cap = _cap(app, category)
        used = ledger.count(CHANNEL, category, now=now)
        if not destinations:
            state = OperationalState.IDLE
        elif exempt:
            # Before the mute check, matching `_deliver`'s own precedence.
            state = OperationalState.RUNNING
        elif muted:
            state = OperationalState.IDLE
        elif cap is None:
            # A fact that could not be read is never rendered as a known
            # one. `0 of 0 in the last hour` is what this branch used to
            # print, and it reads as a cap the operator set.
            state = OperationalState.UNKNOWN
        elif cap > 0 and used >= cap:
            state = OperationalState.DEGRADED
        else:
            state = OperationalState.RUNNING
        said = _said(
            destinations, muted=muted, exempt=exempt, used=used, cap=cap,
        )
        out.append(
            {
                "name": category,
                "state": state.value,
                "label": label_of(state),
                "said": said,
                "at": _iso(last_at.get(category)),
                # What may be done to it, on the rule Managed system follows:
                # the backend answers, so no view draws a control the write
                # would refuse.
                "acts": [] if exempt else ["unmute" if muted else "mute"],
                "value": "cannot be muted" if exempt else "",
                "exempt": exempt,
                "muted": muted,
            }
        )
    return out


def _cap(app: Any, category: str) -> int | None:
    """The per-hour cap `notifications.py` would enforce, read through it.

    That module takes a request because its own handlers have one. The room
    takes the app, so the shim is here rather than a second reading of
    `channels.yaml`.

    None when it could not be read at all, which is a different claim from a
    cap of zero and the room draws it differently.
    """

    class _AsRequest:
        def __init__(self, application: Any) -> None:
            self.app = application

    try:
        return notifications_route._cap_for(_AsRequest(app), CHANNEL, category)
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("channels route: the cap for %s could not be read", category)
        return None


def last_message() -> dict[str, Any] | None:
    """The last thing the runtime said to the operator, or None.

    None means it has said nothing on this machine, which the room states
    rather than leaving a blank band.
    """
    rows = read_recent_sent()
    if not rows:
        return None
    row = rows[0]
    return {
        "category": str(row.get("category") or ""),
        "at": _iso(_parse(row.get("at"))),
        "channels": [str(c) for c in (row.get("channels") or ())],
        "text": str(row.get("text") or ""),
    }


def door() -> list[dict[str, Any]]:
    """Whether the channel itself is up, as the adapter reports it.

    `ChannelStatus` is the adapter's own account and `GET /api/channels`
    renders the same one for Settings. Two readings of one bridge is how a
    panel comes to say connected at a bridge that is down.

    A registry with nothing in it is not a healthy channel with no traffic: it
    means nothing can reach the operator at all, which is the distinction the
    liveness contract exists to draw.
    """
    from tesseract.integrations import list_channels

    try:
        adapters = list_channels()
    except Exception:  # noqa: BLE001 — a room says less rather than 500ing
        log.exception("channels route: the channel registry could not be read")
        adapters = []
    if not adapters:
        return [
            {
                "name": "no channel",
                "state": OperationalState.NOT_INSTRUMENTED.value,
                "label": label_of(OperationalState.NOT_INSTRUMENTED),
                "said": (
                    "nothing is wired into this app, so nothing it writes can "
                    "reach you anywhere but here"
                ),
                "at": None,
                "value": "",
                "acts": [],
            }
        ]
    out: list[dict[str, Any]] = []
    for adapter in adapters:
        try:
            status = adapter.status_snapshot()
        except Exception:  # noqa: BLE001 — one adapter, not the room
            log.exception("channels route: %s could not say how it is", adapter)
            continue
        bridge = str(getattr(status, "bridge_state", "") or "")
        state = _BRIDGE_STATE.get(bridge, OperationalState.UNKNOWN)
        errors = int(getattr(status, "error_count_24h", 0) or 0)
        # A bridge that is up and erroring is not simply up. The count is the
        # runtime's own and the sentence names what it costs: a message it
        # could not deliver is one the operator never saw.
        if state is OperationalState.RUNNING and errors:
            state = OperationalState.DEGRADED
        out.append(
            {
                "name": str(getattr(status, "name", "") or "channel"),
                "state": state.value,
                "label": label_of(state),
                "said": _door_said(status, state, errors),
                "at": _iso(_parse(getattr(status, "last_poll_at", None))),
                "value": f"{int(getattr(status, 'allowed_count', 0) or 0)} approved",
                # Bouncing the bridge is what fixes the state above it, and it
                # is the one control this room needs.
                "acts": ["restart"],
            }
        )
    return out


# What each bridge state means to a room. The adapter's vocabulary is three
# words and none of them is an operational state, so the translation lives
# here rather than in TSX.
_BRIDGE_STATE = {
    "running": OperationalState.RUNNING,
    "stopped": OperationalState.REFUSED,
    "error": OperationalState.FAILED,
}


def _door_said(status: Any, state: OperationalState, errors: int) -> str:
    """One sentence about the channel, saying the consequence first."""
    inbound = int(getattr(status, "messages_in_24h", 0) or 0)
    outbound = int(getattr(status, "messages_out_24h", 0) or 0)
    pending = int(getattr(status, "pending_count", 0) or 0)
    waiting = f", {pending} waiting for you to let them in" if pending else ""
    if state is OperationalState.RUNNING:
        return (
            f"connected, {outbound} sent and {inbound} received in the last day"
            f"{waiting}"
        )
    if state is OperationalState.DEGRADED:
        return (
            f"connected, and {errors} thing{'' if errors == 1 else 's'} it tried "
            f"to send in the last day did not arrive{waiting}"
        )
    if state is OperationalState.REFUSED:
        return "stopped, so nothing it writes reaches you there until it is started"
    if state is OperationalState.FAILED:
        return "it is in an error state, so nothing it writes reaches you there"
    return "it did not say how it is, so whether anything reaches you is unknown"


async def get_channels(request: web.Request) -> web.Response:
    """The three bands, each from the producer that owns it."""
    now = datetime.now(timezone.utc)
    import asyncio

    # File reads on the loop that carries health, the socket and inbound turns.
    rows, last, doors = await asyncio.gather(
        asyncio.to_thread(kinds, request.app, now),
        asyncio.to_thread(last_message),
        asyncio.to_thread(door),
        return_exceptions=True,
    )

    return web.json_response(
        {
            "kinds": _band(rows, []),
            "lastMessage": _band(last, None),
            "adapters": _band(doors, []),
            "observedAt": _iso(now),
        }
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/autonomy/channels", get_channels)


__all__ = [
    "CHANNEL",
    "door",
    "get_channels",
    "kinds",
    "last_message",
    "register",
]
