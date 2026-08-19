"""Slash-command handlers for the Mirror WebSocket.

Each `cmd_*` coroutine handles one operator-typed command. Dispatch lives in
`ws.py::_handle_command`.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.paths import TESSERACT_HOME
from tesseract.brain.session_ops import reflect_in_background
from tesseract.memory.log_notes import append_log_entry, resolve_runtime_subdir
from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_lifecycle import (
    _handle_chat_archive,
    _handle_chat_create,
    _handle_chat_rename,
    _handle_chat_restore,
    _handle_chat_switch,
    chat_is_busy,
    detach_deleted_chat,
    would_orphan_a_session,
)
from tesseract.mirror.server.envelope import make_envelope
from tesseract.mirror.server.routes.system import soul_path
from tesseract.mirror.server.session import ServerSession, send_envelope
from tesseract.scheduler.alarm_parser import (
    ALARM_HANDLER_DOTPATH,
    parse_alarm_spec,
    parse_alarm_when,
    parse_recurrence,
)

__all__ = ["ALARM_HANDLER_DOTPATH", "parse_alarm_spec", "parse_alarm_when", "parse_recurrence"]

log = logging.getLogger(__name__)

OBSERVER_MODES = {"meta", "maintenance"}
SECURITY_MODES = {"max", "standard", "headless"}


def _make_reflect_callbacks(
    app: web.Application, session: ServerSession, label: str
) -> "tuple":
    """Build (on_complete, on_error) callbacks for `reflect_in_background`.

    `on_complete`: writes a session `reflection_proposal` event to the
    workspace inbox carrying the actual save list (one bullet per
    `memory_save` / `diary_append` / `soul_growth_propose` call observed)
    so the operator sees *what* was saved, not just a count. The
    `mission_reflection_proposal` kind is historical-records-only — its
    producer (`app.py::_on_reflection_persisted`) was removed with the
    mission engine; the kind stays defined so old workspace events still
    deserialize.
    `on_error`: writes the same kind with `priority=7` so failures rise
    above ambient inbox noise rather than getting buried in logs.
    """
    async def on_complete(saves: list[dict[str, Any]], reason: str) -> None:
        try:
            from tesseract.workspace_events.events import WorkspaceEvent
            from tesseract.workspace_events.broadcast import broadcast_workspace_event

            store = app.get("workspace_event_store")
            if store is None:
                log.warning("reflect proposal skipped — no workspace_event_store on app")
                return
            count = len(saves)
            event_id = (
                f"evt_refl_session_{session.session_id[:16]}_"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
            )
            if count:
                bullet_lines: list[str] = []
                for s in saves[:6]:
                    tool = s.get("tool", "?")
                    title = s.get("title") or s.get("snippet") or "(no title)"
                    status = s.get("status") or ""
                    status_tag = f" [{status}]" if status and status not in {"saved", "completed"} else ""
                    bullet_lines.append(f"- [{tool}]{status_tag} {title}")
                if count > 6:
                    bullet_lines.append(f"- … and {count - 6} more")
                summary = (
                    f"Reflection complete: {count} write{'s' if count != 1 else ''}. "
                    "Expand for paths and content.\n"
                    + "\n".join(bullet_lines)
                )[:1200]
            else:
                summary = "Reflection complete: nothing load-bearing to save."
            event = WorkspaceEvent(
                event_id=event_id,
                ts=datetime.now(timezone.utc).isoformat(),
                kind="reflection_proposal",
                source="agent",
                title=f"Session reflection ({label})",
                summary=summary,
                payload={
                    "session_id": session.session_id,
                    "saves_count": count,
                    "saves": saves,
                    "reason": reason,
                    "label": label,
                },
            )
            store.append_event(event)
            await broadcast_workspace_event(app, event)
        except Exception:
            log.exception(
                "reflect_in_background on_complete: emit proposal failed (%s)", label
            )

    async def on_error(exc: BaseException, reason: str) -> None:
        try:
            from tesseract.workspace_events.events import WorkspaceEvent
            from tesseract.workspace_events.broadcast import broadcast_workspace_event

            store = app.get("workspace_event_store")
            if store is None:
                return
            event_id = (
                f"evt_refl_err_{session.session_id[:16]}_"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
            )
            event = WorkspaceEvent(
                event_id=event_id,
                ts=datetime.now(timezone.utc).isoformat(),
                kind="reflection_proposal",
                source="agent",
                title=f"Session reflection failed ({label})",
                summary=f"Reflection raised {type(exc).__name__}: {exc}"[:1200],
                priority=7,
                payload={
                    "session_id": session.session_id,
                    "reason": reason,
                    "label": label,
                    "error_type": type(exc).__name__,
                },
            )
            store.append_event(event)
            await broadcast_workspace_event(app, event)
        except Exception:
            log.exception(
                "reflect_in_background on_error: emit proposal failed (%s)", label
            )

    return on_complete, on_error


async def cmd_mode(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Change security mode from chat: `/mode <max|standard|headless>`.

    Mirrors the REST `POST /api/mode` path (`routes/system.py::set_mode`):
    runtime-only mutation of `app["config"].permissions`, then broadcast
    `mode_changed` to every live WS so all panes update in lockstep. Yaml
    persistence is Phase 18 work — until then, mode reverts to the
    permissions.yaml value on restart.
    """
    new_mode = (arg or "").strip().lower()
    if new_mode not in SECURITY_MODES:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {
                "message": f"/mode: unknown {new_mode!r}; expected one of {sorted(SECURITY_MODES)}",
                "severity": "warning",
            },
        ))
        return
    policy = app["config"].permissions
    previous = policy.mode
    if previous == new_mode:
        await send_envelope(session, make_envelope(
            "mode_changed", "routing", session.session_id,
            {"from": previous, "to": new_mode, "noop": True},
        ))
        return
    try:
        policy.set_mode(new_mode)
    except ValueError as exc:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": f"/mode: {exc}", "severity": "warning"},
        ))
        return
    log.info("security_mode (via /mode): %s -> %s", previous, policy.mode)
    sessions = app.get("sessions") or {}
    payload = {"from": previous, "to": policy.mode}
    for sid, ws in list(sessions.items()):
        envelope = make_envelope("mode_changed", "routing", sid, payload)
        try:
            await ws.send_json(envelope)
        except Exception:
            log.debug("/mode broadcast skipped for %s (likely closed)", sid)


