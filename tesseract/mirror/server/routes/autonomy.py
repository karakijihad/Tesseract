"""AU-7 S1 — read-only dashboard endpoints.

Three GETs that feed the Mirror Autonomy Dashboard's default view:

- ``GET /api/workers/active`` — every record under ``workers/active/``,
  serialized for the WorkersPane. Cheap walk; the dashboard polls on
  reconnect and otherwise relies on WS ``worker_record_*`` envelopes.
- ``GET /api/governor/state`` — Governor running state, config, last
  detector tick (``GovernorTickResult.at`` + counts), and the live
  source-pause map. Reads via ``app["autonomy_governor"]`` /
  ``app["autonomy_pause_store"]``; falls back to ``running=False`` +
  empty pauses if neither is wired yet (early boot or AU-6-disabled
  test app).
- ``GET /api/recovery/latest`` — same payload the Settings Runtime
  pane reads off ``/api/runtime/status::last_recovery``, surfaced on
  its own path so the dashboard's RecoveryPane doesn't have to
  unpack the supervisor envelope.

All three are anonymous-readable (matches the AU-4 ``GET /api/agenda``
shape). Mutating actions land in S2.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.completion import completion_payload
from tesseract.orchestrator.autonomy.governor import (
    Governor,
    GovernorConfig,
    GovernorTickResult,
    PauseStore,
)
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel
from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy import prune_ledger
from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus
from tesseract.orchestrator.workers.record import (
    WorkerRecord,
    list_active_records,
    load_record,
)

log = logging.getLogger(__name__)


# -- workers -------------------------------------------------------------


def _worker_payload(record: WorkerRecord) -> dict[str, Any]:
    """Dashboard-facing projection. Keeps the payload tight so the
    poll stays cheap — omits transcripts and full status history."""
    last_transition = record.status_history[-1] if record.status_history else None
    return {
        "id": record.id,
        "kind": record.kind.value,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "agenda_item_id": record.agenda_item_id,
        "mission_id": record.mission_id,
        "risk_class": record.risk_class.value,
        "role": record.role,
        "status": record.status.value,
        # `status` is where the worker ended; `outcome` is what came of it.
        # `null` on records written before the vocabulary existed — render
        # that as unknown, not as success.
        "outcome": record.outcome.value if record.outcome else None,
        "outcome_reason": record.outcome_reason,
        "last_heartbeat": (
            record.last_heartbeat.isoformat() if record.last_heartbeat else None
        ),
        "tokens_in": record.tokens_in,
        "tokens_out": record.tokens_out,
        "cost_usd": record.cost_usd,
        "billing": record.billing.value,
        "duration_seconds": record.duration_seconds,
        "retry_count": record.retry_count,
        "summary": record.summary,
        "last_transition": (
            {
                "at": last_transition.at.isoformat(),
                "from_status": last_transition.from_status,
                "to_status": last_transition.to_status,
                "reason": last_transition.reason,
            }
            if last_transition is not None
            else None
        ),
    }


async def list_active_workers(request: web.Request) -> web.Response:
    """GET /api/workers/active — every worker under ``workers/active/``.

    Sorted newest-first by ``updated_at`` so the dashboard's WorkersPane
    surfaces fresh activity at the top without client-side sort. The
    pane caps render count, so ordering carries the load.
    """
    records = list_active_records()
    records.sort(key=lambda r: r.updated_at, reverse=True)
    return web.json_response({"workers": [_worker_payload(r) for r in records]})


def _worker_detail_payload(record: WorkerRecord) -> dict[str, Any]:
    """Full record projection for the WorkerDetailModal. Includes every
    field the list view omits (prompt, full status_history, error
    surface, transcript path, artifacts) so the operator can audit
    one worker end-to-end without a second fetch."""
    return {
        **_worker_payload(record),
        "prompt": record.prompt,
        "inputs": record.inputs,
        "worktree_path": record.worktree_path,
        "pid": record.pid,
        "pane_id": record.pane_id,
        "cli_invocation": record.cli_invocation,
        "exit_code": record.exit_code,
        "error_class": record.error_class,
        "error_message": record.error_message,
        "transcript_path": record.transcript_path,
        "parent_worker_id": record.parent_worker_id,
        "status_history": [
            {
                "at": t.at.isoformat(),
                "from_status": t.from_status,
                "to_status": t.to_status,
                "reason": t.reason,
            }
            for t in record.status_history
        ],
        "artifacts": [
            {
                "path": a.path,
                "kind": a.kind,
                "size_bytes": a.size_bytes,
                "sha256": a.sha256,
            }
            for a in record.artifacts
        ],
    }


async def get_worker_detail(request: web.Request) -> web.Response:
    """GET /api/workers/{id} — full record projection (active or archive).

    404 if the id parses but no record file is on disk (active OR archive
    bucket — ``load_record`` walks both). The modal won't open against an
    archived record today, but the route is symmetric so a future "open
    a recent terminal" deep link works without backend changes.
    """
    worker_id = request.match_info["id"]
    record = load_record(worker_id)
    if record is None:
        return web.json_response({"error": f"worker {worker_id!r} not found"}, status=404)
    return web.json_response({"worker": _worker_detail_payload(record)})


# -- governor ------------------------------------------------------------


def _config_payload(config: GovernorConfig) -> dict[str, Any]:
    return {
        "cadence_seconds": config.cadence_seconds,
        "loop_n": config.loop_n,
        "loop_window_hours": config.loop_window_hours,
        "cost_threshold_multiplier": config.cost_threshold_multiplier,
        "trust_consecutive_rejections": config.trust_consecutive_rejections,
    }


def _last_tick_payload(tick: GovernorTickResult) -> dict[str, Any]:
    return {
        "at": tick.at.isoformat(),
        "pauses_added": [p.source.value for p in tick.pauses_added],
        "workers_cancelled": list(tick.workers_cancelled),
        "items_blocked": list(tick.items_blocked),
    }


async def get_governor_state(request: web.Request) -> web.Response:
    """GET /api/governor/state — running flag + config + last tick + pauses.

    The pauses block duplicates ``GET /api/agenda/sources/pauses`` on
    purpose: AU-7's dashboard fetches the dashboard surface once and
    renders everything from that single response (BlockedPane reads
    ``governor.pauses`` rather than firing a second request).
    """
    governor: Governor | None = request.app.get("autonomy_governor")
    store: PauseStore | None = (
        request.app.get("autonomy_pause_store") or PauseStore()
    )
    pauses = store.all_paused()
    payload: dict[str, Any] = {
        "running": bool(governor and governor.is_running),
        "config": (
            _config_payload(governor.config)
            if governor is not None
            else _config_payload(GovernorConfig())
        ),
        "last_tick": (
            _last_tick_payload(governor.last_tick)
            if governor is not None and governor.last_tick is not None
            else None
        ),
        "pauses": [p.to_payload() for p in pauses.values()],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return web.json_response(payload)


# -- recovery ------------------------------------------------------------


def filter_live_attention(
    attention: list[dict[str, Any]],
    agenda_store: AgendaStore | None,
) -> list[dict[str, Any]]:
    """Reconcile ``operator_attention`` against the live agenda store.

    The recovery summary is captured once at boot and never refreshed,
    so an agenda item the operator approved post-boot still appears in
    the snapshot. Drop ``kind == "agenda"`` entries whose item is no
    longer ``AWAITING_OPERATOR`` (or no longer exists). Non-agenda kinds
    are left untouched — the cheap O(1) liveness check only fits agenda
    items.
    """
    if not attention:
        return []
    if agenda_store is None:
        return list(attention)
    live: list[dict[str, Any]] = []
    for entry in attention:
        if entry.get("kind") != "agenda":
            live.append(entry)
            continue
        item_id = entry.get("id")
        if not item_id:
            continue
        try:
            item = agenda_store.get(item_id)
        except Exception:
            log.warning("recovery: agenda lookup failed for %s", item_id, exc_info=True)
            live.append(entry)
            continue
        if item is not None and item.status == AgendaStatus.AWAITING_OPERATOR:
            live.append(entry)
    return live


def _serialize_recovery(
    summary: Any,
    *,
    agenda_store: AgendaStore | None = None,
) -> dict[str, Any] | None:
    """Same projection the runtime status route uses. Duplicated here
    rather than imported so the two paths stay loosely coupled — a
    future recovery-shape change shouldn't have to touch ``runtime.py``
    just because the dashboard wanted a new field. ``operator_attention``
    is reconciled against the live agenda store so a stale boot snapshot
    doesn't out-live the items it points at."""
    if summary is None:
        return None
    try:
        payload = summary.to_payload()
    except Exception:
        log.exception("autonomy: serialize last_recovery failed")
        return None
    return {
        "boot_id": payload.get("boot_id"),
        "downtime_seconds": payload.get("downtime_seconds"),
        "scans": payload.get("scans") or {},
        "operator_attention": filter_live_attention(
            payload.get("operator_attention") or [],
            agenda_store,
        ),
        "started_at": (
            summary.started_at.isoformat()
            if hasattr(summary, "started_at") and summary.started_at is not None
            else None
        ),
    }


