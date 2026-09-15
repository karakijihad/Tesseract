"""Workspace REST routes — Inbox + comment threads.

Endpoints:

- ``GET  /api/workspace/inbox``                — pending events (+ filter)
- ``GET  /api/workspace/event/{event_id}``     — single event + thread
- ``POST /api/workspace/event/{event_id}/decision``  — approve / reject
- ``POST /api/workspace/event/{event_id}/comment``   — append operator comment
- ``GET  /api/workspace/seen``                 — last-seen markers
- ``POST /api/workspace/seen``                 — update last-seen marker

The document-editor endpoints (`/api/workspace/docs`, `/api/workspace/doc`)
live in `workspace_docs.py`, mounted by `app.py` alongside these.

A future ``/api/workspace/stream`` would read the same events.jsonl
with a ``kind=stream`` filter.

Approve dispatcher
==================

This is the decision dispatcher: `apply_decision` and the HTTP shape around
it. The commit handlers it calls are split across sibling modules by domain
— `workspace_skill_commits.py` for `agent_approval` and the three
`skill_*` kinds, `workspace_proposal_commits.py` for the file/memory-edit
kinds, `workspace_dial_commits.py` for the two dial cards (`tuning_proposal`,
`working_set_proposal`). Every `_commit_*` name is imported here because
`apply_decision` calls it directly, which is also what keeps
`tesseract.mirror.server.routes.workspace._commit_skill_refinement` (etc.)
importable for existing tests without a separate re-export step.

Approve on a `change_proposal` event triggers a kind-aware commit via
`tesseract.kernel.workspace_changes.apply_change`. Concurrent
modification (file changed since the proposal was queued) returns 409
with the fresh diff so the operator can re-review.

Approve on a `soul_proposal` event (feedback consolidator's distilled
identity bullet) commits the bullet directly to SOUL.md's `Growth`
section — the consolidator card already shows the bullet text, so the
single Approve click is the operator's sole gate. The intermediate
``change_proposal`` step is skipped to avoid double-gating the same
decision.

Approve on a `feedback_proposal` or `feedback_sweep` event invokes the
`memory_promote` or `memory_save` tool with the payload's action — operator
decision is the gate, the memory store mutation runs synchronously, status
flips to ``applied``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes.workspace_dial_commits import (
    _commit_tuning_proposal,
    _commit_working_set_proposal,
)
from tesseract.mirror.server.routes.workspace_proposal_commits import (
    _commit_change_proposal,
    _commit_feedback_proposal,
    _commit_soul_proposal,
    _commit_vault_raw_ingest_batch,
    _commit_yaml_change_proposal,
)
from tesseract.mirror.server.routes.workspace_skill_commits import (
    _apply_skill_refinement,  # re-exported: called directly by existing tests
    _base_moved,  # re-exported: called directly by existing tests
    _commit_agent_approval,
    _commit_skill_approval,
    _commit_skill_refinement,
    _commit_skill_retirement,
    _subject_of,
)
from tesseract.mirror.server.routes.workspace_shared import _store
from tesseract.lib import last_seen
from tesseract.paths import ROOT  # re-exported: `_commit_yaml_change_proposal`
# moved to `workspace_proposal_commits.py` and reads its own `ROOT` there;
# several existing tests still monkeypatch `ws_routes.ROOT` defensively (they
# do not exercise `yaml_change_proposal`, so the patch is inert either way).
from tesseract.permissions.approval_log import record_ask
from tesseract.workspace_events import (
    WorkspaceComment,
    WorkspaceEvent,
)
from tesseract.workspace_events.events import RESOLVABLE_KINDS

log = logging.getLogger(__name__)


# `resolve` is the soft-close verb for informational events — threads
# that record something that already happened (the assistant post, dream-cycle
# nudge, operator-initiated thread) or where the system has nothing
# left to gate (session reflection: writes already committed during
# the reflect turn). Derived from `workspace_events.events.ANSWERABLE_WITH`
# rather than kept as its own list — see that mapping's docstring for
# why three separate copies of this is how a kind ships stuck.
#
# Gated kinds — `change_proposal`, `feedback_proposal`, `feedback_sweep`,
# `agent_approval`, `soul_proposal`, `mission_reflection_proposal` —
# MUST go through explicit approve/reject so the operator's decision
# is recorded; allowing `resolve` on those would erase the gate.
_RESOLVABLE_KINDS = RESOLVABLE_KINDS

# Per-event decision locks. `post_decision` reads the event, runs commit
# side-effects, then flips its status — a check-then-act span. Two concurrent
# Approves on the same event could both pass the pending check and both run the
# commit (the second then 409s on hash mismatch — invariant intact but
# confusing). A per-event_id asyncio.Lock serializes that span; different
# events never contend. The dict is not pruned — bounded by distinct decided
# event_ids (small for an operator inbox), and pruning a lock with a pending
# waiter would let a fresh request mint a second lock and re-open the race.
_decision_locks: dict[str, asyncio.Lock] = {}


def _decision_lock(event_id: str) -> asyncio.Lock:
    lock = _decision_locks.get(event_id)
    if lock is None:
        lock = asyncio.Lock()
        _decision_locks[event_id] = lock
    return lock


def _event_dict(ev: WorkspaceEvent, comments: list[WorkspaceComment]) -> dict[str, Any]:
    return {
        **ev.to_dict(),
        "comments": [c.to_dict() for c in comments],
    }


async def list_inbox(request: web.Request) -> web.Response:
    app = request.app
    store = _store(app)
    status_filter = request.query.get("status", "pending")
    status: Any = status_filter if status_filter != "all" else None
    events = store.list_events(status=status, limit=200)
    payload = []
    for ev in events:
        comments = store.list_comments(ev.event_id)
        payload.append(_event_dict(ev, comments))
    return web.json_response({"events": payload, "count": len(payload)})


async def get_event(request: web.Request) -> web.Response:
    app = request.app
    store = _store(app)
    event_id = request.match_info["event_id"]
    ev = store.get_event(event_id)
    if ev is None:
        return web.json_response({"error": "not_found"}, status=404)
    comments = store.list_comments(event_id)
    return web.json_response(_event_dict(ev, comments))


class DecisionError(Exception):
    """A decision that could not be made, with the reason a caller can render.

    Carries the same payload and status the route used to return inline, so the
    HTTP handler is a translation and every other caller gets the words rather
    than a status code it has no use for.
    """

    def __init__(self, payload: dict[str, Any], status: int) -> None:
        self.payload = payload
        self.status = status
        super().__init__(str(payload.get("detail") or payload.get("error") or "refused"))


async def apply_decision(
    app: web.Application,
    event_id: str,
    decision: str,
    *,
    reason: str | None = None,
    per_file: dict[str, str] | None = None,
    actor: str = "operator",
) -> tuple[WorkspaceEvent, list[WorkspaceComment]]:
    """Approve, reject, resolve or delete one workspace event.

    **The one place a decision happens, and it is no longer behind HTTP.**
    Everything the operator is asked in the cockpit lands in this inbox, and
    until this was liftable out of `post_decision` the only way to answer any
    of it was a `POST` from a browser on the same machine. Ruling 22 says every
    approval has to be answerable from a phone; measured, two of fourteen were,
    and the twelve that were not are all this one call. So it moved, unchanged,
    and the route below is now the HTTP shape around it rather than the thing
    itself.

    Raises `DecisionError` with the payload and status the route used to return.
    """
    store = _store(app)
    if decision not in {"approve", "reject", "resolve", "delete"}:
        raise DecisionError(
            {"error": "decision must be 'approve', 'reject', 'resolve', or 'delete'"},
            400,
        )

    ev = store.get_event(event_id)
    if ev is None:
        raise DecisionError({"error": "not_found"}, 404)
    # `resolve` is restricted to informational kinds. A pending
    # `change_proposal` / `mission_reflection_proposal` / feedback_* /
    # agent_approval MUST go through approve/reject so the decision is
    # recorded; a blanket `resolve` on those would silently bypass the gate.
    if decision == "resolve" and ev.kind not in _RESOLVABLE_KINDS:
        raise DecisionError(
            {
                "error": "resolve_not_permitted_for_kind",
                "detail": (
                    f"'resolve' is only valid for {sorted(_RESOLVABLE_KINDS)}; "
                    f"event kind '{ev.kind}' requires explicit approve or reject."
                ),
                "kind": ev.kind,
            },
            400,
        )

    async with _decision_lock(event_id):
        # Re-read under the lock. A concurrent decision may have settled it
        # while we waited; acting on the stale copy is the race this guards.
        ev = store.get_event(event_id)
        if ev is None:
            raise DecisionError({"error": "not_found"}, 404)
        # `delete` is the universal escape hatch — works on any status so the
        # operator can soft-delete from history too. Other verbs stay
        # pending-only and idempotently return the settled event.
        if decision != "delete" and ev.status not in {"pending"}:
            return ev, store.list_comments(event_id)

        applied_version = ""

        if decision == "approve" and ev.kind == "change_proposal":
            err = await _commit_change_proposal(app, ev)
            if err is not None:
                raise DecisionError(*err)

        if decision == "approve" and ev.kind == "yaml_change_proposal":
            _result, err = await _commit_yaml_change_proposal(app, ev)
            if err is not None:
                raise DecisionError(*err)

        if decision == "approve" and ev.kind == "soul_proposal":
            _result, err = await _commit_soul_proposal(app, ev)
            if err is not None:
                raise DecisionError(*err)

        if decision == "approve" and ev.kind == "working_set_proposal":
            _result, err = await _commit_working_set_proposal(app, ev)
            if err is not None:
                raise DecisionError(*err)

        if decision == "approve" and ev.kind == "tuning_proposal":
            _result, err = await _commit_tuning_proposal(app, ev)
            if err is not None:
                raise DecisionError(*err)

        # feedback_sweep reaches the same handler as feedback_proposal: its
        # payload names `action: memory_save` rather than `merge_into`/`archive`, and `_commit_feedback_proposal`
        # dispatches on that field.
        if decision == "approve" and ev.kind in {"feedback_proposal", "feedback_sweep"}:
            _result, err = await _commit_feedback_proposal(app, ev)
            if err is not None:
                raise DecisionError(*err)

        if decision in {"approve", "reject"} and ev.kind == "agent_approval":
            _agent_meta, err = await _commit_agent_approval(app, ev, decision, reason)
            if err is not None:
                raise DecisionError(*err)

        if decision in {"approve", "reject"} and ev.kind == "skill_approval":
            _skill_meta, err = await _commit_skill_approval(app, ev, decision, reason)
            if err is not None:
                raise DecisionError(*err)

        if decision in {"approve", "reject"} and ev.kind == "skill_refinement":
            refine_meta, err = await _commit_skill_refinement(app, ev, decision, reason)
            if err is not None:
                raise DecisionError(*err)
            applied_version = str((refine_meta or {}).get("applied_version") or "")

        if decision in {"approve", "reject"} and ev.kind == "skill_retirement":
            _retirement_meta, err = await _commit_skill_retirement(app, ev, decision, reason)
            if err is not None:
                raise DecisionError(*err)

        if decision in {"approve", "reject"} and ev.kind == "vault_raw_ingest_batch":
            _result, err = await _commit_vault_raw_ingest_batch(
                app, ev, deny_all=(decision == "reject"), per_file=per_file
            )
            if err is not None:
                raise DecisionError(*err)

        # `resolve` flips status without firing approval side-effects — the
        # verb operator_post threads need to leave the inbox once the
        # conversation has played out. `delete` is the soft-delete verb.
        # `applied` distinguishes "the operator approved AND the side-effect
        # ran" from the generic approval state.
        if decision == "approve" and ev.kind in {
            "yaml_change_proposal",
            "vault_raw_ingest_batch",
            "soul_proposal",
            "feedback_proposal",
            "feedback_sweep",
            "agent_approval",
            "skill_approval",
            "skill_refinement",
            "skill_retirement",
            "working_set_proposal",
            "tuning_proposal",
        }:
            new_status = "applied"
        else:
            new_status = (
                "approved" if decision == "approve"
                else "rejected" if decision == "reject"
                else "resolved" if decision == "resolve"
                else "deleted"
            )
        updated = store.update_event_status(event_id, new_status, reason=reason)
        if updated is None:
            raise DecisionError({"error": "not_found"}, 404)

    try:
        await record_ask(
            session_id="workspace",
            call_id=event_id,
            tool_name="workspace_decision",
            input_summary={
                "kind": ev.kind,
                "decision": decision,
                "reason": reason or "",
                # What the decision was ABOUT. Without these the ledger holds
                # one row per settled card carrying the kind and the verdict
                # and nothing that names the thing, so "what changed this
                # playbook, and did the change help" cannot be asked of it.
                # `_subject_of` returns {} for a kind that has no subject, so
                # the row is unchanged for every other card.
                **_subject_of(ev, applied_version),
            },
            posture_source="workspace_decision",
            result=(
                "allow_once" if decision == "approve"
                else "resolved" if decision == "resolve"
                else "deleted" if decision == "delete"
                else "deny"
            ),
            # Who actually decided. It was hardcoded to the operator, which
            # was true while a person was the only thing that could reach
            # here, and is a lie the moment the mode settles a card itself.
            # The approval ledger is the record of who allowed what.
            actor=actor,
        )
    except Exception:
        log.exception("workspace: approval ledger record failed")

    return updated, store.list_comments(event_id)


async def post_decision(request: web.Request) -> web.Response:
    """The HTTP shape around `apply_decision`, and nothing else."""
    event_id = request.match_info["event_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    raw = body.get("decisions") if isinstance(body, dict) else None
    try:
        updated, comments = await apply_decision(
            request.app,
            event_id,
            (body.get("decision") or "").strip().lower(),
            reason=(body.get("reason") or "").strip() or None,
            per_file=raw if isinstance(raw, dict) else None,
        )
    except DecisionError as exc:
        return web.json_response(exc.payload, status=exc.status)
    # A card answered is the operator being here, and for a day spent reading
    # the inbox rather than talking it is the only trace of it. On the route
    # rather than inside `apply_decision`, because the other caller is a tool
    # the assistant runs, and under `free` that tool answers unattended.
    last_seen.record()
    return web.json_response(_event_dict(updated, comments))


async def post_comment(request: web.Request) -> web.Response:
    app = request.app
    store = _store(app)
    event_id = request.match_info["event_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    text = (body.get("body") or "").strip()
    if not text:
        return web.json_response({"error": "body required"}, status=400)
    if store.get_event(event_id) is None:
        return web.json_response({"error": "not_found"}, status=404)

    comment = WorkspaceComment.new(
        event_id=event_id,
        author="operator",
        body=text,
        reply_to=body.get("reply_to") or None,
    )
    store.append_comment(comment)
    # Talking in a thread is being here as much as answering a card is. A day
    # spent reading the inbox and replying, without settling anything, would
    # otherwise leave the operator marked absent for all of it.
    last_seen.record()

    # Live-push the operator comment to all attached Mirror sessions so the
    # CommentThread renders without a manual refresh. Best-effort — the
    # comment is durable on disk regardless.
    try:
        from tesseract.workspace_events.broadcast import broadcast_comment_appended
        await broadcast_comment_appended(app, comment)
    except Exception:
        log.exception("workspace: broadcast_comment_appended failed")

    # Dispatch a controller session to write the assistant reply directly into
    # the workspace thread (durable — controller calls workspace_reply tool
    # before this returns). Backend reads + broadcasts the controller-written
    # comment; never writes the reply itself. spawn_if_missing=False so no
    # daemon is cold-forked from a web request.
    try:
        from tesseract.mirror.server.ws import _spawn_tracked
        from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
            dispatch_workspace_reply,
            load_workspace_reply_config,
        )
        ev = store.get_event(event_id)
        cfg = load_workspace_reply_config()
        if cfg.enabled and ev is not None:
            _spawn_tracked(
                app,
                dispatch_workspace_reply(
                    app,
                    event_id=event_id,
                    comment_id=comment.comment_id,
                    event=ev,
                    kind="comment",
                    comment_text=text,
                    config=cfg,
                ),
                name=f"workspace-reply:{event_id}",
            )
    except Exception:
        log.exception("workspace: failed to spawn workspace reply")

    return web.json_response(comment.to_dict(), status=201)


_OPERATOR_POST_SOURCES = {"button", "scratchpad", "voice", "hotkey", "telegram"}
_OPERATOR_POST_TITLE_MAX = 200
_OPERATOR_POST_BODY_MAX = 4000


async def post_operator_post(request: web.Request) -> web.Response:
    """Workstream D — operator-initiated workspace thread.

    Body: ``{title, body, source}``. Optional query ``?await_reply=false``
    suppresses the synthetic turn (default fires it so the operator gets
    an assistant reply within seconds without manually leaving a comment).
    """
    app = request.app
    store = _store(app)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    title = (body.get("title") or "").strip()
    text = (body.get("body") or "").strip()
    source = (body.get("source") or "").strip()
    if not text:
        return web.json_response({"error": "body required"}, status=400)
    if source not in _OPERATOR_POST_SOURCES:
        return web.json_response(
            {"error": f"source must be one of {sorted(_OPERATOR_POST_SOURCES)}"},
            status=400,
        )
    if len(title) > _OPERATOR_POST_TITLE_MAX:
        title = title[:_OPERATOR_POST_TITLE_MAX]
    if len(text) > _OPERATOR_POST_BODY_MAX:
        text = text[:_OPERATOR_POST_BODY_MAX]
    if not title:
        # Derive a one-line title from the body so the inbox row is
        # legible. Strip newlines so the row doesn't grow vertically.
        first_line = text.splitlines()[0] if text else ""
        title = (first_line[:80] or "Operator note").strip()

    event = WorkspaceEvent.new(
        kind="operator_post",
        source="operator",
        title=title,
        summary=text[:400],
        payload={"body": text, "source": source},
    )
    store.append_event(event)
    last_seen.record()

    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event
        await broadcast_workspace_event(app, event)
    except Exception:
        log.exception("workspace: broadcast_workspace_event (operator_post) failed")

    await_reply = request.query.get("await_reply", "true").lower() != "false"
    if await_reply:
        try:
            from tesseract.mirror.server.ws import _spawn_tracked
            from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
                dispatch_workspace_reply,
                load_workspace_reply_config,
            )
            cfg = load_workspace_reply_config()
            if cfg.enabled:
                _spawn_tracked(
                    app,
                    dispatch_workspace_reply(
                        app,
                        event_id=event.event_id,
                        comment_id=event.event_id,
                        event=event,
                        kind="post",
                        comment_text=text,
                        config=cfg,
                    ),
                    name=f"workspace-reply:{event.event_id}",
                )
        except Exception:
            log.exception("workspace: failed to spawn workspace post reply")

    return web.json_response(event.to_dict(), status=201)


async def get_seen(request: web.Request) -> web.Response:
    app = request.app
    store = _store(app)
    return web.json_response(store.get_seen())


async def post_seen(request: web.Request) -> web.Response:
    app = request.app
    store = _store(app)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    panel = (body.get("panel") or "").strip()
    if panel not in {"inbox", "stream"}:
        return web.json_response(
            {"error": "panel must be 'inbox' or 'stream'"}, status=400,
        )
    ts = (body.get("last_seen_at") or "").strip()
    if not ts:
        return web.json_response({"error": "last_seen_at required"}, status=400)
    store.set_seen(panel, ts)
    return web.json_response({"ok": True, "panel": panel, "last_seen_at": ts})
