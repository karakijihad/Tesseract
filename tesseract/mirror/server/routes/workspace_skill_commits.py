"""Commit handlers for the identity/skill decision cards.

`agent_approval`, `skill_approval`, `skill_refinement`, `skill_retirement` —
the four kinds whose approve promotes, replaces or retires a file under the
operator's `agents/`/`workspace/skills/` trees. Split out of
`routes/workspace.py` so that file holds the dispatcher and its two
proposal-shaped domains stay under the line cap each.

`apply_decision` in `workspace.py` imports every `_commit_*` here by name and
calls it directly, which is also what keeps
`tesseract.mirror.server.routes.workspace._commit_skill_refinement` (etc.)
importable for existing tests: the name lives in `workspace.py`'s namespace
because `apply_decision` needs it there. `_skills_dir`, monkeypatched by
several tests to point at a fixture tree, is NOT re-exported that way —
patching a bare-name lookup only works from the module where the function
that reads it is defined, so those tests target
`tesseract.mirror.server.routes.workspace_skill_commits._skills_dir` directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.mirror.server.routes.workspace_shared import _store
from tesseract.paths import workspace_dir
from tesseract.workspace_events import WorkspaceComment, WorkspaceEvent

log = logging.getLogger(__name__)


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
    `_commit_agent_approval`). Approve runs the promotion core shared by
    every trigger that writes a skill (`brain/skill_door.py::
    promote_pending_skill`; validate then pending→active dir move). Reject
    archives the draft to `skills/rejected/` with the operator's reason
    sidecar, then leaves the reason as an operator comment carried to the assistant."""
    name = str((ev.payload or {}).get("name") or "")
    if not name:
        return None, ({"error": "skill_approval_missing_name"}, 400)

    skills_dir = _skills_dir()

    if decision == "approve":
        from tesseract.brain.skill_door import promote_pending_skill

        entry, err = await asyncio.to_thread(promote_pending_skill, skills_dir, name)
        if err is not None:
            return None, ({"error": "promote_failed", "detail": err}, 409)
        return {"promoted": name}, None

    from tesseract.brain.skill_door import archive_rejected_skill

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
        origin = str((ev.payload or {}).get("origin") or "")
        err = await asyncio.to_thread(
            _apply_skill_refinement, skills_dir, name, proposed, names, origin,
        )
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
        # Bytes, the same thing the proposer hashed: decoding folds CRLF.
        current = path.read_bytes()
    except OSError as exc:
        return f"{name} could not be read to check the proposal is still current: {exc}"
    if hashlib.sha256(current).hexdigest() == base_sha256.strip().lower():
        return None
    return (
        f"{name} has changed since this was proposed, so the rewrite is "
        "against text that is no longer there. Nothing was applied. Reject "
        "this card and ask for a fresh proposal written from the file as it "
        "stands now."
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
    if ev.kind == "skill_retirement":
        subject = {"skill": str(payload.get("name") or "")}
        if payload.get("live_version"):
            subject["version"] = str(payload["live_version"])
        return subject
    return {}


def _live_skill_version(skills_dir: Path, name: str) -> str:
    """The revision the skill carries NOW, read off the file that was just
    written. "" for a skill that never declared one, and on any failure to
    parse: the ledger row is better short a field than carrying a guess."""
    from tesseract.brain.skills import load_skill_folder

    try:
        entry = load_skill_folder(skills_dir / name)
    except Exception:
        return ""
    return entry.version if entry is not None else ""


def _apply_skill_refinement(
    skills_dir: Path,
    name: str,
    proposed_markdown: str,
    tool_names: frozenset[str] | None = None,
    origin: str = "",
) -> str | None:
    """The approve route's half of a refinement: `brain/skills.py::
    replace_skill_body`, the one path that changes a live skill.

    `origin == "revert"` is the one case that may land on a retired skill:
    `skill_refine`'s `revert` action is a person's act on it by construction,
    so the write is allowed through, the status is restamped `active` in the
    same pass rather than leaving the restored content retired, and the name
    goes back onto the carried dial retiring took it off of.
    """
    from tesseract.brain.playbook_set import CARRIED_FILENAME, add_to_carried
    from tesseract.brain.skills import replace_skill_body, set_skill_status

    err = replace_skill_body(
        skills_dir, name, proposed_markdown, tool_names=tool_names,
        allow_retired=(origin == "revert"),
    )
    if err is not None:
        return err
    if origin == "revert":
        status_err = set_skill_status(skills_dir / name, "active")
        if status_err is not None:
            return status_err
        # Best-effort, and deliberately after the write: the revert has
        # already landed, so failing the decision now would report a refusal
        # for something that happened.
        carried_err = add_to_carried(name, skills_dir / CARRIED_FILENAME)
        if carried_err:
            log.warning(
                "skill revert: could not add %s back to the carried list: %s",
                name, carried_err,
            )
    return None


async def _commit_skill_retirement(
    app: web.Application,
    ev: WorkspaceEvent,
    decision: str,
    reason: str | None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Settle a `skill_retirement` card. Approve retires the skill; reject
    leaves it exactly as it was. Neither side is the act itself: filing the
    card asked, and this is the only place the question is answered."""
    name = str((ev.payload or {}).get("name") or "")
    if not name:
        return None, ({"error": "skill_retirement_missing_name"}, 400)

    skills_dir = _skills_dir()

    if decision == "approve":
        err = await asyncio.to_thread(_apply_skill_retirement, skills_dir, name)
        if err is not None:
            return None, ({"error": "retire_failed", "detail": err}, 409)
        return {"retired": name}, None

    store = _store(app)
    comment_body = f"Kept: {reason}" if reason else "Kept (no reason given)."
    comment = WorkspaceComment.new(event_id=ev.event_id, author="operator", body=comment_body)
    try:
        store.append_comment(comment)
    except OSError:
        log.exception("skill_retirement reject: comment append failed")
        return {"kept": name}, None

    _spawn_reject_reply(app, ev, comment, comment_body)
    return {"kept": name}, None


def _apply_skill_retirement(skills_dir: Path, name: str) -> str | None:
    """Flip the skill's status, and take it off the carried dial: the two
    things that actually change behaviour."""
    from tesseract.brain.playbook_set import CARRIED_FILENAME, remove_from_carried
    from tesseract.brain.skills import set_skill_status

    err = set_skill_status(skills_dir / name, "retired")
    if err is not None:
        return err
    carried_err = remove_from_carried(name, skills_dir / CARRIED_FILENAME)
    if carried_err:
        # The verdict landed; the dial is housekeeping. Refusing the decision
        # here would tell the operator a retirement failed that did not.
        log.warning(
            "skill_retirement: %s is retired but could not come off the carried list: %s",
            name, carried_err,
        )
    return None


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
