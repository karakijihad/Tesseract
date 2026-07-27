"""Workspace REST routes — Phase 1 (Inbox + comment threads).

Endpoints:

- ``GET  /api/workspace/inbox``                — pending events (+ filter)
- ``GET  /api/workspace/event/{event_id}``     — single event + thread
- ``POST /api/workspace/event/{event_id}/decision``  — approve / reject
- ``POST /api/workspace/event/{event_id}/comment``   — append operator comment
- ``GET  /api/workspace/seen``                 — last-seen markers
- ``POST /api/workspace/seen``                 — update last-seen marker

Phase 2 (deferred) adds ``/api/workspace/stream`` reading the same
events.jsonl with a ``kind=stream`` filter.

Approve dispatcher
==================

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

Approve on a `feedback_proposal` event (consolidator's merge / archive
proposals) invokes the `memory_promote` tool with the payload's
action — operator decision is the gate, the memory store mutation
runs synchronously, status flips to ``applied``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.kernel.workspace_changes import (
    ConcurrentModificationError,
    ProposeError,
    apply_change,
    compute_diff,
    hash_text,
    resolve_proposable_path,
)
from tesseract.paths import ROOT, workspace_dir
from tesseract.permissions.approval_log import record_ask
from tesseract.integrations._channel_gate import (
    record_approval,
    record_approval_by_hash,
)
from tesseract.workspace_events import (
    EventStore,
    WorkspaceComment,
    WorkspaceEvent,
)

log = logging.getLogger(__name__)


# `resolve` is the soft-close verb for informational events — threads
# that record something that already happened (TARS post, dream-cycle
# nudge, operator-initiated thread) or where the system has nothing
# left to gate (session reflection: writes already committed during
# the reflect turn).
#
# Gated kinds — `change_proposal`, `feedback_proposal`, `feedback_sweep`,
# `agent_approval`, `soul_proposal`, `mission_reflection_proposal` —
# MUST go through explicit approve/reject so the operator's decision
# is recorded; allowing `resolve` on those would erase the gate.
_RESOLVABLE_KINDS = {
    "operator_post",
    "tars_post",
    "nudge",
    "reflection_proposal",  # session reflection — informational
    "daily_brief",          # MO-9-14 — newsletter card; reactions feed the interests profile, Resolve dismisses the row
    "clarification",        # AU-19 — operator answers in the comment thread; resolve marks the question handled
    "recovery_summary",     # AU-2 — boot reconciliation report; nothing to gate, Resolve dismisses
    "strategist_summary",   # AU-23 — weekly initiative curator one-shot; informational
    "runtime_lock_deny",    # SU-1/SU-5 — audit surface for lock-deny attempts; informational
}

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



def _store(request: web.Request) -> EventStore:
    store = request.app.get("workspace_event_store")
    if store is None:
        raise web.HTTPInternalServerError(reason="workspace_event_store not initialised")
    return store


async def _broadcast_envelope(app: web.Application, type_: str, data: dict[str, Any]) -> None:
    """Fan a session envelope to every connected Mirror WS. Best-effort."""
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    try:
        from tesseract.mirror.server.envelope import make_envelope
        from tesseract.mirror.server.session import send_envelope
    except Exception:
        log.exception("workspace: mirror envelope/session import failed")
        return
    for sess in list(sessions.values()):
        env = make_envelope(type_, "session", getattr(sess, "session_id", ""), data)
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception("workspace: send_envelope failed")


async def _commit_change_proposal(
    request: web.Request,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any], int] | None:
    """Perform the file commit for a `change_proposal` event. Returns
    `(error_payload, status_code)` on failure (caller returns it as the
    JSON response); returns None on success."""
    payload = ev.payload or {}
    target_path = str(payload.get("target_path") or "")
    action = str(payload.get("action") or "")
    content = str(payload.get("content") or "")
    section = payload.get("section")
    if section is not None:
        section = str(section)
    expected_hash_before = payload.get("expected_hash_before")
    if expected_hash_before is not None:
        expected_hash_before = str(expected_hash_before)

    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=target_path,
            action=action,  # type: ignore[arg-type]
            content=content,
            section=section,
            expected_hash_before=expected_hash_before,
        )
    except ConcurrentModificationError as exc:
        # File changed under us. Recompute the diff against current bytes
        # so the operator can re-review with fresh context. The event
        # stays pending — Approve again to commit against the new hash.
        try:
            current = resolve_proposable_path(target_path).read_text(encoding="utf-8")
            from tesseract.kernel.workspace_changes import preview_change
            new_after = preview_change(
                current_text=current,
                action=action,  # type: ignore[arg-type]
                content=content,
                section=section,
            )
            fresh_diff = compute_diff(
                current, new_after, target_label=str(payload.get("label") or "file"),
            )
            new_hash = hash_text(current)
        except Exception:  # noqa: BLE001 — diagnostics only
            fresh_diff = ""
            new_hash = ""
        return {
            "error": "concurrent_modification",
            "detail": str(exc),
            "expected_hash_before": exc.expected,
            "actual_hash": exc.actual,
            "fresh_diff": fresh_diff,
            "fresh_expected_hash_before": new_hash,
        }, 409
    except ProposeError as exc:
        return {"error": "invalid_proposal", "detail": str(exc)}, 400
    except OSError as exc:
        return {"error": "commit_failed", "detail": str(exc)}, 500

    await _broadcast_envelope(
        request.app,
        "soul_updated" if target_path == "tesseract/workspace/SOUL.md" else "workspace_file_updated",
        {
            "path": target_path,
            "label": str(payload.get("label") or ""),
            "content": resolve_proposable_path(target_path).read_text(encoding="utf-8"),
            "source": "workspace_decision",
            "hash_after": applied.hash_after,
            # `no_op_reason` is set when apply_change short-circuited
            # because the proposed content was already present (idempotent
            # commit). Frontend uses it to render a "duplicate, no-op"
            # toast instead of a silent success.
            "no_op_reason": applied.no_op_reason,
        },
    )
    return None


async def _commit_yaml_change_proposal(
    request: web.Request,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``yaml_change_proposal`` event via :func:`apply_yaml_change`.

    Returns ``(result, None)`` on success, ``(None, (error_payload, status))``
    on failure. On success the caller writes the event status as
    ``applied`` and triggers the roles SUMMARY regen for catalog edits.
    """
    from tesseract.kernel.workspace_changes import apply_yaml_change

    payload = ev.payload or {}
    target_path = str(payload.get("target_path") or "")
    action = str(payload.get("action") or "")
    yaml_path = str(payload.get("yaml_path") or "")
    content = payload.get("content")
    expected_hash_before = str(payload.get("expected_hash_before") or "")
    if not target_path or not action or not yaml_path or not expected_hash_before:
        return None, ({"error": "invalid_proposal", "detail": "missing required fields"}, 400)

    try:
        result = await asyncio.to_thread(
            apply_yaml_change,
            repo_root=ROOT,
            target_path=target_path,
            action=action,  # type: ignore[arg-type]
            yaml_path=yaml_path,
            content=content,
            expected_hash_before=expected_hash_before,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("yaml_change_proposal commit crashed")
        return None, ({"error": "commit_failed", "detail": str(exc)}, 500)

    if not result.ok:
        # Pre-write check failure — return 409 so the inbox renders the
        # reason inline (drift / schema / parse). Event stays pending so
        # the operator can re-emit / approve again after fixing it.
        return None, ({
            "error": "apply_refused",
            "reason": result.reason,
            "target_path": result.target_path,
        }, 409)

    meta = {
        "target_path": result.target_path,
        "bytes_before": result.bytes_before,
        "bytes_after": result.bytes_after,
        "hash_before": result.hash_before,
        "hash_after": result.hash_after,
        "no_op_reason": result.no_op_reason,
    }

    if result.target_path in {"tesseract/config/roles.yaml", "tesseract/config/providers.yaml"}:
        try:
            from tesseract.scripts.regenerate_roles_summary import regenerate

            await asyncio.to_thread(regenerate)
        except Exception:  # noqa: BLE001
            log.exception("yaml_change_proposal: SUMMARY regen failed (non-fatal)")

    return meta, None


def _agents_dir() -> Path:
    """Agents tree resolved at call time — delegates to the canonical
    `tesseract.paths.agents_dir()` helper (distributable-app Task 1/3)."""
    from tesseract.paths import agents_dir

    return agents_dir()


def _archive_rejected_agent(agents_dir: Path, name: str, reason: str | None) -> str | None:
    """Move ``pending/{name}.md`` to ``rejected/`` + write the reason sidecar
    agent_create's re-proposal dedup reads. Returns an error string or None."""
    src = agents_dir / "pending" / f"{name}.md"
    if not src.exists():
        return f"no pending agent {name!r} to reject"
    rejected_dir = agents_dir / "rejected"
    try:
        rejected_dir.mkdir(parents=True, exist_ok=True)
        os.replace(str(src), str(rejected_dir / f"{name}.md"))
        if reason:
            (rejected_dir / f"{name}.reason.txt").write_text(reason, encoding="utf-8")
    except OSError as exc:
        return f"reject archive failed: {exc}"
    return None


async def _commit_agent_approval(
    request: web.Request,
    ev: WorkspaceEvent,
    decision: str,
    reason: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Stage 10 — settle an `agent_approval` proposal card.

    Approve runs the promotion core shared with the `agent_promote` chat
    tool (validate → pending→active → INDEX row, rollback on INDEX
    failure). Reject archives the file to `agents/rejected/` with the
    operator's reason sidecar, then leaves the reason as an operator
    comment on the thread — the undelivered-comment rail carries it to
    TARS on its next turn, and the reply dispatch (best-effort, same as
    `post_comment`) prompts an acknowledgment.
    """
    name = str((ev.payload or {}).get("name") or "")
    if not name:
        return None, ({"error": "agent_approval_missing_name"}, 400)

    agents_dir = _agents_dir()

    if decision == "approve":
        from tesseract.kernel.tools.agent_promote import promote_pending_agent

        loaded, err = await asyncio.to_thread(promote_pending_agent, agents_dir, name)
        if err is not None:
            return None, ({"error": "promote_failed", "detail": err}, 409)
        return {"promoted": name, "model_role": loaded.model_role}, None

    err = await asyncio.to_thread(_archive_rejected_agent, agents_dir, name, reason)
    if err is not None:
        return None, ({"error": "reject_failed", "detail": err}, 409)

    store = _store(request)
    comment_body = f"Rejected: {reason}" if reason else "Rejected (no reason given)."
    comment = WorkspaceComment.new(
        event_id=ev.event_id, author="operator", body=comment_body,
    )
    try:
        store.append_comment(comment)
    except OSError:
        # The archive already happened; a lost comment only costs the
        # next-turn notification, not the decision itself.
        log.exception("agent_approval reject: comment append failed")
        return {"rejected": name}, None

    try:
        from tesseract.mirror.server.ws import _spawn_tracked
        from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
            dispatch_workspace_reply,
            load_workspace_reply_config,
        )

        cfg = load_workspace_reply_config()
        if cfg.enabled:
            _spawn_tracked(
                request.app,
                dispatch_workspace_reply(
                    request.app,
                    event_id=ev.event_id,
                    comment_id=comment.comment_id,
                    event=ev,
                    kind="comment",
                    comment_text=comment_body,
                    config=cfg,
                ),
                name=f"workspace-reply:{ev.event_id}",
            )
    except Exception:
        log.exception("agent_approval reject: reply dispatch failed")

    return {"rejected": name}, None


def _skills_dir() -> Path:
    """Skills tree resolved at call time (Phase 4) via `workspace_dir()`."""
    return workspace_dir() / "skills"


async def _commit_skill_approval(
    request: web.Request,
    ev: WorkspaceEvent,
    decision: str,
    reason: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Phase 4 — settle a `skill_approval` proposal card (mirror of
    `_commit_agent_approval`). Approve runs the promotion core shared with the
    `skill_promote` chat tool (validate → pending→active dir move). Reject
    archives the draft to `skills/rejected/` with the operator's reason
    sidecar, then leaves the reason as an operator comment carried to TARS."""
    name = str((ev.payload or {}).get("name") or "")
    if not name:
        return None, ({"error": "skill_approval_missing_name"}, 400)

    skills_dir = _skills_dir()

    if decision == "approve":
        from tesseract.kernel.tools.skill_promote import promote_pending_skill

        entry, err = await asyncio.to_thread(promote_pending_skill, skills_dir, name)
        if err is not None:
            return None, ({"error": "promote_failed", "detail": err}, 409)
        return {"promoted": name}, None

    from tesseract.kernel.tools.skill_promote import archive_rejected_skill

    err = await asyncio.to_thread(archive_rejected_skill, skills_dir, name, reason)
    if err is not None:
        return None, ({"error": "reject_failed", "detail": err}, 409)

    store = _store(request)
    comment_body = f"Rejected: {reason}" if reason else "Rejected (no reason given)."
    comment = WorkspaceComment.new(event_id=ev.event_id, author="operator", body=comment_body)
    try:
        store.append_comment(comment)
    except OSError:
        log.exception("skill_approval reject: comment append failed")
        return {"rejected": name}, None

    _spawn_reject_reply(request, ev, comment, comment_body)
    return {"rejected": name}, None


async def _commit_skill_refinement(
    request: web.Request,
    ev: WorkspaceEvent,
    decision: str,
    reason: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Phase 4 4b — settle a `skill_refinement` card. Approve applies the
    proposed SKILL.md body to the LIVE active skill (atomic, validated).
    Reject leaves the skill untouched and records the operator's reason as a
    comment. A refinement carries `{name, proposed_markdown}` in payload."""
    name = str((ev.payload or {}).get("name") or "")
    proposed = str((ev.payload or {}).get("proposed_markdown") or "")
    if not name:
        return None, ({"error": "skill_refinement_missing_name"}, 400)

    if decision == "approve":
        if not proposed.strip():
            return None, ({"error": "skill_refinement_no_proposal"}, 409)
        skills_dir = _skills_dir()
        err = await asyncio.to_thread(_apply_skill_refinement, skills_dir, name, proposed)
        if err is not None:
            return None, ({"error": "refine_failed", "detail": err}, 409)
        return {"refined": name}, None

    # Reject — skill untouched; record the reason for TARS.
    store = _store(request)
    comment_body = f"Refinement rejected: {reason}" if reason else "Refinement rejected (no reason given)."
    comment = WorkspaceComment.new(event_id=ev.event_id, author="operator", body=comment_body)
    try:
        store.append_comment(comment)
    except OSError:
        log.exception("skill_refinement reject: comment append failed")
        return {"rejected": name}, None

    _spawn_reject_reply(request, ev, comment, comment_body)
    return {"rejected": name}, None


def _apply_skill_refinement(skills_dir: Path, name: str, proposed_markdown: str) -> str | None:
    """Validate + atomically overwrite the live `skills/<name>/SKILL.md`.
    Returns an error string or None. Refuses to write if the proposed body
    fails the loader round-trip (frontmatter/name/size)."""
    import tempfile

    from tesseract.brain.skills import SKILL_FILENAME, load_skill_folder

    target = skills_dir / name / SKILL_FILENAME
    if not target.exists():
        return f"no active skill {name!r} to refine at {target}"

    # Round-trip the proposal in a temp folder before touching the live file.
    tmp_root = Path(tempfile.mkdtemp())
    tmp_folder = tmp_root / name
    tmp_folder.mkdir(parents=True, exist_ok=True)
    try:
        (tmp_folder / SKILL_FILENAME).write_text(proposed_markdown, encoding="utf-8")
        entry = load_skill_folder(tmp_folder)
        if entry is None or entry.name != name:
            return "proposed SKILL.md failed loader validation (frontmatter/name/size)"
    finally:
        try:
            (tmp_folder / SKILL_FILENAME).unlink(missing_ok=True)
            tmp_folder.rmdir()
            tmp_root.rmdir()
        except OSError:
            pass

    tmp = target.with_suffix(".md.tmp")
    try:
        tmp.write_text(proposed_markdown, encoding="utf-8")
        os.replace(str(tmp), str(target))
    except OSError as exc:
        return f"skill refinement write failed: {exc}"
    return None


def _spawn_reject_reply(
    request: web.Request,
    ev: WorkspaceEvent,
    comment: WorkspaceComment,
    comment_body: str,
) -> None:
    """Best-effort next-turn TARS reply after a reject/refinement decision
    (shared by the skill card handlers; mirrors `_commit_agent_approval`)."""
    try:
        from tesseract.mirror.server.ws import _spawn_tracked
        from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
            dispatch_workspace_reply,
            load_workspace_reply_config,
        )

        cfg = load_workspace_reply_config()
        if cfg.enabled:
            _spawn_tracked(
                request.app,
                dispatch_workspace_reply(
                    request.app,
                    event_id=ev.event_id,
                    comment_id=comment.comment_id,
                    event=ev,
                    kind="comment",
                    comment_text=comment_body,
                    config=cfg,
                ),
                name=f"workspace-reply:{ev.event_id}",
            )
    except Exception:
        log.exception("skill card reject: reply dispatch failed")


_SOUL_REL = "tesseract/workspace/SOUL.md"


async def _commit_soul_proposal(
    request: web.Request,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``soul_proposal`` event by appending the bullet to SOUL.md.

    Operator-attended single approval: the consolidator card already
    rendered the bullet text, so this commits straight to SOUL.md instead
    of round-tripping through ``soul_growth_propose`` (which would queue
    a second ``change_proposal`` card for the same decision). On success
    the caller flips the event status to ``applied`` and we broadcast
    ``soul_updated`` so any attached Mirror session refreshes the Soul
    tab without a manual reload.
    """
    payload = ev.payload or {}
    bullet = str(payload.get("bullet") or "").strip().lstrip("-*").strip()
    if not bullet:
        return None, ({"error": "invalid_proposal", "detail": "missing bullet"}, 400)

    bullet_line = f"- {bullet}\n"
    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=_SOUL_REL,
            action="append_to_section",
            content=bullet_line,
            section="Growth",
        )
    except ProposeError as exc:
        return None, ({"error": "invalid_proposal", "detail": str(exc)}, 400)
    except OSError as exc:
        log.exception("soul_proposal commit failed")
        return None, ({"error": "commit_failed", "detail": str(exc)}, 500)

    try:
        content_after = resolve_proposable_path(_SOUL_REL).read_text(encoding="utf-8")
    except OSError:
        content_after = ""
    await _broadcast_envelope(
        request.app,
        "soul_updated",
        {
            "path": _SOUL_REL,
            "label": "Soul",
            "content": content_after,
            "source": "workspace_decision",
            "hash_after": applied.hash_after,
            "no_op_reason": applied.no_op_reason,
        },
    )
    return {
        "target_path": _SOUL_REL,
        "hash_after": applied.hash_after,
        "no_op_reason": applied.no_op_reason,
    }, None


async def _commit_feedback_proposal(
    request: web.Request,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``feedback_proposal`` (merge_into / archive) via ``memory_promote``.

    Resolves the live tool off the Mirror's tool registry so the call
    reuses the bound ``MemoryStore`` + ``MemoryIndex``. If the registry
    isn't wired (early-boot, CLI smoke test), we surface 503 rather than
    silently marking the event applied — the operator's decision is
    durable as ``pending`` and they can retry once the registry is up.
    """
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.memory_promote import MemoryPromoteInput

    payload = ev.payload or {}
    action = str(payload.get("action") or "").strip()
    if action not in {"merge_into", "archive"}:
        return None, (
            {"error": "unsupported_action", "detail": f"action {action!r} not handled"},
            400,
        )

    registry = request.app.get("tool_registry") if hasattr(request.app, "get") else None
    tool = registry.get("memory_promote") if registry is not None and hasattr(registry, "get") else None
    if tool is None:
        return None, (
            {
                "error": "tool_unavailable",
                "detail": "memory_promote tool not registered; retry once Mirror finishes booting",
            },
            503,
        )

    ctx = ToolContext(session_id="workspace", current_call_id=ev.event_id)

    if action == "merge_into":
        keep = str(payload.get("keep") or "").strip()
        absorb_raw = payload.get("absorb") or []
        absorb = [str(x).strip() for x in absorb_raw if str(x).strip()] if isinstance(absorb_raw, list) else []
        if not keep or not absorb:
            return None, (
                {"error": "invalid_proposal", "detail": "merge_into requires keep + absorb"},
                400,
            )

        # All-or-nothing: if any source fails the event stays pending so
        # the operator can investigate without the inbox card disappearing
        # to History. ``memory_promote._merge`` is idempotent on already-
        # archived sources (see its docstring), so retrying after a
        # partial failure does not double-append bodies for the ones that
        # already succeeded.
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for src in absorb:
            res = await tool.run(
                MemoryPromoteInput(memory_id=src, action="merge_into", target=keep),
                ctx,
            )
            entry = {"source": src, "ok": not res.is_error, "output": res.output}
            results.append(entry)
            if res.is_error:
                failures.append(entry)
        if failures:
            return None, (
                {"error": "merge_failed", "failures": failures, "results": results},
                500,
            )
        return {
            "action": "merge_into",
            "target": keep,
            "merged": len(results),
            "results": results,
        }, None

    # action == "archive"
    memory_id = str(payload.get("memory_id") or "").strip()
    if not memory_id:
        return None, ({"error": "invalid_proposal", "detail": "archive requires memory_id"}, 400)
    res = await tool.run(
        MemoryPromoteInput(memory_id=memory_id, action="archive"),
        ctx,
    )
    if res.is_error:
        return None, ({"error": "archive_failed", "detail": res.output}, 500)
    return {"action": "archive", "memory_id": memory_id, "output": res.output}, None


async def _commit_vault_raw_ingest_batch(
    request: web.Request,
    ev: WorkspaceEvent,
    *,
    deny_all: bool,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply (or deny) a ``vault_raw_ingest_batch`` event.

    Approve → run ``vault_raw_watch.apply_ask_batch`` with every file
    marked approved (operator may pass `decisions: {relpath: "denied"}`
    in the body to override per-file). Reject → log every file denied.

    The handler reuses the live VaultManager + VaultIndexer attached to
    the Mirror's tool registry; falls back to constructing fresh ones
    when the registry is not yet wired (CLI / test harness paths).
    """
    from tesseract.paths import TESSERACT_HOME
    from tesseract.scheduler.tasks.vault_raw_watch import apply_ask_batch

    payload = ev.payload or {}
    files = payload.get("files") or []
    if not isinstance(files, list):
        return None, ({"error": "invalid_batch", "detail": "files must be a list"}, 400)

    body: dict[str, Any] = {}
    if request is not None:
        try:
            body = await request.json()
        except Exception:
            body = {}
    decisions_in = body.get("decisions") if isinstance(body, dict) else None
    decisions: dict[str, str] = {
        relpath: verdict.strip().lower()
        for relpath, verdict in (decisions_in.items() if isinstance(decisions_in, dict) else [])
        if isinstance(relpath, str) and isinstance(verdict, str)
        and verdict.strip().lower() in {"approved", "denied"}
    }
    if deny_all:
        for entry in files:
            relpath = entry.get("relpath") if isinstance(entry, dict) else None
            if isinstance(relpath, str):
                decisions[relpath] = "denied"

    app = request.app if request is not None else None
    vault_manager, indexer = _resolve_vault_dependencies(app)
    home_override = os.environ.get("TESSERACT_HOME") if request is not None else None
    home = Path(home_override).resolve() if home_override else TESSERACT_HOME
    cursor_path = home / "autonomy" / "vault-raw-cursors.jsonl"

    try:
        summary = await apply_ask_batch(
            files=files,
            decisions=decisions,
            vault_manager=vault_manager,
            indexer=indexer,
            cursor_path=cursor_path,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("vault_raw_ingest_batch apply crashed")
        return None, ({"error": "apply_crashed", "detail": str(exc)}, 500)

    return summary, None


def _resolve_vault_dependencies(app: Any) -> tuple[Any, Any]:
    """Pull live VaultManager + VaultIndexer off the Mirror tool registry
    so the approve handler reuses the same indexer the runtime configured
    (FAISS + FTS handles). Falls back to fresh instances when the registry
    is not wired (CLI / test harness)."""
    from tesseract.memory.vault_indexer import VaultIndexer
    from tesseract.memory.vault_manager import VaultManager
    from tesseract.paths import TESSERACT_HOME

    if app is not None and hasattr(app, "get"):
        registry = app.get("tool_registry")
        if registry is not None:
            tool = getattr(registry, "get", lambda _name: None)("vault_ingest")
            if tool is not None:
                vm = getattr(tool, "_manager", None)
                idx = getattr(tool, "_indexer", None)
                if isinstance(vm, VaultManager):
                    return vm, idx if isinstance(idx, VaultIndexer) else None
    home_override = os.environ.get("TESSERACT_HOME")
    home = Path(home_override).resolve() if home_override else TESSERACT_HOME
    return VaultManager(vault_root=home / "vault"), None


def _event_dict(ev: WorkspaceEvent, comments: list[WorkspaceComment]) -> dict[str, Any]:
    return {
        **ev.to_dict(),
        "comments": [c.to_dict() for c in comments],
    }


async def list_inbox(request: web.Request) -> web.Response:
    store = _store(request)
    status_filter = request.query.get("status", "pending")
    status: Any = status_filter if status_filter != "all" else None
    events = store.list_events(status=status, limit=200)
    payload = []
    for ev in events:
        comments = store.list_comments(ev.event_id)
        payload.append(_event_dict(ev, comments))
    return web.json_response({"events": payload, "count": len(payload)})


async def get_event(request: web.Request) -> web.Response:
    store = _store(request)
    event_id = request.match_info["event_id"]
    ev = store.get_event(event_id)
    if ev is None:
        return web.json_response({"error": "not_found"}, status=404)
    comments = store.list_comments(event_id)
    return web.json_response(_event_dict(ev, comments))


async def post_decision(request: web.Request) -> web.Response:
    store = _store(request)
    event_id = request.match_info["event_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    decision = (body.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject", "resolve", "delete"}:
        return web.json_response(
            {"error": "decision must be 'approve', 'reject', 'resolve', or 'delete'"},
            status=400,
        )
    reason = (body.get("reason") or "").strip() or None

    ev = store.get_event(event_id)
    if ev is None:
        return web.json_response({"error": "not_found"}, status=404)
    # `resolve` is restricted to informational kinds. A pending
    # `change_proposal` / `mission_reflection_proposal` / feedback_* /
    # agent_approval MUST go through approve/reject so the decision is
    # recorded; a blanket `resolve` on those would silently bypass the
    # gate. Session `reflection_proposal` is informational (writes
    # already committed during the reflect turn) and is in the
    # resolvable set.
    if decision == "resolve" and ev.kind not in _RESOLVABLE_KINDS:
        return web.json_response(
            {
                "error": "resolve_not_permitted_for_kind",
                "detail": (
                    f"'resolve' is only valid for {sorted(_RESOLVABLE_KINDS)}; "
                    f"event kind '{ev.kind}' requires explicit approve or reject."
                ),
                "kind": ev.kind,
            },
            status=400,
        )
    async with _decision_lock(event_id):
        # Re-read the event under the lock. A concurrent decision on the same
        # event may have settled it while we waited for the lock; acting on the
        # stale pre-lock copy is exactly the race this guards against.
        ev = store.get_event(event_id)
        if ev is None:
            return web.json_response({"error": "not_found"}, status=404)
        # `delete` is the universal escape hatch — works on any status so the
        # operator can soft-delete from history too. Other verbs stay
        # pending-only and idempotently return the settled event.
        if decision != "delete" and ev.status not in {"pending"}:
            comments = store.list_comments(event_id)
            return web.json_response(_event_dict(ev, comments))

        if decision == "approve" and ev.kind == "change_proposal":
            err = await _commit_change_proposal(request, ev)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)

        yaml_apply_meta: dict[str, Any] | None = None
        if decision == "approve" and ev.kind == "yaml_change_proposal":
            result, err = await _commit_yaml_change_proposal(request, ev)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)
            yaml_apply_meta = result

        soul_apply_meta: dict[str, Any] | None = None
        if decision == "approve" and ev.kind == "soul_proposal":
            result, err = await _commit_soul_proposal(request, ev)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)
            soul_apply_meta = result

        feedback_apply_meta: dict[str, Any] | None = None
        if decision == "approve" and ev.kind == "feedback_proposal":
            result, err = await _commit_feedback_proposal(request, ev)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)
            feedback_apply_meta = result

        if decision in {"approve", "reject"} and ev.kind == "agent_approval":
            _agent_meta, err = await _commit_agent_approval(request, ev, decision, reason)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)

        if decision in {"approve", "reject"} and ev.kind == "skill_approval":
            _skill_meta, err = await _commit_skill_approval(request, ev, decision, reason)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)

        if decision in {"approve", "reject"} and ev.kind == "skill_refinement":
            _refine_meta, err = await _commit_skill_refinement(request, ev, decision, reason)
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)

        raw_batch_meta: dict[str, Any] | None = None
        if decision in {"approve", "reject"} and ev.kind == "vault_raw_ingest_batch":
            result, err = await _commit_vault_raw_ingest_batch(request, ev, deny_all=(decision == "reject"))
            if err is not None:
                payload, status = err
                return web.json_response(payload, status=status)
            raw_batch_meta = result

        # Codex audit 2026-05-06 m2: `resolve` flips status to `resolved`
        # without firing approval side-effects — the verb operator_post
        # threads need to leave the inbox once the conversation has played
        # out (approve/reject would mis-record an open thread as a gated
        # decision). `delete` is the soft-delete verb — row leaves the
        # active inbox and surfaces in History with the deleted pill.
        # yaml_change_proposal uses `applied` to distinguish "operator approved AND
        # the YAML file was successfully mutated" from the generic approval state.
        # vault_raw_ingest_batch (AU-22) follows the same `applied` convention on
        # approve so the inbox UI can render "ingested" vs "approved without action".
        # agent_approval joins the `applied` convention (Stage 10): approve
        # means the promotion side-effect ran, not just that the operator
        # nodded.
        if decision == "approve" and ev.kind in {
            "yaml_change_proposal",
            "vault_raw_ingest_batch",
            "soul_proposal",
            "feedback_proposal",
            "agent_approval",
            "skill_approval",
            "skill_refinement",
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
            return web.json_response({"error": "not_found"}, status=404)

    try:
        await record_ask(
            session_id="workspace",
            call_id=event_id,
            tool_name="workspace_decision",
            input_summary={
                "kind": ev.kind,
                "decision": decision,
                "reason": reason or "",
            },
            posture_source="workspace_decision",
            result=(
                "allow_once" if decision == "approve"
                else "resolved" if decision == "resolve"
                else "deleted" if decision == "delete"
                else "deny"
            ),
            actor="operator",
        )
    except Exception:
        log.exception("workspace: approval ledger record failed")

    comments = store.list_comments(event_id)
    return web.json_response(_event_dict(updated, comments))


async def post_comment(request: web.Request) -> web.Response:
    store = _store(request)
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

    # Live-push the operator comment to all attached Mirror sessions so the
    # CommentThread renders without a manual refresh. Best-effort — the
    # comment is durable on disk regardless.
    try:
        from tesseract.workspace_events.broadcast import broadcast_comment_appended
        await broadcast_comment_appended(request.app, comment)
    except Exception:
        log.exception("workspace: broadcast_comment_appended failed")

    # Dispatch a controller session to write the TARS reply directly into
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
                request.app,
                dispatch_workspace_reply(
                    request.app,
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
    a TARS reply within seconds without manually leaving a comment).
    """
    store = _store(request)
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

    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event
        await broadcast_workspace_event(request.app, event)
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
                    request.app,
                    dispatch_workspace_reply(
                        request.app,
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


_REJECT_TEMPLATE = (
    "I checked with the operator — they prefer I not use {tool} for this. "
    "Anything else I can help with?"
)


async def post_channel_gate_decision(request: web.Request) -> web.Response:
    """CR-5 — operator-side handler for ``tars_post`` events sourced by the
    channel gate.

    Body: ``{action: "approve_next_turn" | "reject_and_message",
    reply?: string}``. The event must be ``kind == "tars_post"`` and its
    payload must carry ``channel`` + ``chat_id`` (otherwise it wasn't
    sourced by ``channel_gate``).

    Approve-next-turn writes a single-shot token onto the channel
    :class:`ServerSession` so the next call that matches
    ``(tool_name, args_hash)`` auto-passes within
    ``approve_next_turn_ttl_s``. Reject sends a templated outbound reply
    via the channel adapter and resolves the event.
    """
    store = _store(request)
    event_id = request.match_info["event_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    action = (body.get("action") or "").strip().lower()
    if action not in {"approve_next_turn", "reject_and_message"}:
        return web.json_response(
            {"error": "action must be 'approve_next_turn' or 'reject_and_message'"},
            status=400,
        )

    ev = store.get_event(event_id)
    if ev is None:
        return web.json_response({"error": "not_found"}, status=404)
    if ev.kind != "tars_post":
        return web.json_response(
            {"error": "channel_gate decisions only valid for tars_post events"},
            status=400,
        )
    payload = ev.payload or {}
    channel = str(payload.get("channel") or "")
    chat_id = str(payload.get("chat_id") or "")
    tool_name = str(payload.get("tool") or "")
    if not channel or not chat_id or not tool_name:
        return web.json_response(
            {"error": "event was not sourced by channel_gate (missing channel/chat_id/tool)"},
            status=400,
        )

    bridge = _find_channel_bridge(request.app, channel)
    if bridge is None:
        return web.json_response(
            {"error": f"no bridge available for channel '{channel}'"},
            status=503,
        )

    if action == "approve_next_turn":
        ok, detail = _approve_next_turn(bridge, chat_id, payload)
        if not ok:
            return web.json_response({"error": detail}, status=400)
        updated = store.update_event_status(
            event_id, "approved", reason="approve_next_turn",
        )
        try:
            await record_ask(
                session_id="workspace",
                call_id=event_id,
                tool_name="workspace_decision",
                input_summary={
                    "kind": ev.kind,
                    "decision": "approve_next_turn",
                    "tool": tool_name,
                    "channel": channel,
                    "chat_id": chat_id,
                },
                posture_source="channel_gate",
                result="allow_once",
                actor="operator",
            )
        except Exception:
            log.exception("workspace: channel-gate approval ledger record failed")
        comments = store.list_comments(event_id)
        return web.json_response(
            _event_dict(updated or ev, comments),
        )

    # reject_and_message
    reply = (body.get("reply") or "").strip()
    if not reply:
        reply = _REJECT_TEMPLATE.format(tool=tool_name)
    try:
        await _send_outbound_via_bridge(bridge, chat_id, reply)
    except Exception as exc:
        log.exception("workspace: channel-gate reject send failed")
        return web.json_response(
            {"error": "send_failed", "detail": str(exc)}, status=502,
        )
    updated = store.update_event_status(event_id, "rejected", reason="reject_and_message")
    try:
        await record_ask(
            session_id="workspace",
            call_id=event_id,
            tool_name="workspace_decision",
            input_summary={
                "kind": ev.kind,
                "decision": "reject_and_message",
                "tool": tool_name,
                "channel": channel,
                "chat_id": chat_id,
            },
            posture_source="channel_gate",
            result="deny",
            actor="operator",
        )
    except Exception:
        log.exception("workspace: channel-gate rejection ledger record failed")
    comments = store.list_comments(event_id)
    return web.json_response(_event_dict(updated or ev, comments))


def _find_channel_bridge(app: web.Application, channel: str) -> Any | None:
    """Return the bridge instance for ``channel`` or ``None``.

    Today only ``telegram`` exists; the lookup is generalized so future
    adapters slot in under their own ``<channel>_bridge`` app key.
    """
    return app.get(f"{channel}_bridge")


def _approve_next_turn(
    bridge: Any, chat_id: str, payload: dict[str, Any],
) -> tuple[bool, str]:
    sessions = getattr(bridge, "_sessions", None)
    if not isinstance(sessions, dict):
        return False, "bridge does not expose channel sessions"
    try:
        chat_key = int(chat_id)
    except (TypeError, ValueError):
        chat_key = chat_id  # future adapters may key on strings
    session = sessions.get(chat_key)
    if session is None:
        return False, f"no live channel session for chat_id={chat_id}"
    tool_name = str(payload.get("tool") or "")
    ttl_s = int(payload.get("approve_next_turn_ttl_s") or 1800)
    if not tool_name:
        return False, "event payload missing tool name"

    # Prefer the fingerprint the gate already stored on the event — it
    # is keyed on the exact validated args the next ASK will hash against,
    # so the approval token cannot drift from the live call's hash even
    # if the args contained an exotic non-dict value at gate time.
    stored_hash = payload.get("args_hash")
    if isinstance(stored_hash, str) and stored_hash:
        record_approval_by_hash(session, args_hash=stored_hash, ttl_s=ttl_s)
    else:
        args = payload.get("args")
        record_approval(
            session,
            tool_name=tool_name,
            args=args if isinstance(args, dict) else {},
            ttl_s=ttl_s,
        )
    return True, ""


async def _send_outbound_via_bridge(
    bridge: Any, chat_id: str, body: str,
) -> None:
    """Dispatch an operator-templated reply through the bridge's outbound
    path. The Telegram bridge owns ``_send_outbound`` (HTML formatting +
    conversation log); other adapters will expose the same surface."""
    send = getattr(bridge, "_send_outbound", None)
    if not callable(send):
        raise RuntimeError(f"bridge {bridge!r} has no _send_outbound")
    try:
        chat_key = int(chat_id)
    except (TypeError, ValueError):
        chat_key = chat_id
    await send(chat_key, body)


async def get_seen(request: web.Request) -> web.Response:
    store = _store(request)
    return web.json_response(store.get_seen())


async def post_seen(request: web.Request) -> web.Response:
    store = _store(request)
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