async def cmd_observe(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Read-only `/observe` surface — runs the stateless Observer.observe()
    against the current history and emits the resulting text via
    `observer_result`. Does NOT touch the stateful transcript; background
    incremental observation is owned exclusively by ObserverSubscriber
    (armed via the Mirror toggle). Firing both paths here (pre-fix-pass
    2026-04-20) doubled cost and raced with the subscriber's loop_end."""
    mode = (arg or "meta").strip() or "meta"
    if mode not in OBSERVER_MODES:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {
                "message": f"/observe: unknown mode {mode!r}; expected meta|maintenance",
                "severity": "warning",
            },
        ))
        return
    observer = app.get("observer")
    if observer is None:
        await send_envelope(session, make_envelope(
            "observer_unavailable", "background", session.session_id, {"mode": mode},
        ))
        return
    try:
        result = await observer.observe(
            history=session.chat_session.history,
            mode=mode,
            session_id=session.session_id,
        )
    except Exception:
        log.exception("observer.observe failed for %s", session.session_id)
        await send_envelope(session, make_envelope(
            "observer_unavailable", "background", session.session_id,
            {"mode": mode, "reason": "observer_error"},
        ))
        return
    await send_envelope(session, make_envelope(
        "observer_result", "background", session.session_id,
        {"mode": mode, "observation": result},
    ))


async def cmd_soul_show(session: ServerSession) -> None:
    """Display-only — reads SOUL.md and emits `soul_updated` so the Mirror
    the From-agent section refreshes. Does NOT trigger reflection. Formerly
    `/reflect` (renamed 2026-04-20 F1); the misnamed command is why the
    operator could not trigger real reflection from the Mirror.
    """
    soul = soul_path()
    content = (
        await asyncio.to_thread(soul.read_text, encoding="utf-8")
        if soul.exists()
        else ""
    )
    await send_envelope(session, make_envelope(
        "soul_updated", "session", session.session_id,
        {"content": content, "source": "soul_show"},
    ))


