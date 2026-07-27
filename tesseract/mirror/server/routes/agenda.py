"""AgendaStore REST routes — AU-4 S2.

Contract:

- ``GET  /api/agenda``                   — list active items, ranked desc.
- ``GET  /api/agenda/{id}``              — single item (active + archive).
- ``POST /api/agenda``                   — operator-authored item (operator-session-gated).
- ``PATCH /api/agenda/{id}``             — mutate priority / risk_class /
                                            operator_priority / status
                                            (operator-session-gated).
- ``POST /api/agenda/{id}/cancel``       — terminal cancel transition
                                            (operator-session-gated).
- ``POST /api/agenda/{id}/approve``      — mark approval gate(s) fulfilled
                                            and bump propose → autonomous
                                            for one shot (operator-session-gated).

GET is anonymous-readable (Mirror polls without session context).
Mutating endpoints require an operator session matching the
``agent_promote`` / ``runtime/shutdown`` pattern: caller passes
``session_id`` resolving to an ``app["server_sessions"]`` entry with a
live ``ask_fn`` (i.e. an actual operator-attended chat session). Anonymous
+ channel-routed (Telegram bridge etc.) callers get 401.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from tesseract.orchestrator.autonomy.agenda_comments import (
    MAX_BODY_CHARS as COMMENT_MAX_BODY_CHARS,
    append_comment,
    list_comments,
)
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.broadcast import (
    broadcast_agenda_comment_event,
    broadcast_agenda_event,
)
from tesseract.orchestrator.autonomy.governor import (
    PauseStore,
    REASON_OPERATOR_UNPAUSE,
)
from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)

log = logging.getLogger(__name__)


_OPERATOR_PRIORITY_MIN = -2
_OPERATOR_PRIORITY_MAX = 5


def _get_store(request: web.Request) -> AgendaStore:
    """Resolve the per-app AgendaStore singleton; falls back to a fresh instance."""
    return request.app.get("agenda_store") or AgendaStore()


def _require_operator_session(request: web.Request, body: dict[str, Any]) -> web.Response | None:
    """Mirror the ``agent_promote`` auth shape: session_id must resolve to an operator chat session with a live ask_fn."""
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


async def _read_json_body(request: web.Request) -> dict[str, Any] | web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    return body


async def _authed_body(
    request: web.Request,
) -> tuple[dict[str, Any], None] | tuple[None, web.Response]:
    """Parse the JSON body and verify operator session in one step.

    Returns ``(body, None)`` on success, ``(None, error_response)`` on failure.
    """
    body = await _read_json_body(request)
    if isinstance(body, web.Response):
        return None, body
    err = _require_operator_session(request, body)
    if err is not None:
        return None, err
    return body, None


def _payload(item: AgendaItem) -> dict[str, Any]:
    return item.model_dump(mode="json")


# -- GET handlers --------------------------------------------------------


async def list_items(request: web.Request) -> web.Response:
    """GET /api/agenda — every active item, ranked by priority_score."""
    store = _get_store(request)
    items = store.ranked()
    return web.json_response({"items": [_payload(i) for i in items]})


async def get_item(request: web.Request) -> web.Response:
    """GET /api/agenda/{id} — active or archived."""
    store = _get_store(request)
    item_id = request.match_info["id"]
    try:
        item = store.get(item_id)
    except ValidationError as exc:
        return web.json_response(
            {"error": f"agenda item {item_id!r} is malformed: {exc}"},
            status=500,
        )
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)
    return web.json_response(_payload(item))


# -- mutating handlers ---------------------------------------------------


async def create_item(request: web.Request) -> web.Response:
    """POST /api/agenda — operator-authored item.

    Body: ``{session_id, goal, [rationale], [risk_class], [operator_priority]}``.
    Source is forced to ``operator`` for this endpoint — observer / kernel
    mappers go through AU-5, not this REST surface.
    """
    body, err = await _authed_body(request)
    if err is not None:
        return err

    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return web.json_response({"error": "goal required (non-empty string)"}, status=400)
    rationale = body.get("rationale") or ""
    if not isinstance(rationale, str):
        return web.json_response({"error": "rationale must be a string"}, status=400)
    risk_raw = body.get("risk_class") or RiskClass.PROPOSE.value
    try:
        risk = RiskClass(risk_raw)
    except ValueError:
        return web.json_response(
            {"error": f"invalid risk_class {risk_raw!r}"}, status=400
        )
    try:
        op_priority_int = int(body.get("operator_priority", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "operator_priority must be int"}, status=400)
    if not (_OPERATOR_PRIORITY_MIN <= op_priority_int <= _OPERATOR_PRIORITY_MAX):
        return web.json_response(
            {"error": f"operator_priority out of range [{_OPERATOR_PRIORITY_MIN}, {_OPERATOR_PRIORITY_MAX}]"},
            status=400,
        )

    store = _get_store(request)
    existing = store.find_dedupe(goal, AgendaSource.OPERATOR)
    if existing is not None:
        return web.json_response(
            {"item": _payload(existing), "deduped": True},
            status=200,
        )

    now = datetime.now(timezone.utc)
    item = AgendaItem(
        id=mint_agenda_id(goal[:40], now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal=goal,
        rationale=rationale,
        risk_class=risk,
        operator_priority=op_priority_int,
    )
    try:
        store.add(item, by="operator", reason="operator_created")
    except (ValueError, ValidationError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    await broadcast_agenda_event(request.app, "agenda_item_added", item)
    return web.json_response({"item": _payload(item), "deduped": False}, status=201)


async def patch_item(request: web.Request) -> web.Response:
    """PATCH /api/agenda/{id} — adjust operator_priority / risk_class /
    rationale. Status transitions go through the dedicated endpoints so
    the index ts + reason capture is clean.

    Body: ``{session_id, [operator_priority], [risk_class], [rationale]}``.
    """
    body, err = await _authed_body(request)
    if err is not None:
        return err

    store = _get_store(request)
    item_id = request.match_info["id"]
    item = store.get(item_id)
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)
    if item.is_terminal():
        return web.json_response(
            {"error": f"agenda item {item_id!r} is terminal ({item.status.value})"},
            status=409,
        )

    changed = False
    if "operator_priority" in body:
        try:
            new_priority = int(body["operator_priority"])
        except (TypeError, ValueError):
            return web.json_response({"error": "operator_priority must be int"}, status=400)
        if not (_OPERATOR_PRIORITY_MIN <= new_priority <= _OPERATOR_PRIORITY_MAX):
            return web.json_response(
                {"error": f"operator_priority out of range [{_OPERATOR_PRIORITY_MIN}, {_OPERATOR_PRIORITY_MAX}]"},
                status=400,
            )
        item.operator_priority = new_priority
        changed = True
    if "risk_class" in body:
        try:
            new_risk = RiskClass(body["risk_class"])
        except ValueError:
            return web.json_response(
                {"error": f"invalid risk_class {body['risk_class']!r}"}, status=400
            )
        if new_risk == RiskClass.ABSOLUTE_DENY:
            return web.json_response(
                {"error": "cannot patch risk_class to absolute_deny"},
                status=400,
            )
        item.risk_class = new_risk
        changed = True
    if "rationale" in body:
        rationale = body["rationale"]
        if not isinstance(rationale, str):
            return web.json_response({"error": "rationale must be a string"}, status=400)
        if len(rationale) > 2000:
            return web.json_response({"error": "rationale exceeds 2000 chars"}, status=400)
        item.rationale = rationale
        changed = True

    if not changed:
        return web.json_response({"item": _payload(item), "noop": True})

    item.updated_at = datetime.now(timezone.utc)
    store.save(item)
    await broadcast_agenda_event(request.app, "agenda_item_updated", item)
    return web.json_response({"item": _payload(item), "noop": False})


async def cancel_item(request: web.Request) -> web.Response:
    """POST /api/agenda/{id}/cancel — operator-driven terminal transition."""
    body, err = await _authed_body(request)
    if err is not None:
        return err

    store = _get_store(request)
    item_id = request.match_info["id"]
    item = store.get(item_id)
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)
    if item.is_terminal():
        return web.json_response(
            {"item": _payload(item), "already_terminal": True}, status=200,
        )
    reason = body.get("reason") or "operator_cancel"
    prior_status = item.status.value
    store.transition(item, AgendaStatus.CANCELLED, reason=reason, by="operator")
    await broadcast_agenda_event(
        request.app, "agenda_item_transitioned", item, prior_status=prior_status
    )
    return web.json_response({"item": _payload(item), "already_terminal": False})


async def approve_item(request: web.Request) -> web.Response:
    """POST /api/agenda/{id}/approve — mark approval gate(s) fulfilled.

    Body: ``{session_id, [gate_kinds]}``. ``gate_kinds`` is a list of
    ApprovalKind strings; omitting it fulfils every unfulfilled gate.
    """
    body, err = await _authed_body(request)
    if err is not None:
        return err

    store = _get_store(request)
    item_id = request.match_info["id"]
    item = store.get(item_id)
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)
    if item.is_terminal():
        return web.json_response(
            {"error": f"agenda item {item_id!r} is terminal ({item.status.value})"},
            status=409,
        )

    gate_kinds_raw = body.get("gate_kinds")
    if gate_kinds_raw is None:
        target_kinds: set[str] | None = None
    else:
        if not isinstance(gate_kinds_raw, list):
            return web.json_response({"error": "gate_kinds must be a list"}, status=400)
        target_kinds = {str(k) for k in gate_kinds_raw}

    session_id = body["session_id"]  # validated by _require_operator_session
    now = datetime.now(timezone.utc)
    fulfilled_count = 0
    for gate in item.approvals_required:
        if gate.fulfilled:
            continue
        if target_kinds is not None and gate.kind not in target_kinds:
            continue
        gate.fulfilled = True
        gate.fulfilled_at = now
        gate.fulfilled_by = session_id
        fulfilled_count += 1

    # Fix 3 — once every gate is fulfilled the item must leave
    # ``AWAITING_OPERATOR`` so the kernel picks it up on the next tick.
    # Kernel iterates only ``PROPOSED`` items; leaving the item in
    # awaiting is the "Approved toast but nothing changes" symptom.
    # Evaluated BEFORE the noop short-circuit so re-approving an item
    # that was fulfilled-but-not-transitioned (legacy data, or
    # fulfilled by a parallel writer) still releases it.
    can_transition = (
        item.status == AgendaStatus.AWAITING_OPERATOR
        and all(g.fulfilled for g in item.approvals_required)
    )

    if fulfilled_count == 0 and not can_transition:
        return web.json_response(
            {"item": _payload(item), "fulfilled_count": 0, "noop": True},
        )
    item.updated_at = now

    transitioned = False
    prior_status = item.status.value
    if can_transition:
        store.transition(
            item,
            AgendaStatus.PROPOSED,
            reason="approvals_satisfied",
            by="operator",
        )
        transitioned = True
        operator_journal.append(
            "approval",
            {
                "agenda_item_id": item.id,
                "summary": item.goal,
                "by": "operator",
                "session_id": session_id,
                "prior_status": prior_status,
            },
        )
    else:
        store.save(item)

    event_type = "agenda_item_transitioned" if transitioned else "agenda_item_updated"
    await broadcast_agenda_event(
        request.app,
        event_type,
        item,
        prior_status=prior_status if transitioned else None,
    )

    return web.json_response(
        {
            "item": _payload(item),
            "fulfilled_count": fulfilled_count,
            "noop": False,
            "transitioned": transitioned,
        },
    )


# -- comment threads -----------------------------------------------------


def _comment_payload(comment: Any) -> dict[str, Any]:
    return {
        "id": comment.id,
        "at": comment.at.isoformat(),
        "role": comment.role,
        "by": comment.by,
        "body": comment.body,
    }


async def list_item_comments(request: web.Request) -> web.Response:
    """GET /api/agenda/{id}/comments — full thread for the item.

    Anonymous-readable (matches ``GET /api/agenda``). 404 only if the
    item itself is unknown; an item with no comments returns
    ``{comments: []}`` so the dashboard renders an empty thread instead
    of an error.
    """
    store = _get_store(request)
    item_id = request.match_info["id"]
    item = store.get(item_id)
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)
    comments = list_comments(item_id)
    return web.json_response({
        "item_id": item_id,
        "comments": [_comment_payload(c) for c in comments],
    })


async def post_item_comment(request: web.Request) -> web.Response:
    """POST /api/agenda/{id}/comments — operator-session-gated append.

    Body: ``{session_id, body}``. The route is operator-only —
    bidirectional thread schema supports an ``agent`` role too, but
    agent-side writes go through the tool path (a future
    ``ask_clarification``-style flow), not this endpoint. 400 on empty
    or over-length body; 404 on unknown item.
    """
    body, err = await _authed_body(request)
    if err is not None:
        return err

    store = _get_store(request)
    item_id = request.match_info["id"]
    item = store.get(item_id)
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)

    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        return web.json_response({"error": "body must be a non-empty string"}, status=400)
    if len(text) > COMMENT_MAX_BODY_CHARS:
        return web.json_response(
            {"error": f"body exceeds {COMMENT_MAX_BODY_CHARS} chars"},
            status=400,
        )

    session_id = body["session_id"]
    try:
        comment = append_comment(item_id, role="operator", by=session_id, body=text)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    payload = _comment_payload(comment)
    await broadcast_agenda_comment_event(
        request.app,
        "agenda_comment_added",
        item_id=item_id,
        comment=payload,
    )

    # Fire a background TARS reply so the operator's question gets answered
    # in-thread (the workspace comment surface already auto-replies; this
    # closes the agenda gap). Fire-and-forget + a fresh controller session
    # per call → replies across items run in parallel and a slow/failed
    # dispatch never blocks this POST. Best-effort: the operator comment is
    # already durable regardless.
    try:
        from tesseract.mirror.server.ws import _spawn_tracked
        from tesseract.orchestrator.autonomy.agenda_reply import (
            dispatch_agenda_reply,
            load_agenda_reply_config,
        )

        cfg = load_agenda_reply_config()
        if cfg.enabled:
            thread = list_comments(item_id)
            _spawn_tracked(
                request.app,
                dispatch_agenda_reply(
                    request.app, item=item, thread=thread, config=cfg,
                ),
                name=f"agenda-reply:{item_id}",
            )
    except Exception:
        log.exception("agenda: failed to spawn comment auto-reply")

    return web.json_response({"item_id": item_id, "comment": payload}, status=201)


# -- resume --------------------------------------------------------------


async def resume_item(request: web.Request) -> web.Response:
    """POST /api/agenda/{id}/resume — re-queue a BLOCKED item.

    Body: ``{session_id}``. Operator-session-gated. Transitions an
    item in ``blocked`` status back to ``proposed`` so the kernel
    re-picks it on the next tick — a fresh worker is dispatched with
    the same goal. The prior worker's record stays archived for audit.

    No-op (200 + ``noop: True``) if the item is not BLOCKED. 404 if
    the item is unknown. Refuses terminal items (cancelled / done /
    abandoned / superseded) with 409 — they cannot be revived.

    The operator typically raises ``agenda.yaml::worker_timeouts``
    BEFORE clicking resume so the fresh worker has a longer budget.
    Resume itself does not change the timeout — that is yaml-driven.
    """
    body, err = await _authed_body(request)
    if err is not None:
        return err

    store = _get_store(request)
    item_id = request.match_info["id"]
    item = store.get(item_id)
    if item is None:
        return web.json_response({"error": f"agenda item {item_id!r} not found"}, status=404)
    if item.is_terminal():
        return web.json_response(
            {"error": f"agenda item {item_id!r} is terminal ({item.status.value})"},
            status=409,
        )
    if item.status != AgendaStatus.BLOCKED:
        return web.json_response(
            {"item": _payload(item), "noop": True, "status": item.status.value},
        )

    prior_status = item.status.value
    item.blocked_reason = None
    store.transition(
        item, AgendaStatus.PROPOSED,
        reason="operator_resume",
        by="operator",
    )
    await broadcast_agenda_event(
        request.app, "agenda_item_transitioned", item, prior_status=prior_status,
    )
    return web.json_response(
        {"item": _payload(item), "noop": False, "transitioned": True},
    )


# -- governor surface ----------------------------------------------------


def _get_pause_store(request: web.Request) -> PauseStore:
    """Resolve the per-app PauseStore singleton; fall back to a fresh
    instance so reads work even before ``_start_autonomy_kernel`` wires one."""
    return request.app.get("autonomy_pause_store") or PauseStore()


async def list_source_pauses(request: web.Request) -> web.Response:
    """GET /api/agenda/sources/pauses — every active pause. Anonymous-readable."""
    store = _get_pause_store(request)
    pauses = store.all_paused()
    return web.json_response(
        {"pauses": [p.to_payload() for p in pauses.values()]}
    )


async def unpause_source(request: web.Request) -> web.Response:
    """POST /api/agenda/sources/{name}/unpause — operator-driven clear.

    Body: ``{session_id, [reason]}``. Mirrors the ``cancel_item`` auth
    shape so a Telegram-routed call gets 401; only an operator-attended
    chat session can clear a Governor pause. The governor never
    auto-clears — only the operator does.
    """
    body, err = await _authed_body(request)
    if err is not None:
        return err

    name = request.match_info["name"]
    try:
        source = AgendaSource(name)
    except ValueError:
        return web.json_response(
            {"error": f"unknown agenda source {name!r}"}, status=400
        )

    reason = body.get("reason") or REASON_OPERATOR_UNPAUSE
    if not isinstance(reason, str):
        return web.json_response({"error": "reason must be a string"}, status=400)

    store = _get_pause_store(request)
    pause = store.remove(source, by="operator", reason=reason)
    if pause is None:
        return web.json_response(
            {"source": source.value, "was_paused": False}, status=200
        )

    # Mirror the kernel cache too — the kernel reads ``_paused_sources``
    # in-memory between ticks; without this nudge the source would stay
    # parked until the next backend boot. The kernel's resume_source
    # writes back through to the store, which we've already mutated;
    # passing ``by`` makes the audit log entry attribute correctly.
    kernel = request.app.get("autonomy_kernel")
    if kernel is not None:
        try:
            kernel._paused_sources.discard(source)
        except Exception:
            log.exception("agenda: kernel paused_sources discard failed")

    return web.json_response(
        {
            "source": source.value,
            "was_paused": True,
            "pause": pause.to_payload(),
        },
        status=200,
    )


# -- registration --------------------------------------------------------


def register(app: web.Application) -> None:
    """Wire the agenda routes. Called from ``app.py::_routes``."""
    app["agenda_store"] = AgendaStore()
    # Default PauseStore if the lifecycle hasn't built a kernel-shared one
    # yet. ``_start_autonomy_kernel`` runs in ``on_startup`` *after* route
    # registration; both call sites consult ``app.get(...)`` and assign
    # only when absent, so the kernel + governor + REST end up sharing
    # the same instance.
    if app.get("autonomy_pause_store") is None:
        app["autonomy_pause_store"] = PauseStore()
    app.router.add_get("/api/agenda", list_items)
    app.router.add_get("/api/agenda/{id}", get_item)
    app.router.add_post("/api/agenda", create_item)
    app.router.add_patch("/api/agenda/{id}", patch_item)
    app.router.add_post("/api/agenda/{id}/cancel", cancel_item)
    app.router.add_post("/api/agenda/{id}/approve", approve_item)
    app.router.add_post("/api/agenda/{id}/resume", resume_item)
    app.router.add_get("/api/agenda/{id}/comments", list_item_comments)
    app.router.add_post("/api/agenda/{id}/comments", post_item_comment)
    app.router.add_get("/api/agenda/sources/pauses", list_source_pauses)
    app.router.add_post("/api/agenda/sources/{name}/unpause", unpause_source)


__all__ = [
    "approve_item",
    "cancel_item",
    "create_item",
    "get_item",
    "list_item_comments",
    "list_items",
    "list_source_pauses",
    "patch_item",
    "post_item_comment",
    "register",
    "resume_item",
    "unpause_source",
]
