"""Canvas click → the `open` verb, through the operator's own ASK gate.

A folder card lists entries, and clicking one has to reach `open`. `open` may
resolve the entry to something that renders in the cockpit — no gate at all —
or to something that has to be handed to Windows, which goes through
`os_launch` and its ASK.

That ASK is why this rides the chat WebSocket rather than the surface REST
route. `POST /api/surfaces/{view}/event` has no way to ask the operator
anything, and `decide.evaluate` denies an ASK it cannot put to anyone, so a
click routed there would silently refuse to launch — the feature would look
built and behave like a bug. The WS already owns the round-trip (`tool_ask`
envelope → the operator's prompt → `tool_response`), so the click reuses it
instead of growing a second approval channel that would have to be audited
separately.

The gate is `os_launch`'s own. Nothing here approves anything: `execute_tool`
runs the full permission pipeline, and the ask_fn is the same one the chat
turn uses, so the prompt looks identical whether TARS asked to launch a file
or the operator clicked it.
"""

from __future__ import annotations

import logging
import uuid

from aiohttp import web

from tesseract.brain.tools import execute_tool
from tesseract.kernel.tools.base import ToolContext
from tesseract.mirror.server.ask_gate import _make_ask_fn
from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.session_model import ServerSession

log = logging.getLogger(__name__)


async def _say(session: ServerSession, text: str, is_error: bool) -> None:
    """Report the outcome on the same channel a slash command reports on. A
    render needs no narration — the card arrives on its own — but a refusal or
    a launch does, or the click looks like it did nothing."""
    env = make_envelope(
        "surface_open_result",
        "execution",
        session.session_id,
        {"text": text, "is_error": is_error},
    )
    session.event_log.append(env)
    try:
        await session.ws.send_json(env)
    except (ConnectionResetError, RuntimeError):
        # The operator navigated away mid-open. The verb already ran; there is
        # nothing to undo and nobody to tell.
        log.debug("surface_open: ws closed before the result could be sent")


async def handle_surface_open(
    app: web.Application, session: ServerSession, data: dict
) -> None:
    target = str(data.get("target") or "").strip()
    view = str(data.get("view") or "tars").strip() or "tars"
    if not target:
        return

    registry = app.get("tool_registry")
    if registry is None or "open" not in getattr(registry, "tools", {}):
        await _say(session, "open is not available yet — the tool registry is still booting", True)
        return

    policy = app["config"].permissions
    # Rebuilt rather than stashed: `_make_ask_fn` closes over this connection's
    # ws + pending-ask table, and the session already carries every part of it.
    ask_fn = _make_ask_fn(
        session.ws,
        session.session_id,
        session.pending_asks,
        session.event_log,
        session.parked_asks,
    )
    context = ToolContext(
        session_id=session.session_id,
        current_call_id=f"surface-open-{uuid.uuid4().hex[:12]}",
        posture_source="operator-canvas",
        tool_registry_provider=lambda: registry,
        ask_fn=ask_fn,
        policy=policy,
    )

    try:
        result = await execute_tool(
            registry,
            "open",
            {"target": target, "view": view},
            context,
            ask_fn=ask_fn,
            policy=policy,
        )
    except Exception as exc:  # noqa: BLE001 — a click must not kill the socket
        log.exception("surface_open failed for %r", target)
        await _say(session, f"could not open it: {type(exc).__name__}: {exc}", True)
        return

    # A cockpit render already announced itself by creating the card, so only
    # the OS handoff and every failure need words.
    destination = (result.metadata or {}).get("destination")
    if result.is_error or destination != "canvas":
        await _say(session, result.output, result.is_error)
