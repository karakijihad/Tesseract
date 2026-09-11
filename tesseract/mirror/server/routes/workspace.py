"""Workspace REST routes — Inbox + comment threads.

Endpoints:

- ``GET  /api/workspace/inbox``                — pending events (+ filter)
- ``GET  /api/workspace/event/{event_id}``     — single event + thread
- ``POST /api/workspace/event/{event_id}/decision``  — approve / reject
- ``POST /api/workspace/event/{event_id}/comment``   — append operator comment
- ``GET  /api/workspace/seen``                 — last-seen markers
- ``POST /api/workspace/seen``                 — update last-seen marker

A future ``/api/workspace/stream`` would read the same events.jsonl
with a ``kind=stream`` filter.

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
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.kernel.workspace_changes import (
    PROPOSABLE_PATHS,
    ConcurrentModificationError,
    ProposeError,
    apply_change,
    compute_diff,
    hash_text,
    resolve_proposable_path,
    validate_growth_section,
)
from tesseract.lib import last_seen
from tesseract.paths import ROOT, workspace_dir
from tesseract.permissions.approval_log import record_ask
from tesseract.workspace_events import (
    EventStore,
    WorkspaceComment,
    WorkspaceEvent,
)

log = logging.getLogger(__name__)


# `resolve` is the soft-close verb for informational events — threads
# that record something that already happened (the assistant post, dream-cycle
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
    "agent_post",
    "nudge",
    "reflection_proposal",  # session reflection — informational
    "daily_brief",          # Newsletter card; informational, Resolve dismisses the row
    "clarification",        # Operator answers in the comment thread; resolve marks the question handled
    "recovery_summary",     # Boot reconciliation report; nothing to gate, Resolve dismisses
    "strategist_summary",   # Weekly initiative curator one-shot; informational
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



def _store(app: web.Application) -> EventStore:
    store = app.get("workspace_event_store")
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
    app: web.Application,
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
            fresh_bytes = (
                len(current.encode("utf-8")),
                len(new_after.encode("utf-8")),
            )
        except Exception:  # noqa: BLE001 — diagnostics only
            fresh_diff = ""
            new_hash = ""
            fresh_bytes = None
        # Write the recomputed snapshot back onto the card, which is what makes
        # "Approve again" true. Without it the payload keeps the hash it was
        # born with, so every later Approve loses the same race and the event
        # is pending forever with no way out but Reject.
        #
        # The whole snapshot moves together — hash, diff and the two sizes the
        # card renders — because half of it would describe the file as it was
        # and half as it is.
        #
        # Only when the recompute produced a hash: merging an empty one would
        # leave `expected_hash_before` falsy, and the next Approve would skip
        # the drift check entirely and commit against bytes nobody reviewed.
        if new_hash and fresh_bytes is not None:
            _store(app).merge_event_payload(
                ev.event_id,
                {
                    "expected_hash_before": new_hash,
                    "diff": fresh_diff,
                    "bytes_before": fresh_bytes[0],
                    "bytes_after": fresh_bytes[1],
                },
            )
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
        app,
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
    app: web.Application,
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
    """The operator's agents tree, resolved at call time.

    The user root specifically: this route promotes and rejects the
    assistant's proposals, and both move files. Nothing shipped is ever
    pending, so there is nothing here that belongs in the app tree.
    """
    from tesseract.paths import user_agents_dir

    return user_agents_dir()


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
    app: web.Application,
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
    The assistant on its next turn, and the reply dispatch (best-effort, same as
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

    store = _store(app)
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
                app,
                dispatch_workspace_reply(
                    app,
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
    """Skills tree resolved at call time via `workspace_dir()`."""
    return workspace_dir() / "skills"


async def _commit_skill_approval(
    app: web.Application,
    ev: WorkspaceEvent,
    decision: str,
    reason: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Settle a `skill_approval` proposal card (mirror of
    `_commit_agent_approval`). Approve runs the promotion core shared with the
    `skill_promote` chat tool (validate → pending→active dir move). Reject
    archives the draft to `skills/rejected/` with the operator's reason
    sidecar, then leaves the reason as an operator comment carried to the assistant."""
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

    store = _store(app)
    comment_body = f"Rejected: {reason}" if reason else "Rejected (no reason given)."
    comment = WorkspaceComment.new(event_id=ev.event_id, author="operator", body=comment_body)
    try:
        store.append_comment(comment)
    except OSError:
        log.exception("skill_approval reject: comment append failed")
        return {"rejected": name}, None

    _spawn_reject_reply(app, ev, comment, comment_body)
    return {"rejected": name}, None


