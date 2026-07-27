"""Daemon-side session/turn IPC handlers — the ``_DISPATCH_TABLE`` targets
for ``new_session``, ``attach``, ``user_input``, ``approval``,
``cancel_worker``, ``detach``, ``delete_session``, ``rename_session``,
``shutdown``, ``reload``, and ``activity_snapshot``.

Extracted from ``daemon.py`` (module-size cleanup, Task 7.5) as a mixin,
matching the existing ``lane_handlers.py`` / ``named_lane_handlers.py``
pattern. ``ControllerDaemon`` inherits this mixin; every method resolves
``self._registry`` / ``self._pending_approvals`` / ``self._push_or_disconnect``
etc. (set up in ``daemon.py``'s ``__init__``) via the MRO. The mixin holds
no state of its own.

References to ``daemon`` types are annotations only (``TYPE_CHECKING`` +
``from __future__ import annotations``) — no import cycle with ``daemon``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from .events import AssistantTextEvent, UserTextEvent
from .protocol import (
    AckPush,
    ActivitySnapshotPush,
    AttachedPush,
    ControllerAskSettledPush,
    ErrorPush,
    ParkedAsksSnapshotPush,
    ReloadCompletePush,
    SessionDeletedPush,
    SessionListPush,
    SessionRenamedPush,
    SessionStatusPush,
)
from .transcript import TranscriptReader

if TYPE_CHECKING:
    from .daemon import _ClientConn
    from .protocol import (
        ActivitySnapshotMessage,
        ApprovalMessage,
        AttachMessage,
        CancelWorkerMessage,
        DecideParkedAskMessage,
        DeleteSessionMessage,
        DetachMessage,
        ListSessionsMessage,
        NewSessionMessage,
        ParkedAsksSnapshotMessage,
        ReloadMessage,
        RenameSessionMessage,
        ShutdownMessage,
        UserInputMessage,
    )
    from .sessions import ControllerSessionRecord

log = logging.getLogger("tesseract.orchestrator.tars_controller.daemon")


class _SessionHandlersMixin:
    """The session-lifecycle, turn-dispatch, reload, and shutdown IPC verb
    handlers. Mixed into ``ControllerDaemon``."""

    async def _on_new_session(
        self, conn: _ClientConn, msg: NewSessionMessage
    ) -> None:
        record = self._registry.create_session(
            mode=msg.mode,
            origin=msg.origin,
            title=msg.title,
            controller_id=self._controller_id,
            preferred_coder=msg.preferred_coder,
        )
        self._sessions_attached.setdefault(record.session_id, set()).add(
            conn.writer_id
        )
        conn.sessions.add(record.session_id)
        self._refresh_active_sessions()

        await conn.outbound.put(
            AttachedPush(
                session=record.model_dump(mode="json"),
                replay_events=[],
                end_offset=0,
            ).model_dump(mode="json")
        )

    async def _on_list_sessions(
        self, conn: _ClientConn, msg: ListSessionsMessage
    ) -> None:
        sessions = [
            r.model_dump(mode="json")
            for r in self._registry.list_sessions()
        ]
        await conn.outbound.put(
            SessionListPush(sessions=sessions).model_dump(mode="json")
        )

    async def _on_attach(self, conn: _ClientConn, msg: AttachMessage) -> None:
        if msg.session_id is None:
            await conn.outbound.put(
                ErrorPush(
                    code="invalid_attach",
                    detail="attach requires session_id; use new_session to mint a session",
                ).model_dump(mode="json")
            )
            return
        record = self._registry.get_session(msg.session_id)
        if record is None:
            await conn.outbound.put(
                ErrorPush(
                    code="session_not_found",
                    detail=msg.session_id,
                ).model_dump(mode="json")
            )
            return
        conn.mode = msg.mode
        self._sessions_attached.setdefault(record.session_id, set()).add(
            conn.writer_id
        )
        conn.sessions.add(record.session_id)
        if record.status == "detached":
            self._registry.update_session(record.session_id, status="active")
            record = record.model_copy(update={"status": "active"})
        self._refresh_active_sessions()

        reader = TranscriptReader(record.session_id)
        replay: list[dict[str, Any]] = []
        last_offset = msg.from_offset
        for event, end_offset in reader.read_from(msg.from_offset):
            replay.append(event.model_dump(mode="json"))
            last_offset = end_offset

        await conn.outbound.put(
            AttachedPush(
                session=record.model_dump(mode="json"),
                replay_events=replay,
                end_offset=last_offset,
            ).model_dump(mode="json")
        )

    async def _on_user_input(
        self, conn: _ClientConn, msg: UserInputMessage
    ) -> None:
        record = self._registry.get_session(msg.session_id)
        if record is None:
            await conn.outbound.put(
                ErrorPush(
                    code="session_not_found", detail=msg.session_id
                ).model_dump(mode="json")
            )
            return
        event = UserTextEvent(
            session_id=record.session_id,
            origin="chat",
            text=msg.text,
        )
        await self.append_event(record.session_id, event)
        await conn.outbound.put(
            AckPush(msg="user_input", session_id=record.session_id).model_dump(
                mode="json"
            )
        )

        if self._dispatch_turn is not None:
            task = asyncio.create_task(
                self._run_dispatch_turn(record, msg.text),
                name=f"controller-turn-{record.session_id}",
            )
            self._inflight_turns.add(task)
            task.add_done_callback(self._inflight_turns.discard)

    async def _run_dispatch_turn(
        self, record: ControllerSessionRecord, text: str
    ) -> None:
        try:
            assert self._dispatch_turn is not None
            await self._dispatch_turn(record, text, self)
        except Exception as exc:  # noqa: BLE001 — surface as transcript event
            log.exception("controller: dispatch_turn failed: %s", exc)
            try:
                await self.append_event(
                    record.session_id,
                    AssistantTextEvent(
                        session_id=record.session_id,
                        origin="chat",
                        text=f"[controller turn failed: {exc}]",
                    ),
                )
            except Exception:  # noqa: BLE001
                log.debug(
                    "controller: failed to append turn-failure event",
                    exc_info=True,
                )

    async def _on_approval(
        self, conn: _ClientConn, msg: ApprovalMessage
    ) -> None:
        """Resolve a pending ASK only when the sender is an interactive
        client attached to ``msg.session_id``.

        Without this check, any authenticated client (an observer WS, a
        client attached to a different session, an autonomy probe) can
        approve a tool gate intended for a different operator. Token
        authentication is fan-wide; per-session authorization has to
        happen here.
        """
        attached_sessions = conn.sessions
        authorized = (
            conn.mode == "interactive"
            and msg.session_id in attached_sessions
        )
        if not authorized:
            log.warning(
                "controller: unauthorized approval rejected "
                "(writer=%s mode=%s session=%s tool_use_id=%s)",
                conn.writer_id, conn.mode, msg.session_id, msg.tool_use_id,
            )
            await conn.outbound.put(
                ErrorPush(
                    code="unauthorized_approval",
                    detail=(
                        "approvals require an interactive attachment to "
                        "the target session"
                    ),
                ).model_dump(mode="json")
            )
            return

        key = (msg.session_id, msg.tool_use_id)
        fut = self._pending_approvals.get(key)
        if fut is not None and not fut.done():
            fut.set_result(bool(msg.approved))
        await conn.outbound.put(
            AckPush(msg="approval", session_id=msg.session_id).model_dump(
                mode="json"
            )
        )

    async def _on_cancel_worker(
        self, conn: _ClientConn, msg: CancelWorkerMessage
    ) -> None:
        ok = False
        if self._cancel_child is not None:
            try:
                ok = await self._cancel_child(msg.session_id, msg.worker_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("controller: cancel_child raised: %s", exc)
                await conn.outbound.put(
                    ErrorPush(
                        code="cancel_failed", detail=str(exc)
                    ).model_dump(mode="json")
                )
                return
        await conn.outbound.put(
            AckPush(msg="cancel_worker", session_id=msg.session_id).model_dump(
                mode="json"
            )
        )
        if not ok:
            await conn.outbound.put(
                ErrorPush(
                    code="cancel_unknown_worker",
                    detail=msg.worker_id,
                ).model_dump(mode="json")
            )

    async def _on_detach(
        self, conn: _ClientConn, msg: DetachMessage
    ) -> None:
        self._detach_from_session(conn.writer_id, msg.session_id)
        await conn.outbound.put(
            AckPush(msg="detach", session_id=msg.session_id).model_dump(
                mode="json"
            )
        )

    # ── delete / rename (2026-05-24) ───────────────────────────────────

    async def _on_delete_session(
        self, conn: _ClientConn, msg: DeleteSessionMessage
    ) -> None:
        """Remove a session's record + transcript. Refuses if any
        client is currently attached — the operator detaches first
        (``/quit`` or ``client.detach()``) and re-issues. Broadcasts
        a :class:`SessionDeletedPush` to every authenticated client so
        picker UIs drop the row.
        """
        session_id = msg.session_id
        if session_id in self._sessions_attached:
            await conn.outbound.put(
                ErrorPush(
                    code="session_attached",
                    detail=(
                        f"session {session_id} is attached; detach first"
                    ),
                ).model_dump(mode="json")
            )
            return
        try:
            existed = self._registry.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "controller: registry delete failed for %s: %s",
                session_id, exc,
            )
            await conn.outbound.put(
                ErrorPush(
                    code="delete_failed", detail=str(exc)
                ).model_dump(mode="json")
            )
            return
        # Drop any cached writer so a future session that re-uses the
        # id (unlikely but possible across daemon restarts) doesn't
        # inherit a stale file handle.
        self._writers.pop(session_id, None)
        # Audit-2 A1 follow-up — evict any in-flight permission futures
        # for this session BEFORE the runtime drop_session hook fires.
        # Without this an operator who deletes a session mid-ASK leaves
        # the awaiting future dangling in ``_pending_approvals`` until
        # its own timeout (default 300s). Mirrors ``stop()``'s drain
        # pattern (cancel-then-pop).
        for key in [
            k for k in self._pending_approvals if k[0] == session_id
        ]:
            fut = self._pending_approvals.pop(key)
            if not fut.done():
                fut.cancel()
        # Option B (2026-07-13) — same drain for the parked-asks VIEW. The
        # future itself is the same object popped above (when the ask was
        # currently parked); this pass just removes the now-orphaned view
        # entry promptly instead of waiting for `_park_and_await`'s own
        # `finally` to notice the cancellation on a later loop iteration.
        for approval_id in [
            aid
            for aid, entry in self._parked_asks.items()
            if entry.session_id == session_id
        ]:
            entry = self._parked_asks.pop(approval_id, None)
            if entry is None:
                continue
            if not entry.future.done():
                entry.future.cancel()
            try:
                await self._broadcast_to_all(
                    ControllerAskSettledPush(
                        approval_id=approval_id,
                        session_id=session_id,
                        tool_use_id=entry.tool_use_id,
                        resolution="cancelled",
                    ).model_dump(mode="json")
                )
            except Exception:  # noqa: BLE001 — best-effort
                log.debug(
                    "controller: settled-push broadcast failed during delete "
                    "(approval_id=%s)",
                    approval_id, exc_info=True,
                )
        # Audit-2 A1 — fire the entry-point's runtime hook so cached
        # ChatSession instances keyed by this session_id get dropped.
        # Best-effort; never block the delete on the callback.
        if self._on_session_deleted is not None:
            try:
                await self._on_session_deleted(session_id)
            except Exception:  # noqa: BLE001
                log.debug(
                    "controller: on_session_deleted callback raised for %s",
                    session_id, exc_info=True,
                )
        push = SessionDeletedPush(session_id=session_id).model_dump(
            mode="json"
        )
        await conn.outbound.put(push)
        await self._broadcast_to_all(push, exclude_writer_id=conn.writer_id)
        if not existed:
            log.debug(
                "controller: delete_session no-op for missing %s",
                session_id,
            )

    async def _on_rename_session(
        self, conn: _ClientConn, msg: RenameSessionMessage
    ) -> None:
        """Update the session's title in the registry and broadcast a
        :class:`SessionRenamedPush` so picker UIs reflect the change.
        """
        try:
            self._registry.update_session(
                msg.session_id,
                title=msg.title,
                touch_last_active=False,
            )
        except KeyError:
            await conn.outbound.put(
                ErrorPush(
                    code="session_not_found", detail=msg.session_id
                ).model_dump(mode="json")
            )
            return
        push = SessionRenamedPush(
            session_id=msg.session_id, title=msg.title
        ).model_dump(mode="json")
        await conn.outbound.put(push)
        await self._broadcast_to_all(push, exclude_writer_id=conn.writer_id)

    # ── shutdown (2026-05-24) ──────────────────────────────────────────

    async def _on_shutdown(
        self, conn: _ClientConn, msg: ShutdownMessage
    ) -> None:
        """Ack the operator's shutdown request, then trip the entry
        point's shutdown event so ``run_controller`` exits its
        ``await stop_event.wait()`` and runs the normal teardown
        (PTY close → server close → port-file unlink → record stamp).
        """
        await conn.outbound.put(
            AckPush(msg="shutdown", session_id=None).model_dump(mode="json")
        )
        log.info(
            "controller: shutdown requested by operator (writer=%s)",
            conn.writer_id,
        )
        self._operator_shutdown_event.set()

    # ── TC-5 reload protocol ───────────────────────────────────────────

    async def _on_reload(
        self, conn: _ClientConn, msg: ReloadMessage
    ) -> None:
        async with self._reload_lock:
            result = await self._drain_and_reload(msg.target)
        push = ReloadCompletePush(
            target=msg.target,
            reloaded=result["reloaded"],
            failed=result["failed"],
            session_count=result["session_count"],
            pending_turns=result["pending_turns"],
            drain_timeout_seconds=self._drain_timeout_seconds,
        ).model_dump(mode="json")
        # Reply on the asking client first, then fan to all attached
        # clients so observer attaches also see the reload happened.
        await conn.outbound.put(push)
        await self._broadcast_to_all(push, exclude_writer_id=conn.writer_id)

    async def _drain_and_reload(self, target: str) -> dict[str, Any]:
        """Pause new turns, mark sessions idle, drain in-flight turns up
        to ``drain_timeout_seconds``, run the reload callback, then mark
        sessions active again. Returns a dict with the counters
        :class:`ReloadCompletePush` consumes."""

        attached_sessions = sorted(self._sessions_attached.keys())
        for session_id in attached_sessions:
            await self._push_session_status(
                session_id, status="idle", reason=f"reload:{target}"
            )
            try:
                self._registry.update_session(session_id, status="idle")
            except KeyError:
                pass

        inflight = [t for t in self._inflight_turns if not t.done()]
        pending_turns = 0
        if inflight:
            # ``asyncio.wait`` (vs ``wait_for(gather)``) is intentional:
            # we want turns to keep running past the drain deadline so a
            # reload doesn't kill long-running advisor work. Sessions
            # whose turn is still active land in ``pending_turns`` on the
            # ``reload_complete`` push.
            done, pending = await asyncio.wait(
                inflight, timeout=self._drain_timeout_seconds
            )
            pending_turns = len(pending)
            if pending_turns:
                log.warning(
                    "controller: reload drain timed out after %.0fs; "
                    "%d turn(s) still running",
                    self._drain_timeout_seconds,
                    pending_turns,
                )

        reloaded: list[str] = []
        failed: list[str] = []
        if self._reload_callback is None:
            reloaded.append(f"{target}: no callback wired (headless)")
        else:
            try:
                cb_result = await self._reload_callback(target)
                if isinstance(cb_result, dict):
                    reloaded.extend(cb_result.get("reloaded") or [])
                    failed.extend(cb_result.get("failed") or [])
                else:
                    reloaded.append(f"{target}: ok")
            except Exception as exc:  # noqa: BLE001 — surface, don't crash
                log.exception(
                    "controller: reload callback failed for target=%s", target
                )
                failed.append(f"{target}: {exc}")

        for session_id in attached_sessions:
            try:
                self._registry.update_session(session_id, status="active")
            except KeyError:
                pass
            await self._push_session_status(
                session_id, status="active", reason="reload_complete"
            )

        return {
            "reloaded": reloaded,
            "failed": failed,
            "session_count": len(attached_sessions),
            "pending_turns": pending_turns,
        }

    async def _push_session_status(
        self, session_id: str, *, status: str, reason: str
    ) -> None:
        push = SessionStatusPush(
            session_id=session_id,
            status=status,  # type: ignore[arg-type]
            reason=reason,
        ).model_dump(mode="json")
        for writer_id in list(
            self._sessions_attached.get(session_id, set())
        ):
            conn = self._clients.get(writer_id)
            if conn is None:
                continue
            self._push_or_disconnect(
                conn, push, source=f"session_status:{session_id}"
            )

    async def _on_activity_snapshot(
        self, conn: _ClientConn, msg: ActivitySnapshotMessage
    ) -> None:
        """AS-1 gap-a — reply to the requesting client with the full
        Activity-registry snapshot so a (re)connecting subscriber reconciles
        mid-flight lanes/sessions at once. Point-to-point, not broadcast: only
        the asking client needs it, and ``activity_event`` pushes already keep
        every other client live."""
        del msg
        from tesseract.orchestrator.activity import get_activity_registry

        records = [r.model_dump(mode="json") for r in get_activity_registry().snapshot()]
        await conn.outbound.put(
            ActivitySnapshotPush(records=records).model_dump(mode="json")
        )

    # ── controller-side ASK parking (Option B, 2026-07-13) ──────────────

    async def _on_parked_asks_snapshot(
        self, conn: _ClientConn, msg: ParkedAsksSnapshotMessage
    ) -> None:
        """Point-to-point reply mirroring ``_on_activity_snapshot`` — a
        (re)connecting Mirror subscriber reconciles the full parked-ask set
        at once instead of waiting for the next park/settle push."""
        del msg
        items = [entry.to_wire() for entry in self._parked_asks.values()]
        await conn.outbound.put(
            ParkedAsksSnapshotPush(items=items).model_dump(mode="json")
        )

    async def _on_decide_parked_ask(
        self, conn: _ClientConn, msg: DecideParkedAskMessage
    ) -> None:
        """Settle a controller-side parked ask.

        Deliberately carries NO attach requirement, unlike ``_on_approval``:
        a parked ask by definition has no attached interactive watcher —
        that is WHY it parked. The deciding authority here is the
        operator-authenticated Mirror route
        (``POST /api/asks/{approval_id}/decision``), which sits behind the
        same token auth as any other authenticated client — equivalent
        trust to an attached TUI client sending ``approval``. Per-session
        attach scoping (the concern ``_on_approval`` guards against — one
        session's client approving a different session's ask) does not
        apply here: the approval_id is a server-minted, globally unique
        key, so there is no cross-session ambiguity to exploit.
        """
        entry = self._parked_asks.get(msg.approval_id)
        if entry is None or entry.future.done():
            await conn.outbound.put(
                ErrorPush(
                    code="unknown_or_settled_parked_ask",
                    detail=msg.approval_id,
                ).model_dump(mode="json")
            )
            return
        entry.future.set_result(bool(msg.approved))
        await conn.outbound.put(
            AckPush(
                msg="decide_parked_ask", session_id=entry.session_id
            ).model_dump(mode="json")
        )