async def get_latest_recovery(request: web.Request) -> web.Response:
    """GET /api/recovery/latest — most recent RecoveryManager pass.

    Returns ``{recovery: null, state: "recovering"|"ready"}`` when no
    pass has run yet in this backend lifetime. The state field mirrors
    ``app["recovery_state"]`` so the dashboard can render a "Recovery
    in progress" banner without polling ``/api/runtime/status`` too.
    """
    return web.json_response(
        {
            "recovery": _serialize_recovery(
                request.app.get("last_recovery_summary"),
                agenda_store=request.app.get("agenda_store"),
            ),
            "state": request.app.get("recovery_state") or "ready",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


# -- registration --------------------------------------------------------


async def get_operator_journal(request: web.Request) -> web.Response:
    """GET /api/autonomy/journal?limit=N&days=D — reverse-chronological
    rows from the operator journal. Missing dir returns ``{"rows": []}``
    so a fresh install renders an empty pane rather than 500-ing.
    """
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    try:
        days = int(request.query.get("days", "7"))
    except ValueError:
        days = 7
    limit = max(1, min(limit, 500))
    days = max(1, min(days, 30))
    rows = operator_journal.read_recent(limit=limit, days=days)
    return web.json_response({"rows": rows, "limit": limit, "days": days})


# -- pruned ledger + source mute -----------------------------------------


_PRUNED_DEFAULT_WINDOW_HOURS = 168
_PRUNED_MAX_WINDOW_HOURS = 720


async def get_pruned_ledger(request: web.Request) -> web.Response:
    """GET /api/autonomy/pruned?window_hours=168 — the admission gate's
    prune ledger. ``records`` is the newest 200 raw prunes (any window);
    ``counts`` buckets ``{source: {stage: count}}`` over ``window_hours``
    so the dashboard can flag a recurrent-useless source. Anonymous-
    readable, matching the module's other GETs.
    """
    try:
        window_hours = int(
            request.query.get("window_hours", str(_PRUNED_DEFAULT_WINDOW_HOURS))
        )
    except ValueError:
        window_hours = _PRUNED_DEFAULT_WINDOW_HOURS
    window_hours = max(1, min(window_hours, _PRUNED_MAX_WINDOW_HOURS))
    records = prune_ledger.read_prunes(limit=200)
    counts = prune_ledger.prune_counts(window_hours=window_hours)
    return web.json_response(
        {
            "records": [r.model_dump(mode="json") for r in records],
            "counts": counts,
        }
    )


async def get_completion(request: web.Request) -> web.Response:
    """GET /api/autonomy/completion — completion rate per agenda source and
    outcome counts per worker lane, derived from the agenda index and the
    worker records. A source with nothing finished reports
    ``completion_rate: null``, which a surface must render as "no data" and
    never as zero.
    """
    payload = await asyncio.to_thread(completion_payload)
    return web.json_response(payload)


def _require_operator_session(
    request: web.Request, body: dict[str, Any],
) -> web.Response | None:
    """Mirror ``routes/agenda.py``'s auth shape for mutating governor
    actions: ``session_id`` must resolve to a connected operator chat
    session. Duplicated rather than imported so the two route modules
    stay loosely coupled (same convention as this module's
    ``_serialize_recovery``)."""
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return web.json_response(
            {"error": "session_id required (operator chat session)"},
            status=401,
        )
    server_session = request.app.get("server_sessions", {}).get(session_id)
    if server_session is None or getattr(
        getattr(server_session, "chat_session", None), "ask_fn", None,
    ) is None:
        return web.json_response(
            {"error": f"operator session {session_id!r} not connected"},
            status=401,
        )
    return None


async def _authed_body(
    request: web.Request,
) -> tuple[dict[str, Any], None] | tuple[None, web.Response]:
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response(
            {"error": "body must be a JSON object"}, status=400
        )
    err = _require_operator_session(request, body)
    if err is not None:
        return None, err
    return body, None


def _resolve_mute_source(
    request: web.Request,
) -> tuple[AgendaSource, AutonomyKernel] | web.Response:
    """Shared validation for mute/unmute: unknown source → 400, no
    kernel mounted (CLI-only backend) → 503."""
    name = request.match_info["source"]
    try:
        source = AgendaSource(name)
    except ValueError:
        return web.json_response(
            {"error": f"unknown agenda source {name!r}"}, status=400
        )
    kernel: AutonomyKernel | None = request.app.get("autonomy_kernel")
    if kernel is None:
        return web.json_response(
            {"error": "autonomy kernel not mounted (CLI-only backend)"},
            status=503,
        )
    return source, kernel


async def mute_source(request: web.Request) -> web.Response:
    """POST /api/autonomy/source/{source}/mute — operator pause, reusing
    the Governor's existing pause path (``AutonomyKernel.pause_source``,
    the same call the loop/cost/trust detectors use) rather than a
    parallel mute mechanism. Operator-session-gated, same as
    ``routes/agenda.py::unpause_source``.
    """
    _, err = await _authed_body(request)
    if err is not None:
        return err
    resolved = _resolve_mute_source(request)
    if isinstance(resolved, web.Response):
        return resolved
    source, kernel = resolved
    kernel.pause_source(source, reason="operator_muted", detector="operator")
    return web.json_response({"source": source.value, "muted": True})


async def unmute_source(request: web.Request) -> web.Response:
    """POST /api/autonomy/source/{source}/unmute — clears the pause via
    ``AutonomyKernel.resume_source``, the same operator-unpause path
    ``routes/agenda.py::unpause_source`` drives. Operator-session-gated."""
    _, err = await _authed_body(request)
    if err is not None:
        return err
    resolved = _resolve_mute_source(request)
    if isinstance(resolved, web.Response):
        return resolved
    source, kernel = resolved
    kernel.resume_source(source, by="operator")
    return web.json_response({"source": source.value, "muted": False})


def register(app: web.Application) -> None:
    """Wire the dashboard read-only routes."""
    app.router.add_get("/api/workers/active", list_active_workers)
    app.router.add_get(r"/api/workers/{id:wk-[A-Za-z0-9_\-]+}", get_worker_detail)
    app.router.add_get("/api/governor/state", get_governor_state)
    app.router.add_get("/api/recovery/latest", get_latest_recovery)
    app.router.add_get("/api/autonomy/journal", get_operator_journal)
    app.router.add_get("/api/autonomy/pruned", get_pruned_ledger)
    app.router.add_get("/api/autonomy/completion", get_completion)
    app.router.add_post("/api/autonomy/source/{source}/mute", mute_source)
    app.router.add_post("/api/autonomy/source/{source}/unmute", unmute_source)


__all__ = [
    "get_completion",
    "get_governor_state",
    "get_latest_recovery",
    "get_operator_journal",
    "get_pruned_ledger",
    "get_worker_detail",
    "list_active_workers",
    "mute_source",
    "register",
    "unmute_source",
]