async def _commit_skill_refinement(
    app: web.Application,
    ev: WorkspaceEvent,
    decision: str,
    reason: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Settle a `skill_refinement` card. Approve applies the
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
        registry = app.get("tool_registry")
        names = frozenset(registry.names()) if registry is not None else None
        # The proposal was written against a particular SKILL.md. Since the
        # runtime stamps the new revision number from whatever is live at
        # THIS moment, a card approved after the skill moved on would be
        # silently rebased onto a text it was never about, and would read as
        # current. Refuse instead: a stale proposal is not a proposal for the
        # file that is there now. Cards filed before this field existed carry
        # no hash and are applied as before.
        base = str((ev.payload or {}).get("base_sha256") or "")
        if base:
            stale = await asyncio.to_thread(_base_moved, skills_dir, name, base)
            if stale:
                return None, ({"error": "skill_refinement_stale", "detail": stale}, 409)
        err = await asyncio.to_thread(_apply_skill_refinement, skills_dir, name, proposed, names)
        if err is not None:
            return None, ({"error": "refine_failed", "detail": err}, 409)
        # What the rewrite BECAME. The card carries the revision it was
        # measured against; the runtime stamps the new number inside
        # `replace_skill_body` and nothing carried it back out, so the
        # approval ledger held only the superseded version and "did this
        # rewrite measure better than the one it replaced" had no second
        # side. Assuming the measured version plus one is wrong whenever the
        # live version could not be ordered, which is the case the stamp
        # skips.
        applied = await asyncio.to_thread(_live_skill_version, skills_dir, name)
        if applied:
            # Best-effort, and deliberately after the write: the rewrite has
            # already landed, so failing the decision now would report a
            # refusal for something that happened. The ledger gets the
            # version from the returned meta either way; this is the card
            # keeping its own record of what it became.
            try:
                _store(app).merge_event_payload(
                    ev.event_id, {"applied_version": applied}
                )
            except Exception:
                log.warning(
                    "skill_refinement: could not record the applied version for %s",
                    name, exc_info=True,
                )
        return {"refined": name, "applied_version": applied}, None

    # Reject — skill untouched; record the reason for the assistant.
    store = _store(app)
    comment_body = f"Refinement rejected: {reason}" if reason else "Refinement rejected (no reason given)."
    comment = WorkspaceComment.new(event_id=ev.event_id, author="operator", body=comment_body)
    try:
        store.append_comment(comment)
    except OSError:
        log.exception("skill_refinement reject: comment append failed")
        return {"rejected": name}, None

    _spawn_reject_reply(app, ev, comment, comment_body)
    return {"rejected": name}, None


def _base_moved(skills_dir: Path, name: str, base_sha256: str) -> str | None:
    """A sentence naming the drift when the live SKILL.md is no longer the
    text this proposal was written against, else None."""
    import hashlib

    from tesseract.brain.skills import SKILL_FILENAME

    path = skills_dir / name / SKILL_FILENAME
    try:
        current = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{name} could not be read to check the proposal is still current: {exc}"
    if hashlib.sha256(current.encode("utf-8")).hexdigest() == base_sha256:
        return None
    return (
        f"{name} has changed since this was proposed, so the rewrite is "
        "against text that is no longer there. Nothing was applied. Reject "
        "this card; the next run measures the file as it stands now."
    )


def _subject_of(ev: WorkspaceEvent, applied_version: str = "") -> dict[str, Any]:
    """What a settled card was about, for the approval ledger's row.

    Only the kinds that HAVE a durable subject, and only the identifiers: the
    ledger is a decision record, not a copy of the payload. A refinement
    carries the skill and the revision it was measured against, which is what
    makes "did this rewrite measure better than the one it replaced" a
    question the ledger can answer at all.
    """
    payload = ev.payload or {}
    if ev.kind == "skill_refinement":
        subject: dict[str, Any] = {"skill": str(payload.get("name") or "")}
        # `version` is what the card was MEASURED against and is on the
        # payload from the moment it is filed. `applied_version` is what the
        # approved rewrite became, and is only on the payload once an apply
        # has stamped it, which is why it is passed in rather than assumed:
        # the key was read here before anything wrote it, so every ledger row
        # carried the superseded revision alone.
        if applied_version:
            subject["applied_version"] = applied_version
        elif payload.get("applied_version"):
            subject["applied_version"] = str(payload["applied_version"])
        if payload.get("version"):
            subject["version"] = str(payload["version"])
        return subject
    return {}


def _live_skill_version(skills_dir: Path, name: str) -> str:
    """The revision the skill carries NOW, read off the file that was just
    written. "" for a plain skill, which has none, and on any failure to
    parse: the ledger row is better short a field than carrying a guess."""
    from tesseract.brain.skills import load_skill_folder

    try:
        entry = load_skill_folder(skills_dir / name)
    except Exception:
        return ""
    return entry.version if entry is not None and entry.is_playbook else ""


def _apply_skill_refinement(
    skills_dir: Path, name: str, proposed_markdown: str, tool_names: frozenset[str] | None = None,
) -> str | None:
    """The approve route's half of a refinement: `brain/skills.py::
    replace_skill_body`, which `skill_refine` also calls once its gate is
    answered, so a live skill changes on one path whichever surface asked."""
    from tesseract.brain.skills import replace_skill_body

    return replace_skill_body(skills_dir, name, proposed_markdown, tool_names=tool_names)


def _spawn_reject_reply(
    app: web.Application,
    ev: WorkspaceEvent,
    comment: WorkspaceComment,
    comment_body: str,
) -> None:
    """Best-effort next-turn the assistant reply after a reject/refinement decision
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
                app,
                dispatch_workspace_reply(
                    app,
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


async def _commit_tuning_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``tuning_proposal`` by moving the one seam the card named.

    **The job that filed this card wrote nothing.** It read the runtime's own
    gauges and said what it would change; the change happens here, on the
    approval, through the seam that already owns the field. That is the same
    boundary ``_commit_working_set_proposal`` draws, and the reason both exist
    rather than the job editing config directly.

    The kind is checked against ``scheduler/proposals.py`` before anything is
    moved. A card carrying a kind this runtime does not declare is a card from
    a producer that is ahead of, or behind, the list, and applying it would be
    guessing which.
    """
    from tesseract.mirror.server.routes.settings import apply_role_ceilings
    from tesseract.scheduler import proposals

    payload = ev.payload or {}
    key = str(payload.get("kind") or "")
    try:
        declared = proposals.filed(key)
    except proposals.UnknownProposalKind as exc:
        return None, ({"error": "unknown_proposal_kind", "detail": str(exc)}, 400)

    if declared.key != "ceiling":
        # Every other declared kind is filed by a producer whose apply half is
        # not built yet. Refusing here is the difference between a card that
        # cannot be applied and one that reports success and moves nothing.
        return None, (
            {
                "error": "not_applicable_yet",
                "detail": (
                    f"'{declared.key}' proposals can be read and declined, but "
                    "approving one does not move anything yet"
                ),
            },
            400,
        )

    # **The card carries a LIST**, because one run can find two roles at their
    # cap and the operator should answer that once. Applying only the first
    # and reporting success is the shape this whole function exists to refuse,
    # so the whole list goes to the seam in ONE call: `apply_role_ceilings`
    # validates every name before it writes any of them, which is what makes
    # a partly applied card impossible rather than merely unlikely.
    wanted: dict[str, Any] = {}
    # **What the card was worked out FROM travels with it.** A card filed on
    # Monday and approved on Friday would otherwise put Monday's reading back
    # over a cap the operator moved by hand in between, and a card that says
    # "raise" would lower it. `_commit_change_proposal` in this file already
    # carries `expected_hash_before` for the same reason; this is that rule
    # for a number rather than a file.
    expected: dict[str, Any] = {}
    rows = payload.get("changes") or []
    for row in rows:
        # **A row this cannot read refuses the whole card.** Skipping it left
        # the rest to be applied and marked `applied`, so a card the operator
        # answered as three changes landed as two and said nothing about the
        # third. The comment below promises one call to one seam precisely so
        # that a partly applied card is impossible; silently dropping a row
        # was that promise being broken one line above it.
        if not isinstance(row, dict):
            return None, (
                {
                    "error": "invalid_proposal",
                    "detail": (
                        "one of the changes on this card is not readable, so "
                        "none of them have been applied"
                    ),
                },
                400,
            )
        role = str(row.get("role") or "")
        proposed = row.get("proposed_usd")
        if not role or proposed is None:
            return None, (
                {
                    "error": "invalid_proposal",
                    "detail": (
                        "one of the changes on this card does not say which "
                        "limit to move or what to move it to, so none of them "
                        "have been applied"
                    ),
                },
                400,
            )
        # **A change with no reading behind it is refused, not applied
        # unchecked.** Every card this job files carries `current_usd`
        # (`Change.as_json`), so a row without one did not come from the job
        # as it stands, and letting it through would be the staleness gate
        # opening for exactly the payload nobody can vouch for.
        was = row.get("current_usd")
        if was is None:
            return None, (
                {
                    "error": "invalid_proposal",
                    "detail": (
                        f"the change for {role} does not say what the limit "
                        "was when it was worked out, so it cannot be checked "
                        "against what the limit is now"
                    ),
                },
                400,
            )
        wanted[role] = proposed
        expected[role] = was
    if not wanted:
        return None, (
            {"error": "invalid_proposal", "detail": "no role and cap to apply"},
            400,
        )

    cost_cfg = app["config"].models.get("cost_tracking") or {}
    try:
        caps = await asyncio.to_thread(
            partial(
                apply_role_ceilings,
                app,
                wanted,
                warning_at_pct=float(cost_cfg.get("warning_at_pct", 0.75)),
                expected=expected,
            )
        )
    except ValueError as exc:
        return None, ({"error": "refused", "detail": str(exc)}, 400)
    except Exception as exc:  # noqa: BLE001 — the operator is waiting on it
        log.exception("tuning proposal: applying a ceiling failed")
        return None, ({"error": "apply_failed", "detail": str(exc)}, 500)

    applied = {role: caps.get(role) for role in wanted}
    _record_tuning_applied(app, ev, applied)
    return {"applied": applied}, None


def _record_tuning_applied(
    app: web.Application, ev: WorkspaceEvent, applied: dict[str, Any]
) -> None:
    """Write what landed back onto the card, so the pane says what changed.

    The same two keys the working set proposal writes, read by the same
    component. Without it the card goes on describing what it WOULD do after
    the operator has already agreed to it, which is the one moment they want
    to see the number that is now in the file.
    """
    lines = [
        f"{role} is now capped at {float(value):.2f} dollars a day."
        for role, value in sorted(applied.items())
        if value is not None
    ]
    try:
        _store(app).merge_event_payload(
            ev.event_id, {"applied": applied, "applied_lines": lines}
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "tuning_proposal: could not record what was applied", exc_info=True
        )


async def _commit_working_set_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``working_set_proposal`` by moving names on and off the dial.

    **The job that filed this card never touched either file**, and that is the
    whole boundary: the names under ``core:`` are the operator's half of a file
    whose other half is generated, so the change happens here, on the approval,
    and nowhere else.

    Two files, because the two halves have different privacy. Tools go to
    ``working_set.yaml``, which ships; playbooks go to
    ``workspace/skills/carried.txt``, which never leaves the machine. Both are
    written through their own generator so the annotations beside every name
    are rebuilt rather than carried forward from whatever a previous release
    wrote.

    Applied live rather than at the next restart, for the reason the tool
    switch in Conscience is: an operator who approved a change and then watched
    the next turn ignore it has no way to tell a slow write from a broken one.
    """
    from tesseract.brain.playbook_set import (
        load_carried_names,
        skills_dir,
        write_carried,
    )
    from tesseract.brain.skills import load_skills
    from tesseract.config.working_set import (
        UNDROPPABLE,
        config_path,
        load_core_tool_names,
    )
    from tesseract.scripts.generate_working_set import _registry_facts, render

    payload = ev.payload or {}
    def _names(key: str, field: str) -> list[str]:
        return [n for n in (str(r.get(field) or "") for r in payload.get(key) or []) if n]

    carry = _names("carry", "tool")
    drop = _names("drop", "tool")
    drop_playbooks = _names("drop_playbooks", "playbook")
    carry_playbooks = _names("carry_playbooks", "playbook")
    if not (carry or drop or drop_playbooks or carry_playbooks):
        return None, ({"error": "invalid_proposal", "detail": "nothing to apply"}, 400)

    registry = app.get("tool_registry")
    if (carry or drop) and registry is None:
        return None, ({"error": "the tool registry is not up yet"}, 503)

    changed: dict[str, Any] = {
        "carried": [], "dropped": [], "dropped_playbooks": [],
        "carried_playbooks": [], "refused_custom": [],
    }
    tiers_error: tuple[dict[str, Any], int] | None = None

    if carry or drop:
        try:
            chosen = set(load_core_tool_names())
        except (OSError, ValueError, KeyError) as exc:
            return None, ({"error": "invalid_proposal", "detail": str(exc)}, 500)
        for name in carry:
            # A tool the card named that has since been removed is skipped
            # rather than written: `working_set.yaml` naming a tool nothing
            # answers to stops the app at boot, and a card can outlive a
            # release that deleted one.
            tool = registry.tools.get(name)
            if tool is None or name in chosen:
                continue
            # And a tool the OPERATOR wrote never goes in this file, whatever
            # a card says. It is regenerated and it ships, so a machine-local
            # name here is destroyed by the next generator run or handed to
            # strangers as a tool they do not have. The proposing stage filters
            # these out only when it had a registry to ask; this is the check
            # that does not depend on that. `conscience.py::set_working_set`
            # makes the same split, to `write_promoted`.
            if getattr(tool, "origin", "shipped") == "custom":
                changed["refused_custom"].append(name)
                continue
            chosen.add(name)
            changed["carried"].append(name)
        for name in drop:
            # Both doors stay, whatever a card says. `tool_search` reaches
            # every tool not carried and `playbook_search` every playbook, so
            # dropping either is a capability cut rather than a saving. Read
            # from the one constant both this and the proposing stage use: a
            # card can outlive the release that added a lock, and this layer
            # is the one that re-derives its guards rather than trusting the
            # stage that filed the card.
            if name in UNDROPPABLE or name not in chosen:
                continue
            chosen.discard(name)
            changed["dropped"].append(name)

        def _write_tools() -> None:
            config_path().write_text(
                render(sorted(chosen), _registry_facts()), encoding="utf-8"
            )

        try:
            await asyncio.to_thread(_write_tools)
        except Exception as exc:  # noqa: BLE001
            log.exception("working_set_proposal: could not write the working set")
            return None, ({"error": "saved nothing", "detail": str(exc)}, 500)

        # Live NOW, beside the write that earned it, not at the end of the
        # function. Two things had to be true at once and were not: the
        # playbook half below can return early, which skipped this entirely,
        # and on the retry the names are already on the list so `changed` comes
        # back empty and a guard keyed on it never fires again in this
        # process. Meanwhile `_partly_saved` was telling the operator the tool
        # changes were "saved and are live". `conscience.py::set_working_set`
        # reloads unconditionally after its write, which is the shape this
        # should have copied.
        tiers_error = _reload_tiers(registry)

    if drop_playbooks or carry_playbooks:
        kept = set(load_carried_names())
        # Retired ones are not live. `prompt_content.py` filters them out of
        # what a turn is given, so carrying one back puts a name on the dial
        # that the prompt will never read, and the card that did it looks
        # like it worked.
        live = {
            e.name for e in load_skills(skills_dir()) if e.status != "retired"
        }
        for name in drop_playbooks:
            if name in kept:
                kept.discard(name)
                changed["dropped_playbooks"].append(name)
        for name in carry_playbooks:
            # Only one that is still there. A card can outlive the playbook it
            # named, and `carried.txt` keeps an unknown name visibly rather
            # than quietly, so writing one would leave a marked line nobody
            # asked for.
            if name in live and name not in kept:
                kept.add(name)
                changed["carried_playbooks"].append(name)
        # 089d0784: only when something actually came off. A card naming a
        # playbook the operator had already removed by hand would otherwise
        # rewrite the file to identical bytes and move its mtime, which is what
        # `generate_playbook_set --check` keys on.
        if changed["dropped_playbooks"] or changed["carried_playbooks"]:
            try:
                await asyncio.to_thread(write_carried, sorted(kept))
            except Exception as exc:  # noqa: BLE001
                log.exception("working_set_proposal: could not write the carried list")
                changed["dropped_playbooks"] = []
                changed["carried_playbooks"] = []
                # **Two files, and the first one may already have landed.**
                # Reporting "saved nothing" here was a lie about a working set
                # that had just changed on disk, and it returned before the
                # card could record the half that worked. So the record is
                # written first and the error names what survived.
                # The card stays pending, which is the recovery: approving it
                # again re-runs both halves, and the tool half is idempotent
                # because every name it wrote is already on the list. Nothing
                # said so, and a `What changed` block above a live Approve
                # button is not a thing an operator should have to infer.
                _record(
                    app, ev, changed,
                    outstanding=(
                        "The playbook half did not run. This card is still "
                        "open: approving it again finishes the job and repeats "
                        "nothing. Rejecting it leaves the tool changes above "
                        "in place, and you can undo them in Conscience."
                    ),
                )
                return changed, (
                    {
                        "error": _partly_saved(_accumulated(ev, changed)),
                        "detail": str(exc),
                    },
                    500,
                )

    # Separate from the write, which already landed. `_apply_tool_tiers` raises
    # when the file names a tool that is not registered, and reporting "saved
    # nothing" for a file that just changed on disk is a lie.
    # What actually landed, written back onto the card. A card can propose a
    # name a later release removed, or the floor, and both are skipped above:
    # a card that still reads as its original proposal after being applied
    # would be telling the operator something that did not happen. This is
    # locked decision 21's other half, said about one card rather than about a
    # filter.
    _record(app, ev, changed)

    if tiers_error is not None:
        return changed, tiers_error
    return changed, None


def _reload_tiers(registry: Any) -> tuple[dict[str, Any], int] | None:
    """Make the file that just changed take effect on the next turn.

    Separate from the write, which already landed: `_apply_tool_tiers` raises
    when the file names a tool that is not registered, and reporting "saved
    nothing" for a file that just changed on disk is a lie. Returns the error
    the caller should surface, or None.
    """
    from tesseract.brain.boot import _apply_tool_tiers, core_tool_names

    try:
        core_tool_names(refresh=True)
        _apply_tool_tiers(registry)
    except Exception as exc:  # noqa: BLE001
        log.exception("working_set_proposal: applied but could not take effect")
        return (
            {
                "error": (
                    "Saved, but it does not take effect until the next "
                    f"restart: {exc}"
                )
            },
            500,
        )
    return None


def _record(
    app: web.Application,
    ev: WorkspaceEvent,
    changed: dict[str, Any],
    *,
    outstanding: str = "",
) -> None:
    """Write what actually landed back onto the card, ACCUMULATING.

    A card can name a tool a later release removed, the floor, or one the
    operator wrote, and every one of those is skipped above. A card still
    reading as its original proposal after being applied would be telling the
    operator something that did not happen. Called on the success path AND on
    the partial-failure path, because the half that landed is exactly what the
    operator needs to see when the other half did not.

    **Merged into whatever the card already recorded, not written over it.**
    A partial apply leaves the card pending, so the operator can approve it
    again to finish the job. On that second pass `changed` is rebuilt from
    scratch and the names written the first time are skipped as already
    applied, so a plain overwrite would replace a true record of a live change
    with an empty one. `merge_event_payload` is a shallow merge over top-level
    keys, so `applied` has to be unioned here or not at all.
    """
    union = _accumulated(ev, changed)
    lines = _applied_lines(union)
    if outstanding:
        lines.append(outstanding)
    try:
        _store(app).merge_event_payload(
            ev.event_id, {"applied": union, "applied_lines": lines}
        )
    except Exception:  # noqa: BLE001
        log.warning("working_set_proposal: could not record what was applied", exc_info=True)


def _accumulated(ev: WorkspaceEvent, changed: dict[str, Any]) -> dict[str, Any]:
    """Everything this card has applied, across every attempt at it.

    Seeded from what the card already holds and overlaid with this call, so a
    key the card carries and this call does not is kept rather than dropped.
    That is not hypothetical tidiness: a direction added to one code path and
    not the other is exactly how the change count fell behind, and this is the
    same shape one level down.
    """
    previous = (ev.payload or {}).get("applied") or {}
    if not isinstance(previous, dict):
        previous = {}
    union: dict[str, Any] = {
        key: sorted(set(value or []))
        for key, value in previous.items()
        if isinstance(value, list)
    }
    for key, value in changed.items():
        union[key] = sorted(set(union.get(key) or []) | set(value))
    return union


def _partly_saved(changed: dict[str, Any]) -> str:
    """The error for a two-file apply where the first file landed.

    Takes the ACCUMULATED record, not this call's. On a retry the tool names
    written the first time are already on the list and are skipped, so this
    call's dict is empty while the change is live on disk. Telling the
    operator `Nothing was saved` at that moment is the same defect `_record`
    was fixed for, one function over, and the second message is the one they
    would act on.
    """
    tools = len(changed["carried"]) + len(changed["dropped"])
    if not tools:
        return "Nothing was saved."
    return (
        f"{tools} tool changes were saved and are live. The playbook list "
        "could not be written, so nothing changed there."
    )


def _applied_lines(changed: dict[str, Any]) -> list[str]:
    """What happened, in the sentences the card renders.

    Written here rather than in the pane, per AR-21's rule that no explanatory
    copy about a proposal lives in TSX: a second wording in a component is a
    second account of what the runtime did.
    """
    lines: list[str] = []
    if changed["carried"]:
        lines.append(
            "Now carried on every turn: " + ", ".join(sorted(changed["carried"])) + "."
        )
    if changed["dropped"]:
        lines.append(
            "No longer carried, and one tool_search away: "
            + ", ".join(sorted(changed["dropped"]))
            + "."
        )
    if changed["carried_playbooks"]:
        lines.append(
            "Playbooks now carried on every turn: "
            + ", ".join(sorted(changed["carried_playbooks"]))
            + "."
        )
    if changed["dropped_playbooks"]:
        lines.append(
            "No longer carried, and one playbook_search away: "
            + ", ".join(sorted(changed["dropped_playbooks"]))
            + "."
        )
    if changed.get("refused_custom"):
        lines.append(
            "Left alone, because these are tools you wrote and that list is "
            "yours to set in Conscience: "
            + ", ".join(sorted(changed["refused_custom"]))
            + "."
        )
    if not lines:
        lines.append(
            "Nothing changed. Every name on the card was already where it "
            "asked for, or is no longer here."
        )
    return lines


async def _commit_soul_proposal(
    app: web.Application,
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

    # The section the proposal named, not a fixed one: SOUL holds a section per
    # kind of growth, and committing every approved bullet to one heading would
    # undo that at the last step.
    try:
        section = validate_growth_section(str(payload.get("section") or "").strip())
    except ProposeError as exc:
        return None, ({"error": "invalid_proposal", "detail": str(exc)}, 400)

    bullet_line = f"- {bullet}\n"
    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=_SOUL_REL,
            action="append_to_section",
            content=bullet_line,
            section=section,
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
        app,
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
    app: web.Application,
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

    registry = app.get("tool_registry") if hasattr(app, "get") else None
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
    app: web.Application | None,
    ev: WorkspaceEvent,
    *,
    deny_all: bool,
    per_file: dict[str, str] | None = None,
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

    # Handed in rather than read off a request. The cockpit sends a verdict
    # per file in its decision body; a caller with no request (a tool, a
    # channel) sends none and every file follows the batch verdict.
    decisions: dict[str, str] = {
        relpath: verdict.strip().lower()
        for relpath, verdict in (per_file or {}).items()
        if isinstance(relpath, str) and isinstance(verdict, str)
        and verdict.strip().lower() in {"approved", "denied"}
    }
    if deny_all:
        for entry in files:
            relpath = entry.get("relpath") if isinstance(entry, dict) else None
            if isinstance(relpath, str):
                decisions[relpath] = "denied"

    vault_manager, indexer, librarian = _resolve_vault_dependencies(app)
    home_override = os.environ.get("TESSERACT_HOME") if app is not None else None
    home = Path(home_override).resolve() if home_override else TESSERACT_HOME
    cursor_path = home / "autonomy" / "vault-raw-cursors.jsonl"

    try:
        summary = await apply_ask_batch(
            files=files,
            decisions=decisions,
            vault_manager=vault_manager,
            indexer=indexer,
            cursor_path=cursor_path,
            librarian=librarian,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("vault_raw_ingest_batch apply crashed")
        return None, ({"error": "apply_crashed", "detail": str(exc)}, 500)

    return summary, None


def _resolve_vault_dependencies(app: Any) -> tuple[Any, Any, Any]:
    """Pull live VaultManager + VaultIndexer + VaultLibrarian off the Mirror
    tool registry so the approve handler reuses the same handles the runtime
    configured (FAISS + FTS + wiki compile). Falls back to fresh instances
    when the registry is not wired (CLI / test harness)."""
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
                librarian = getattr(tool, "_librarian", None)
                if isinstance(vm, VaultManager):
                    return (
                        vm,
                        idx if isinstance(idx, VaultIndexer) else None,
                        librarian,
                    )
    home_override = os.environ.get("TESSERACT_HOME")
    home = Path(home_override).resolve() if home_override else TESSERACT_HOME
    return VaultManager(vault_root=home / "vault"), None, None


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

        if decision == "approve" and ev.kind == "feedback_proposal":
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
            "agent_approval",
            "skill_approval",
            "skill_refinement",
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


# ── Operator direct editing of the workspace documents ────────
#
# The assistant proposes; the operator writes. Both land through the same
# `apply_change` — the operator path skips only the proposal card, not the
# hash check or the atomic commit, so a direct save racing a pending
# proposal settles the same way a second Approve would: whoever wrote
# first wins and the loser re-reviews against fresh bytes.
#
# Reachable only from the Mirror (local-only, no auth) and only for the
# `PROPOSABLE_PATHS` allowlist. `file_write` still cannot touch these
# files, so this does not widen what a tool can reach.


def _doc_row(target_path: str, spec: dict[str, object]) -> dict[str, Any]:
    path = resolve_proposable_path(target_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {
            "path": target_path,
            "label": str(spec.get("label") or ""),
            "exists": False,
            "bytes": 0,
            "lines": 0,
            "hash": "",
            "modified_at": None,
        }
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        modified_at = None
    return {
        "path": target_path,
        "label": str(spec.get("label") or ""),
        "exists": True,
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "hash": hash_text(text),
        "modified_at": modified_at,
    }


async def list_docs(request: web.Request) -> web.Response:
    """GET /api/workspace/docs — the editable workspace documents.

    One row per `PROPOSABLE_PATHS` entry, present or not: a doc missing
    from the operator's workspace is a fact the tab should show, not a
    row it should silently drop.
    """
    rows = await asyncio.to_thread(
        lambda: [_doc_row(path, spec) for path, spec in PROPOSABLE_PATHS.items()]
    )
    return web.json_response({"docs": rows, "count": len(rows)})


async def get_doc(request: web.Request) -> web.Response:
    """GET /api/workspace/doc?path=tesseract/workspace/SOUL.md — read one.

    `hash` is the concurrency token the save must echo back.
    """
    target_path = (request.query.get("path") or "").strip().replace("\\", "/")
    if target_path not in PROPOSABLE_PATHS:
        return web.json_response(
            {"error": "not_editable", "detail": f"path {target_path!r} is not an editable workspace document"},
            status=400,
        )
    path = resolve_proposable_path(target_path)
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except OSError as exc:
        return web.json_response(
            {"error": "read_failed", "detail": str(exc)}, status=404,
        )
    return web.json_response({
        "path": target_path,
        "label": str(PROPOSABLE_PATHS[target_path].get("label") or ""),
        "content": content,
        "hash": hash_text(content),
    })


async def save_doc(request: web.Request) -> web.Response:
    """POST /api/workspace/doc — operator-authored replacement of one doc.

    Body: ``{path, content, expected_hash}``. `expected_hash` is the
    `hash` from the read that seeded the editor; a mismatch means the file
    moved underneath the operator (the assistant's proposal was approved,
    an external editor saved) and returns 409 with the current bytes so
    they re-review rather than clobber.
    """
    app = request.app
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    target_path = str(body.get("path") or "").strip().replace("\\", "/")
    if target_path not in PROPOSABLE_PATHS:
        return web.json_response(
            {"error": "not_editable", "detail": f"path {target_path!r} is not an editable workspace document"},
            status=400,
        )
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be a string"}, status=400)
    expected_hash = body.get("expected_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        return web.json_response(
            {"error": "expected_hash is required — re-open the document to get one"},
            status=400,
        )

    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=target_path,
            action="replace",
            content=content,
            expected_hash_before=expected_hash,
        )
    except ConcurrentModificationError as exc:
        try:
            current = resolve_proposable_path(target_path).read_text(encoding="utf-8")
        except OSError:
            current = ""
        return web.json_response({
            "error": "concurrent_modification",
            "detail": str(exc),
            "expected_hash_before": exc.expected,
            "actual_hash": exc.actual,
            "current_content": current,
            "diff": compute_diff(
                current, content,
                target_label=str(PROPOSABLE_PATHS[target_path].get("label") or "file"),
            ),
        }, status=409)
    except ProposeError as exc:
        return web.json_response({"error": "invalid_edit", "detail": str(exc)}, status=400)
    except OSError as exc:
        log.exception("workspace doc save failed")
        return web.json_response({"error": "save_failed", "detail": str(exc)}, status=500)

    label = str(PROPOSABLE_PATHS[target_path].get("label") or "")
    await _broadcast_envelope(
        app,
        "soul_updated" if target_path == _SOUL_REL else "workspace_file_updated",
        {
            "path": target_path,
            "label": label,
            "content": content,
            "source": "operator_edit",
            "hash_after": applied.hash_after,
            "no_op_reason": applied.no_op_reason,
        },
    )

    try:
        await record_ask(
            session_id="workspace",
            call_id=applied.hash_after,
            tool_name="workspace_doc_save",
            input_summary={
                "target_path": target_path,
                "bytes_before": applied.bytes_before,
                "bytes_after": applied.bytes_after,
            },
            posture_source="operator_edit",
            result="allow_once",
            actor="operator",
        )
    except Exception:
        log.exception("workspace: doc-save ledger record failed")

    return web.json_response({
        "path": target_path,
        "label": label,
        "hash": applied.hash_after,
        "bytes": applied.bytes_after,
        "no_op_reason": applied.no_op_reason,
    })


