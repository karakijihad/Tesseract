"""Operator presence — where the operator is looking, cached in memory.

Frontend ``viewSnapshot.ts`` emits a debounced ``view_snapshot`` envelope on
every route change (and view-internal focus mutation). The handler here keeps an
in-memory ``operator_presence`` cache, exposed as ``GET /api/operator/presence``
and read by the autonomy layer as context.

Contract:

- No new event store — ``workspace_event.jsonl`` is deliberately *not* appended
  to. Workspace events are reserved for operator-attended threads.
- No screenshots. Structured ``{view, view_state, layers, since_ts}`` only.
- ``_SECRET_KEY_RE`` redaction re-applied here as belt-and-braces (the frontend
  already redacts; we never trust the WS payload).

**This surface reports; it does not propose.** It used to publish every snapshot
to the autonomy bus, where a mapper turned dwell time and tab-switching into paid
work — inferring intent from ambient telemetry, which produced six agenda items
and no outcome. The cache stays because it is cheap and answering "what is the
operator looking at" is a fair question; deriving a task from the answer is not.

Single-operator MVP: state lives per ``aiohttp.web.Application`` instance so
tests inject a fresh app without touching module-level state.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


# Mirror's ``ui.ts::View`` union. ``ALLOWED_VIEWS`` is the server's
# authoritative gate: an unknown view is dropped before it reaches the
# cache, and only at debug level — so a view the frontend can send but this
# set omits loses its snapshots with nothing to show for it.
# Parity is enforced by ``tests/mcp_config_drift/test_allowed_views_parity.py``,
# because this set carried ``soul`` for a while after the tab became
# ``identity`` and every Identity snapshot was silently discarded.
ALLOWED_VIEWS: frozenset[str] = frozenset(
    {
        "autonomy",
        "orb",
        "chat",
        "terminal",
        "pulse",
        "identity",
        "schedule",
        "agents",
        "conscience",
        "channels",
        "workspace",
        "settings",
    }
)

_SECRET_KEY_RE = re.compile(r"(token|secret|password|api_?key|bot_?token)", re.I)
_REDACTED = "[redacted]"


# ``app`` key.
PRESENCE_KEY = "operator_presence"


def _redact(value: Any) -> Any:
    """Walk the value tree replacing any secret-shaped key with ``[redacted]``.
    Defence-in-depth: the frontend already redacts; we never trust the
    WS payload."""
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(k) else _redact(v))
            for k, v in value.items()
        }
    return value


def get_presence(app: web.Application) -> dict[str, Any] | None:
    """Latest cached operator presence, or ``None`` if no snapshot landed yet."""
    return app.get(PRESENCE_KEY)


async def get_operator_presence(request: web.Request) -> web.Response:
    """GET /api/operator/presence — current cache, anonymous-readable."""
    return web.json_response(
        {
            "presence": get_presence(request.app),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


async def handle_view_snapshot(
    app: web.Application,
    session: Any,
    data: dict[str, Any] | None,
) -> None:
    """Process a ``view_snapshot`` WS envelope — cache it, and nothing else.

    Called from :func:`tesseract.mirror.server.ws._dispatch`.
    """
    if not isinstance(data, dict):
        return
    view = data.get("view")
    if not isinstance(view, str) or view not in ALLOWED_VIEWS:
        log.debug("view_snapshot: unknown view %r ignored", view)
        return
    view_state_raw = data.get("view_state") or {}
    if not isinstance(view_state_raw, dict):
        view_state_raw = {}

    # Which panels are open, stacked how, and which has focus. The frontend
    # ships compiled into the app and updates on a different cadence to this
    # file, so an older client sending no `layers` is version skew, not an
    # error — it stores as an empty mapping and reads as "not reported".
    layers_raw = data.get("layers") or {}
    if not isinstance(layers_raw, dict):
        layers_raw = {}

    app[PRESENCE_KEY] = {
        "session_id": getattr(session, "session_id", "") or "_anon",
        "view": view,
        "view_state": _redact(view_state_raw),
        "layers": _redact(layers_raw),
        "since_ts": datetime.now(timezone.utc).isoformat(),
    }


def register(app: web.Application) -> None:
    app.router.add_get("/api/operator/presence", get_operator_presence)


__all__ = [
    "ALLOWED_VIEWS",
    "PRESENCE_KEY",
    "get_operator_presence",
    "get_presence",
    "handle_view_snapshot",
    "register",
]