async def cmd_reflect(app: web.Application, session: ServerSession) -> None:
    """Real reflection — backgrounded.

    Reflection runs on a snapshot of session history in an `asyncio.Task`,
    so `/reflect` returns immediately. The post-reflect librarian pass and
    SOUL-edit detection run inside the background callback. Outcome lands
    on the workspace tab as a `reflection_proposal` event (and a
    `reflect_started` envelope is sent right away so the chat shows
    progress).
    """
    bundle = app.get("memory_bundle")

    if bundle is not None and getattr(bundle, "librarian", None) is not None:
        try:
            await bundle.librarian.distill_personality_candidates(soul_path())
        except Exception:
            log.exception("pre-reflect distillation failed for %s", session.session_id)

    base_complete, base_error = _make_reflect_callbacks(app, session, label="manual")

    async def _on_complete(saves: list[dict[str, Any]], reason: str) -> None:
        # Run the librarian consolidation pass + SOUL transparency notification
        # AFTER the reflection turn finishes. Failures here are non-fatal —
        # they're surfaced via log + the proposal event.
        count = len(saves)
        librarian_stats: dict | None = None
        soul_edited = False
        try:
            soul = soul_path()
            mtime_before = soul.stat().st_mtime if soul.exists() else 0.0
            if bundle is not None and getattr(bundle, "librarian", None) is not None:
                try:
                    librarian_stats = await bundle.librarian.run_pass()
                except Exception:
                    log.exception(
                        "librarian pass failed during /reflect for %s",
                        session.session_id,
                    )
            mtime_after = soul.stat().st_mtime if soul.exists() else 0.0
            soul_edited = mtime_after > mtime_before

            last_reflected_at = datetime.now(timezone.utc).isoformat()
            app["last_reflected_at"] = last_reflected_at
            session.memory_saves = count

            try:
                stats = librarian_stats or {}
                probe = (
                    f"reflect-probe:{session.session_id[:8]}"
                    f"/saves={count}"
                    f"/soul={'1' if soul_edited else '0'}"
                    f"/lib={stats.get('promoted', 0)}:{stats.get('deduped', 0)}:{stats.get('skipped', 0)}"
                )
                body = (
                    f"Reflection complete (session={session.session_id[:8]}).\n"
                    f"Saves: {count}\n"
                    f"Soul edited: {'yes' if soul_edited else 'no'}\n"
                    f"Librarian: promoted={stats.get('promoted', 0)} "
                    f"deduped={stats.get('deduped', 0)} skipped={stats.get('skipped', 0)}\n"
                    f"<!-- {probe} -->"
                )
                append_log_entry(
                    header=f"## [reflect] Reflection {session.session_id[:8]} {last_reflected_at[:19]}Z",
                    body=body,
                    log_dir=resolve_runtime_subdir(app, "logs", "sessions", fallback_root=TESSERACT_HOME),
                    idempotency_probe=probe,
                )
            except Exception:
                log.exception(
                    "logs/sessions [reflect] append failed for %s",
                    session.session_id,
                )

            try:
                await send_envelope(session, make_envelope(
                    "reflect_result", "session", session.session_id,
                    {
                        "saves": count,
                        "saves_detail": saves,
                        "soul_edited": soul_edited,
                        "last_reflected_at": last_reflected_at,
                        "librarian": librarian_stats,
                    },
                ))
                if soul_edited:
                    content = await asyncio.to_thread(
                        soul.read_text, encoding="utf-8"
                    )
                    await send_envelope(session, make_envelope(
                        "soul_updated", "session", session.session_id,
                        {
                            "content": content,
                            "source": "reflect",
                            "transparency": True,
                            "last_reflected_at": last_reflected_at,
                        },
                    ))
            except Exception:
                log.exception(
                    "post-reflect envelope send failed for %s", session.session_id
                )
        finally:
            await base_complete(saves, reason)

    started = reflect_in_background(
        session.chat_session,
        reason="manual_reflect",
        on_complete=_on_complete,
        on_error=base_error,
    )
    if started is None:
        await send_envelope(session, make_envelope(
            "command_result", "command_result", session.session_id,
            {
                "command": "reflect",
                "ok": False,
                "reason": "reflection skipped — history too short or another reflection already running",
                "reason_code": "reflect_skipped",
                "severity": "info",
            },
        ))
        return

    await send_envelope(session, make_envelope(
        "command_result", "command_result", session.session_id,
        {
            "command": "reflect",
            "ok": True,
            "reason": "reflection started in background — check the workspace tab",
            "severity": "info",
        },
    ))


async def cmd_sessions(session: ServerSession) -> None:
    payload = [
        {
            "chat_id": record.chat_id,
            "title": record.title,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "turn_count": record.turn_count,
            "model": record.model,
        }
        for record in chat_store.list_records(limit=20)
    ]
    await send_envelope(session, make_envelope(
        "session_list", "session", session.session_id, {"sessions": payload},
    ))


def _known_chats(session: ServerSession) -> dict[str, str]:
    """``chat_id -> title`` for every chat this session can reach.

    On-disk records first, live meta second, so a title the operator renamed
    this connection wins over the one last written.
    """
    known = {
        record.chat_id: record.title
        for record in chat_store.list_records(include_archived=True)
    }
    known.update({cid: meta.title for cid, meta in session.chat_meta.items()})
    return known


def _resolve_chat(session: ServerSession, arg: str) -> tuple[str | None, str]:
    """Resolve what the operator typed to a chat_id. Returns ``(id, reason)``.

    A conversation is named by its title now, and a title is neither unique nor
    a filename, so an ambiguous one is reported rather than picked between —
    the old store resolved a name to a path and so could not be wrong about
    which conversation the operator meant.
    """
    arg = arg.strip()
    known = _known_chats(session)
    # TITLES FIRST, then the id namespace. Checking ids first meant a chat
    # titled with another chat's uuid resolved to that other chat — the
    # operator names what they can see, and what they can see is the title.
    matches = [cid for cid, title in known.items() if title == arg]
    if not matches:
        folded = arg.casefold()
        matches = [cid for cid, title in known.items() if title.casefold() == folded]
    if len(matches) == 1:
        return matches[0], ""
    if matches:
        return None, "ambiguous_name"
    return (arg, "") if arg in known else (None, "not_found")


