"""AU-21 — operator presence routes + viewSnapshot WS handler.

Frontend ``viewSnapshot.ts`` emits a debounced ``view_snapshot`` envelope
on every route change (and view-internal focus mutation). The handler
here keeps an in-memory ``operator_presence`` cache the autonomy layer
queries via :func:`get_presence`, and forwards a threshold-stamped
payload to the autonomy event bus for the operator-view mapper.

Per ``Docs/Plan/autonomy/phase-AU-21-mirror-vision.md``:

- No new event store — workspace_event.jsonl is deliberately *not*
  appended to. The presence cache + autonomy bus carry the signal;
  workspace events are reserved for operator-attended threads.
- No screenshots. Structured ``{view, view_state, since_ts}`` only.
- ``SECRET_KEY_RE`` redaction re-applied here as belt-and-braces (the
  frontend already redacts; we never trust the WS payload).
- Threshold detection lives here so the mapper stays pure: when the
  handler stamps ``long_dwell=True`` or ``repeat_switch=True``, the
  mapper lifts the event into an ``inform``-class agenda candidate;
  otherwise the bus event drains as ambient context.

Single-operator MVP: state lives per ``aiohttp.web.Application`` instance
so tests inject a fresh app without touching module-level state. The
in-process autonomy event bus is itself per-kernel, so the wiring fans
cleanly.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.publishers import publish_to_bus

log = logging.getLogger(__name__)


# Mirror's ``ui.ts::View`` union. ``ALLOWED_VIEWS`` is the server's
# authoritative gate: an unknown view is dropped before it reaches the
# cache or the bus, and only at debug level — so a view the frontend can
# send but this set omits loses its snapshots with nothing to show for it.
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


LONG_DWELL_SECONDS = 300.0  # 5 min
REPEAT_SWITCH_N = 3
SWITCH_COUNT_RETENTION_DAYS = 2  # prune counts older than this on every tick

_SECRET_KEY_RE = re.compile(r"(token|secret|password|api_?key|bot_?token)", re.I)
_REDACTED = "[redacted]"


# ``app`` keys.
PRESENCE_KEY = "operator_presence"
_DWELL_KEY = "_operator_view_dwell"
_SWITCH_COUNTS_KEY = "_operator_view_switch_counts"


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


def _prune_switch_counts(
    counts: dict[tuple[str, str], int],
    *,
    today_iso: str,
) -> None:
    """Drop entries whose ``date_iso`` is older than the retention window.
    Mutates in place; cheap (sub-millisecond) at any reasonable cardinality."""
    if not counts:
        return
    keep_dates: set[str] = set()
    today = datetime.fromisoformat(today_iso).date()
    for date_iso, _view in list(counts.keys()):
        try:
            d = datetime.fromisoformat(date_iso).date()
        except ValueError:
            counts.pop((date_iso, _view), None)
            continue
        age_days = (today - d).days
        # Strict less-than: SWITCH_COUNT_RETENTION_DAYS=2 keeps today (age=0)
        # and yesterday (age=1); drops age=2 and older.
        if age_days < SWITCH_COUNT_RETENTION_DAYS:
            keep_dates.add(date_iso)
    for key in list(counts.keys()):
        if key[0] not in keep_dates:
            counts.pop(key, None)


async def handle_view_snapshot(
    app: web.Application,
    session: Any,
    data: dict[str, Any] | None,
) -> None:
    """Process a ``view_snapshot`` WS envelope.

    Called from :func:`tesseract.mirror.server.ws._dispatch`. Updates
    the presence cache and publishes one ``operator_view`` event to the
    autonomy bus. The mapper inspects the threshold flags before
    emitting any agenda candidate.
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
    view_state = _redact(view_state_raw)

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    today_iso = now.date().isoformat()

    # Dwell tracking — per session_id so two concurrent operators (post-MVP)
    # don't fold their views into one bucket.
    dwell_map: dict[str, tuple[str, float]] = app.setdefault(_DWELL_KEY, {})
    session_id = getattr(session, "session_id", "") or "_anon"
    prev = dwell_map.get(session_id)

    long_dwell = False
    dwell_seconds = 0.0
    prev_view: str | None = None
    if prev is not None and prev[0] != view:
        prev_view, prev_since = prev
        dwell_seconds = max(0.0, now_ts - prev_since)
        if dwell_seconds >= LONG_DWELL_SECONDS:
            long_dwell = True
        counts: dict[tuple[str, str], int] = app.setdefault(
            _SWITCH_COUNTS_KEY, defaultdict(int)
        )
        _prune_switch_counts(counts, today_iso=today_iso)
        counts[(today_iso, view)] += 1
    if prev is None or prev[0] != view:
        dwell_map[session_id] = (view, now_ts)

    app[PRESENCE_KEY] = {
        "session_id": session_id,
        "view": view,
        "view_state": view_state,
        "since_ts": now.isoformat(),
    }

    counts = app.get(_SWITCH_COUNTS_KEY) or {}
    switch_count_today = int(counts.get((today_iso, view), 0))
    repeat_switch = (
        prev is not None
        and prev_view is not None
        and prev_view != view
        and switch_count_today >= REPEAT_SWITCH_N
    )

    # Codex audit-2 P1 #2 follow-on: pair repeat_switch with concrete
    # operational state. Bare repeat_switch was generating "propose as
    # default route" agenda spam (view-switch-chat at counts 3 → 9 on
    # the live system). The mapper now requires this flag to emit.
    paired_with_failure, failure_summary = _derive_paired_with_failure(app, view)

    payload = {
        "view": view,
        "view_state": view_state,
        "ts": now.isoformat(),
        "prev_view": prev_view,
        "dwell_seconds": round(dwell_seconds, 2),
        "long_dwell": long_dwell,
        "repeat_switch": repeat_switch,
        "switch_count_today": switch_count_today,
        "paired_with_failure": paired_with_failure,
        "failure_summary": failure_summary,
    }
    publish_to_bus(AgendaSource.OPERATOR_VIEW, payload)