async def cmd_save(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Write the active chat's own record. An argument renames it.

    There is nothing to name a save after: the conversation was given an id and
    a ``created_at`` when it was created, and this writes back to that record.
    So ``/save Something`` renames the chat rather than forking a second copy
    of it under a different filename.
    """
    opts = app["adapter_options"]
    if opts is None:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id, {"message": "chat infra not ready"},
        ))
        return
    if not session.chat_session.history:
        await send_envelope(session, make_envelope(
            "command_result", "command_result", session.session_id,
            {
                "command": "save",
                "ok": False,
                "reason": "nothing to save — session has no turns yet",
                "reason_code": "empty_history",
                "severity": "warning",
            },
        ))
        return
    chat_id = session.active_chat_id
    meta = session.chat_meta.get(chat_id)
    if meta is None:
        await send_envelope(session, make_envelope(
            "command_result", "command_result", session.session_id,
            {
                "command": "save",
                "ok": False,
                "reason": f"no such chat: {chat_id}",
                "reason_code": "unknown_chat",
                "severity": "warning",
            },
        ))
        return
    title = (arg or "").strip()
    if title:
        # The rename handler is what keeps live `chat_meta` and the record in
        # lock-step, and it emits `chat_renamed` so the sidebar follows.
        # Reaching it beats a second title writer here.
        await _handle_chat_rename(app, session, {"chat_id": chat_id, "title": title})
    chat_store.persist_session_chats(session, model=opts.model)
    await send_envelope(session, make_envelope(
        "session_saved", "session", session.session_id,
        {
            "session_id": session.session_id,
            "chat_id": chat_id,
            "title": meta.title,
            "path": str(chat_store.chats_dir() / f"{chat_id}.json"),
        },
    ))


def _not_found_message(arg: str, reason: str) -> str:
    if reason == "ambiguous_name":
        return f"more than one chat is called {arg} — use its id"
    return f"session not found: {arg}"


async def cmd_load(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Open a conversation. Not a restore in the old sense — there is nothing
    to copy anywhere: the chat keeps its id and its ``created_at``, and the
    turns taken after this land in the same record.

    An open chat is switched to; an archived one is un-archived and focused.
    Both are the existing chat-lifecycle handlers, so the frontend sees the
    same envelopes it sees when the operator clicks the chat in the sidebar.
    """
    if not arg:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": "usage: /load <chat title or id>", "severity": "warning"},
        ))
        return
    chat_id, reason = _resolve_chat(session, arg)
    if chat_id is None:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": _not_found_message(arg.strip(), reason), "severity": "warning"},
        ))
        return
    if chat_id in session.chat_order:
        await _handle_chat_switch(app, session, {"chat_id": chat_id})
    else:
        await _handle_chat_restore(app, session, {"chat_id": chat_id})


async def cmd_reset(
    app: web.Application,
    session: ServerSession,
    arg: str | None = None,
) -> None:
    """Reset the conversation.

    Two modes (operator picks via the frontend confirm dialog):
    - ``arg in (None, "reflect")`` — keep the conversation, start a fresh one.
      The active chat is written to its own record, archived, and a new chat
      opens in its place; reflection runs in the background on a snapshot.
    - ``arg == "clear"`` — the operator wants this conversation gone. The
      history is wiped in place and the emptied record written, so disk agrees
      with the screen. No reflection, no archive, nothing kept.

    Reset used to write a snapshot under a new name and then wipe the chat in
    place. Under one record per conversation there is no second file to hide
    the transcript in — wiping in place IS deleting it — so keeping it means
    keeping the record, and the operator reaches it in the drawer's archive
    view.
    """
    arg_norm = (arg or "").strip().lower()
    # `.get`, not `[...]`: `clear` is the operator asking for zero side effects
    # and must not depend on chat infra having finished booting.
    model = getattr(app.get("adapter_options"), "model", "") or ""

    if arg_norm == "clear":
        session.chat_session.reset()
        session.started_at = datetime.now(timezone.utc).isoformat()
        session.turn_count = 0
        try:
            chat_store.persist_session_chats(session, model=model)
        except Exception:
            log.exception("reset clear: persist failed for %s", session.session_id)
        await send_envelope(session, make_envelope(
            "session_reset", "session", session.session_id,
            {
                "autosaved": False,
                "chat_id": session.active_chat_id,
                "title": None,
                "path": None,
                "reflected": False,
                "reflect_saves": 0,
                "mode": "clear",
            },
        ))
        return

    outgoing_id = session.active_chat_id
    outgoing = session.chat_meta.get(outgoing_id)
    kept = bool(session.chat_session.history)
    if kept:
        try:
            chat_store.persist_session_chats(session, model=model)
        except Exception:
            # Refuse rather than reset. Archiving a conversation whose write
            # just failed would shelve a transcript that exists only in memory,
            # and sending `session_reset` would clear the screen showing it.
            log.exception("reset: persist failed for %s", session.session_id)
            await send_envelope(session, make_envelope(
                "command_result", "command_result", session.session_id,
                {
                    "command": "reset",
                    "ok": False,
                    "reason": "could not write this conversation — nothing was reset",
                    "reason_code": "persist_failed",
                    "severity": "error",
                },
            ))
            return
    # Layer D — reflect runs in the BACKGROUND on a snapshot of history, then a
    # `reflection_proposal` event lands in the workspace inbox. Fired before the
    # new chat opens so it reads the conversation being left, and the operator
    # gets control back in ~milliseconds instead of waiting 10-60s for the
    # reflect LLM stream.
    on_complete, on_error = _make_reflect_callbacks(app, session, label="reset")
    refl_pending = reflect_in_background(
        session.chat_session,
        reason="ws_reset",
        on_complete=on_complete,
        on_error=on_error,
    ) is not None
    # An empty chat has nothing to keep, so archiving it and opening another
    # empty one beside it would leave the operator two of the same thing.
    if kept:
        # Create BEFORE archiving: `archive_chat` refuses to archive the only
        # open chat, and the new one is what the operator is left looking at.
        await _handle_chat_create(app, session, {})
        if session.active_chat_id == outgoing_id:
            # Creation failed and said so with its own envelope. Nothing was
            # reset — the history is still live under the same chat — so
            # claiming otherwise would clear the operator's screen while the
            # backend kept appending to the conversation they think is gone.
            log.warning("reset: chat creation failed for %s, nothing reset", session.session_id)
            return
        await _handle_chat_archive(app, session, {"chat_id": outgoing_id})
    session.started_at = datetime.now(timezone.utc).isoformat()
    session.turn_count = 0
    await send_envelope(session, make_envelope(
        "session_reset", "session", session.session_id,
        {
            "autosaved": kept,
            "chat_id": outgoing_id if kept else None,
            "title": outgoing.title if (kept and outgoing) else None,
            "path": str(chat_store.chats_dir() / f"{outgoing_id}.json") if kept else None,
            "reflected": "pending" if refl_pending else False,
            "reflect_saves": 0,
            "mode": "reflect",
        },
    ))


async def cmd_compact(app: web.Application, session: ServerSession) -> None:
    try:
        before, after = await session.chat_session.compact()
    except Exception as exc:
        log.exception("manual compact failed for %s", session.session_id)
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id, {"message": f"compact failed: {exc}"},
        ))
        return
    await send_envelope(session, make_envelope(
        "session_compact", "session", session.session_id,
        {"tokens_before": before, "tokens_after": after, "trigger": "manual"},
    ))


async def cmd_compact_file(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Compact a stored conversation without disturbing the live one.

    Temporarily swaps the live ChatSession's history with the record's, runs
    compact(), writes the compacted result back to the SAME record, then
    restores. Guarded by the busy-turn check in the dispatcher so the swap
    can't race a live turn.

    Identity is untouched: ``chat_id``, ``created_at`` and ``started_at`` come
    off the record and go back onto it. Compacting a conversation is a shorter
    transcript of the same conversation, not a new one.
    """
    opts = app["adapter_options"]
    if opts is None:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id, {"message": "chat infra not ready"},
        ))
        return
    if not arg:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": "usage: /compact_file <chat title or id>", "severity": "warning"},
        ))
        return
    # Persist BEFORE anything is read. The record is up to one autosave
    # interval behind, and the compaction is written back over the live chat —
    # so compacting a stale copy discards every turn taken since the last tick.
    # It also has to happen before the lookup, or a live chat that has never
    # been written yet reports itself missing.
    try:
        chat_store.persist_session_chats(session, model=opts.model)
    except Exception:
        log.exception("compact_file: pre-flush failed for %s", session.session_id)
    chat_id, reason = _resolve_chat(session, arg)
    record = chat_store.load_chat(chat_id) if chat_id else None
    if record is None:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": _not_found_message(arg.strip(), reason), "severity": "warning"},
        ))
        return

    live_history = list(session.chat_session.history)
    session.chat_session.history = list(record.history)
    try:
        before, after = await session.chat_session.compact()
    except Exception as exc:
        log.exception("batch compact failed for %s", record.chat_id)
        session.chat_session.history = live_history
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": f"compact failed for {record.title}: {exc}"},
        ))
        return

    compacted_history = list(session.chat_session.history)
    session.chat_session.history = live_history

    try:
        chat_store.save_chat(replace(
            record,
            history=compacted_history,
            model=record.model or opts.model,
        ))
    except Exception as exc:
        log.exception("save after batch compact failed for %s", record.chat_id)
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": f"save failed for {record.title}: {exc}"},
        ))
        return
    # A chat still live in this session holds the pre-compaction history in
    # memory, and the next persist would write it straight back over what was
    # just saved. Bring the live copy along.
    live = session.chats.get(record.chat_id)
    if live is not None and live is not session.chat_session:
        live.history = list(compacted_history)
    elif live is not None:
        session.chat_session.history = list(compacted_history)

    await send_envelope(session, make_envelope(
        "session_compact_file", "session", session.session_id,
        {"chat_id": record.chat_id, "title": record.title,
         "tokens_before": before, "tokens_after": after},
    ))


def _delete_failed(session: ServerSession, reason: str, code: str, severity: str = "warning") -> dict:
    return make_envelope(
        "command_result", "command_result", session.session_id,
        {
            "command": "delete",
            "ok": False,
            "reason": reason,
            "reason_code": code,
            "severity": severity,
        },
    )


async def cmd_delete(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Delete a conversation's record, and the conversation with it.

    Archive-before-delete (D1) is the same policy the REST route enforces: a
    conversation the operator can still see in the sidebar is not one they have
    decided to lose.

    Takes ``app`` because a chat can be open in a SECOND connection that was
    never told about the archive — nothing broadcasts one — and a delete that
    cleaned only this session's registry would be undone by that connection's
    next persist. `detach_deleted_chat` is the same cleanup the REST route runs.
    """
    if not arg:
        await send_envelope(session, make_envelope(
            "stream_error", "loop", session.session_id,
            {"message": "usage: /delete <chat title or id>", "severity": "warning"},
        ))
        return
    chat_id, resolve_reason = _resolve_chat(session, arg)
    if chat_id is None:
        await send_envelope(session, _delete_failed(
            session, _not_found_message(arg.strip(), resolve_reason), resolve_reason,
        ))
        return
    title = _known_chats(session).get(chat_id, chat_id)
    if chat_id in session.chat_order:
        await send_envelope(session, _delete_failed(
            session, f"archive {title} before deleting it", "not_archived",
        ))
        return
    if would_orphan_a_session(app, chat_id):
        await send_envelope(session, _delete_failed(
            session, f"{title} is the only chat open in another window", "last_open_chat",
        ))
        return
    if chat_is_busy(app, chat_id):
        await send_envelope(session, _delete_failed(
            session, f"{title} is mid-turn — try again when it finishes", "chat_busy",
        ))
        return
    ok, reason = chat_store.delete_chat(chat_id)
    if not ok:
        # not_found → warning (operator typo, recoverable, orb stays normal).
        # io_error → error (filesystem fault, orb red).
        severity = "warning" if reason in ("not_found", "invalid_id") else "error"
        human = (
            f"session not found: {title}"
            if reason == "not_found"
            else f"not a usable chat id: {chat_id}"
            if reason == "invalid_id"
            else f"delete failed for {title}: {reason}"
        )
        await send_envelope(session, _delete_failed(session, human, reason, severity))
        return
    # EVERY live session, not just this one. An archived chat stays in `chats`
    # for the restore window and teardown persists everything it holds, so a
    # delete that cleaned one registry gets the conversation written back by
    # another connection.
    detach_deleted_chat(app, chat_id)
    await send_envelope(session, make_envelope(
        "session_deleted", "session", session.session_id,
        {"chat_id": chat_id, "title": title},
    ))
    await cmd_sessions(session)


async def cmd_alarm_set(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Queue an alarm (one-shot or recurring). Usage:

        /alarm-set <label> <when> [message]
        /alarm-set <label> daily at 9am stand up
        /alarm-set <label> "in 20 minutes" "take out the trash"

    `<when>` accepts: compact duration ('20m', '1h30m'), ISO 8601, clock
    ('9am', '14:00'), 'tomorrow at HH', 'next mon at HH', 'in N minutes',
    plus optional recurrence prefix ('daily', 'weekdays', 'every mon',
    'every 2h'). Multi-word phrases may be quoted or typed bare — we parse
    greedily and the tail is the message.
    """
    registry = app.get("alarm_registry")
    if registry is None:
        await _emit_alarm_error(session, "alarm_unavailable", "alarm_registry not ready")
        return
    if not arg:
        await _emit_alarm_error(session, "alarm_invalid", "usage: /alarm-set <label> <when> [message]")
        return
    head, _, tail = arg.strip().partition(" ")
    label = head.strip()
    if not label or not tail.strip():
        await _emit_alarm_error(session, "alarm_invalid", "usage: /alarm-set <label> <when> [message]")
        return
    now = datetime.now(timezone.utc)
    run_at, recurrence, message = parse_alarm_spec(tail.strip(), now)
    if run_at is None or run_at <= now:
        await _emit_alarm_error(session, "alarm_invalid", f"cannot parse when-expression: {tail.strip()!r}", label=label)
        return
    try:
        alarm = registry.add(
            label=label,
            run_at=run_at,
            handler_dotpath=ALARM_HANDLER_DOTPATH,
            message=message,
            recurrence=recurrence,
        )
    except ValueError as exc:
        await _emit_alarm_error(session, "alarm_duplicate", str(exc), label=label)
        return
    await send_envelope(session, make_envelope(
        "schedule_state", "schedule", session.session_id,
        {
            "action": "alarm_queued",
            "alarm": _alarm_to_envelope(alarm),
        },
    ))


def _alarm_to_envelope(alarm) -> dict:
    """Shape an alarm for `schedule_state` envelopes. Keeps `name` for the
    S4 frontend (reads `data.alarm.name`) and adds `id` + `label` + `recurrence`."""
    return {
        "id": alarm.id,
        "name": alarm.label,  # S4 back-compat
        "label": alarm.label,
        "run_at": alarm.run_at.isoformat(),
        "message": alarm.message,
        "recurrence": alarm.recurrence.to_dict() if alarm.recurrence else None,
    }


async def _emit_alarm_error(
    session: ServerSession,
    action: str,
    reason: str,
    *,
    label: str | None = None,
) -> None:
    payload: dict = {"action": action, "reason": reason}
    if label is not None:
        payload["name"] = label
        payload["label"] = label
    await send_envelope(session, make_envelope(
        "schedule_state", "schedule", session.session_id, payload,
    ))


def _schedule_state_envelope(session_id: str, data: dict) -> dict:
    return make_envelope("schedule_state", "schedule", session_id, data)


def _runtime_snapshot(scheduler, name: str) -> dict | None:
    try:
        return scheduler.runtime_state(name)
    except KeyError:
        return None


async def _emit_schedule_state(session: ServerSession, action: str, name: str, scheduler) -> None:
    rt = _runtime_snapshot(scheduler, name)
    payload: dict = {"action": action, "job_name": name}
    if rt is not None:
        payload.update({
            "enabled": rt["enabled"],
            "cadence": rt["cadence"],
            "circuit_broken": rt.get("circuit_broken", False),
            "consecutive_failures": rt.get("consecutive_failures", 0),
            "model_role": rt.get("model_role"),
            "effective_model_role": rt.get("effective_model_role"),
            "uses_llm": rt.get("uses_llm", False),
        })
    await send_envelope(session, _schedule_state_envelope(session.session_id, payload))


async def _emit_schedule_error(session: ServerSession, action: str, reason: str, name: str | None = None) -> None:
    payload: dict = {"action": action, "reason": reason}
    if name is not None:
        payload["job_name"] = name
    await send_envelope(session, _schedule_state_envelope(session.session_id, payload))


async def _cmd_schedule_set_enabled(
    app: web.Application, session: ServerSession, arg: str | None, *, enabled: bool
) -> None:
    verb = "enable" if enabled else "disable"
    scheduler = app.get("scheduler")
    if scheduler is None:
        await _emit_schedule_error(session, "schedule_unavailable", "scheduler not running")
        return
    name = (arg or "").strip()
    if not name:
        await _emit_schedule_error(session, "schedule_invalid", f"usage: /schedule-{verb} <name>")
        return
    try:
        scheduler.set_enabled(name, enabled)
    except KeyError:
        await _emit_schedule_error(session, "schedule_not_found", f"no job named {name!r}", name)
        return
    action = "enabled" if enabled else "disabled"
    await _emit_schedule_state(session, action, name, scheduler)


async def cmd_schedule_enable(app: web.Application, session: ServerSession, arg: str | None) -> None:
    await _cmd_schedule_set_enabled(app, session, arg, enabled=True)


async def cmd_schedule_disable(app: web.Application, session: ServerSession, arg: str | None) -> None:
    await _cmd_schedule_set_enabled(app, session, arg, enabled=False)


async def cmd_schedule_run_now(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Fire a job off-schedule. Usage: /schedule-run-now <name>.

    The scheduler engine broadcasts `schedule_job_started` / `schedule_job_done`
    envelopes on the same channel the tick loop uses — frontend `JobRow` reacts
    to the existing flash state without extra plumbing. We still send back a
    `schedule_state` ack so the UI can toast on rejection paths.
    """
    scheduler = app.get("scheduler")
    if scheduler is None:
        await _emit_schedule_error(session, "schedule_unavailable", "scheduler not running")
        return
    name = (arg or "").strip()
    if not name:
        await _emit_schedule_error(session, "schedule_invalid", "usage: /schedule-run-now <name>")
        return
    try:
        scheduler.runtime_state(name)  # existence probe — KeyError surfaces synchronously
    except KeyError:
        await _emit_schedule_error(session, "schedule_not_found", f"no job named {name!r}", name)
        return
    scheduler.spawn_tracked_task(
        scheduler.run_now(name, trigger="operator"),
        name=f"scheduler-run-now-{name}",
    )
    await _emit_schedule_state(session, "run_now", name, scheduler)


async def cmd_schedule_set_cadence(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Update cadence in the runtime registry. Usage: /schedule-set-cadence <name> <cadence>."""
    scheduler = app.get("scheduler")
    if scheduler is None:
        await _emit_schedule_error(session, "schedule_unavailable", "scheduler not running")
        return
    try:
        parts = shlex.split(arg or "")
    except ValueError:
        parts = []
    if len(parts) < 2:
        await _emit_schedule_error(
            session, "schedule_invalid",
            "usage: /schedule-set-cadence <name> <cron-or-interval>",
        )
        return
    name, cadence = parts[0], " ".join(parts[1:])
    try:
        scheduler.set_cadence(name, cadence)
    except KeyError:
        await _emit_schedule_error(session, "schedule_not_found", f"no job named {name!r}", name)
        return
    except ValueError as exc:
        await _emit_schedule_error(session, "schedule_invalid", str(exc), name)
        return
    await _emit_schedule_state(session, "cadence_set", name, scheduler)


async def cmd_schedule_set_role(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Update the per-job model_role override. Usage: /schedule-set-role <name> <role>.

    Pass `-` (or `default`/`none`) as the role to clear the override and
    revert to the handler's `default_model_role`. The engine validates
    against `roles.yaml` and against the handler's `uses_llm` flag — a
    missing role or non-LLM handler returns a `schedule_invalid` error
    envelope instead of being persisted.
    """
    scheduler = app.get("scheduler")
    if scheduler is None:
        await _emit_schedule_error(session, "schedule_unavailable", "scheduler not running")
        return
    try:
        parts = shlex.split(arg or "")
    except ValueError:
        parts = []
    if len(parts) < 2:
        await _emit_schedule_error(
            session, "schedule_invalid",
            "usage: /schedule-set-role <name> <role-or-dash>",
        )
        return
    name, raw_role = parts[0], parts[1]
    cleared = raw_role.strip().lower() in ("-", "default", "none", "")
    role: str | None = None if cleared else raw_role.strip()
    try:
        scheduler.set_model_role(name, role)
    except KeyError:
        await _emit_schedule_error(session, "schedule_not_found", f"no job named {name!r}", name)
        return
    except (ValueError, RuntimeError) as exc:
        await _emit_schedule_error(session, "schedule_invalid", str(exc), name)
        return
    await _emit_schedule_state(session, "model_role_set", name, scheduler)


async def cmd_alarm_cancel(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Cancel (delete) a pending alarm by label or id-prefix.

    Usage: /alarm-cancel <handle>. For recurring alarms this removes the whole
    rule — there is no 'skip next fire' state. `/alarm-delete` is an alias.
    """
    registry = app.get("alarm_registry")
    if registry is None:
        await _emit_alarm_error(session, "alarm_unavailable", "alarm_registry not ready")
        return
    if not arg:
        await _emit_alarm_error(session, "alarm_invalid", "usage: /alarm-cancel <handle>")
        return
    handle = arg.strip()
    removed = registry.cancel(handle)
    if removed is None:
        suggestions = registry.suggestions(handle)
        payload: dict = {"action": "alarm_not_found", "name": handle, "label": handle}
        if suggestions:
            payload["suggestions"] = suggestions
        await send_envelope(session, make_envelope(
            "schedule_state", "schedule", session.session_id, payload,
        ))
        return
    await send_envelope(session, make_envelope(
        "schedule_state", "schedule", session.session_id,
        {
            "action": "alarm_cancelled",
            "name": removed.label,  # S4 back-compat
            "alarm": _alarm_to_envelope(removed),
        },
    ))


async def cmd_alarm_list(app: web.Application, session: ServerSession) -> None:
    """List pending alarms. Usage: /alarm-list."""
    registry = app.get("alarm_registry")
    if registry is None:
        await _emit_alarm_error(session, "alarm_unavailable", "alarm_registry not ready")
        return
    pending = registry.list_pending()
    await send_envelope(session, make_envelope(
        "schedule_state", "schedule", session.session_id,
        {
            "action": "alarm_list",
            "alarms": [_alarm_to_envelope(a) for a in pending],
        },
    ))


async def cmd_alarm_snooze(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Snooze (reschedule) a pending alarm. Usage: /alarm-snooze <handle> [duration].

    `duration` defaults to 10m. For recurring alarms, only the upcoming fire
    is shifted — the recurrence cycle itself is unchanged.
    """
    registry = app.get("alarm_registry")
    if registry is None:
        await _emit_alarm_error(session, "alarm_unavailable", "alarm_registry not ready")
        return
    if not arg:
        await _emit_alarm_error(session, "alarm_invalid", "usage: /alarm-snooze <handle> [duration]")
        return
    try:
        parts = shlex.split(arg)
    except ValueError:
        parts = []
    if not parts:
        await _emit_alarm_error(session, "alarm_invalid", "usage: /alarm-snooze <handle> [duration]")
        return
    handle = parts[0]
    duration = parts[1] if len(parts) >= 2 else "10m"
    now = datetime.now(timezone.utc)
    # Time-only parse: snooze does not take a recurrence prefix. Using the
    # combined `parse_alarm_spec` here would silently accept 'every 2h' and
    # the recurrence side of the result would be dropped on the floor.
    run_at = parse_alarm_when(duration, now)
    if run_at is None or run_at <= now:
        await _emit_alarm_error(
            session, "alarm_invalid",
            f"cannot parse snooze duration: {duration!r}",
            label=handle,
        )
        return
    alarm = registry.snooze(handle, run_at)
    if alarm is None:
        suggestions = registry.suggestions(handle)
        payload: dict = {"action": "alarm_not_found", "name": handle, "label": handle}
        if suggestions:
            payload["suggestions"] = suggestions
        await send_envelope(session, make_envelope(
            "schedule_state", "schedule", session.session_id, payload,
        ))
        return
    await send_envelope(session, make_envelope(
        "schedule_state", "schedule", session.session_id,
        {
            "action": "alarm_snoozed",
            "alarm": _alarm_to_envelope(alarm),
            "snooze_to": run_at.isoformat(),
        },
    ))


async def cmd_alarm_dismiss(app: web.Application, session: ServerSession, arg: str | None) -> None:
    """Dismiss a (just-)fired alarm. Usage: /alarm-dismiss <handle>.

    For one-shot alarms this is a no-op — the alarm is already gone from the
    pending queue. For the recently-fired buffer it clears the entry so
    'snooze the last one' semantics don't resurrect it.
    """
    registry = app.get("alarm_registry")
    if registry is None:
        await _emit_alarm_error(session, "alarm_unavailable", "alarm_registry not ready")
        return
    if not arg:
        await _emit_alarm_error(session, "alarm_invalid", "usage: /alarm-dismiss <handle>")
        return
    handle = arg.strip()
    before = len(registry.recently_fired)
    # Match exact id or exact label only. Id-prefix matching like resolve()'s
    # would need the same `len(by_id) == 1` ambiguity guard, and the Dismiss
    # button passes the full id anyway — there's no ergonomic win that would
    # justify the silent-clear risk on short prefixes.
    registry.recently_fired = type(registry.recently_fired)(
        [f for f in registry.recently_fired if f.id != handle and f.label != handle],
        maxlen=registry.recently_fired.maxlen,
    )
    cleared = before - len(registry.recently_fired)
    action = "alarm_dismissed" if cleared > 0 else "alarm_not_found"
    await send_envelope(session, make_envelope(
        "schedule_state", "schedule", session.session_id,
        {"action": action, "name": handle, "label": handle, "cleared": cleared},
    ))