def _derive_paired_with_failure(app: Any, view: str) -> tuple[bool, str]:
    """Read live operational state to decide whether the current view
    is showing pain. Returns ``(flag, summary)``. Best-effort — any
    exception logs at debug and returns ``(False, "")``.

    Pairing rules per view (extend as new failure-bearing views ship):

    * ``recovery``        — pair when last_recovery has operator_attention items.
    * ``workers``         — pair when any worker is FAILED in active/ within the last hour.
    * ``approvals``       — pair when there are AWAITING_OPERATOR agenda items.
    * ``blocked``         — pair when there are agenda items in status BLOCKED OR
                            governor has any paused sources.

    Other views never pair — repeat-switch on those views is genuine UI
    preference, not operational pain.
    """
    try:
        if view == "recovery":
            last = app.get("last_recovery") or {}
            attention = last.get("operator_attention") if isinstance(last, dict) else None
            if isinstance(attention, list) and attention:
                kinds = sorted({str(item.get("kind") or "") for item in attention if isinstance(item, dict)})
                return True, f"recovery: {len(attention)} item(s) need operator ({', '.join(kinds)[:200]})"
            return False, ""

        if view == "workers":
            from tesseract.orchestrator.workers.record import iter_active_status_summary
            failed = sum(1 for _wid, _kind, status in iter_active_status_summary() if status == "failed")
            if failed > 0:
                return True, f"{failed} worker(s) FAILED in active/"
            return False, ""

        if view == "approvals":
            store = app.get("agenda_store")
            if store is None:
                return False, ""
            from tesseract.orchestrator.autonomy.models import AgendaStatus
            count = sum(1 for it in store.iter_active() if it.status == AgendaStatus.AWAITING_OPERATOR)
            if count > 0:
                return True, f"{count} agenda item(s) awaiting operator"
            return False, ""

        if view == "blocked":
            store = app.get("agenda_store")
            if store is None:
                return False, ""
            from tesseract.orchestrator.autonomy.models import AgendaStatus
            blocked = sum(1 for it in store.iter_active() if it.status == AgendaStatus.BLOCKED)
            pause_store = app.get("pause_store")
            paused = 0
            if pause_store is not None:
                try:
                    paused = len(pause_store.reload())
                except Exception:
                    paused = 0
            if blocked > 0 or paused > 0:
                parts = []
                if blocked:
                    parts.append(f"{blocked} blocked agenda")
                if paused:
                    parts.append(f"{paused} paused source(s)")
                return True, " · ".join(parts)
            return False, ""

        return False, ""
    except Exception:
        log.debug("paired_with_failure: derivation failed for view=%r", view, exc_info=True)
        return False, ""


def register(app: web.Application) -> None:
    app.router.add_get("/api/operator/presence", get_operator_presence)


__all__ = [
    "ALLOWED_VIEWS",
    "LONG_DWELL_SECONDS",
    "PRESENCE_KEY",
    "REPEAT_SWITCH_N",
    "get_operator_presence",
    "get_presence",
    "handle_view_snapshot",
    "register",
]
