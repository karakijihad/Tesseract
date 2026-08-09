"""TC-4 — headless agent controller daemon.

A sibling process to the Mirror backend. Owns:

* `<TESSERACT_HOME>/agent_controller/controller.json`  — singleton PID record
* `<TESSERACT_HOME>/agent_controller/<controller_id>/heartbeat` — fresh-touch every 30s
* `<TESSERACT_HOME>/run/controller.port`  — TCP loopback port (kernel-assigned)
* `<TESSERACT_HOME>/run/controller.token` — UUID4 written by supervisor on spawn
* `<TESSERACT_HOME>/agent_controller/sessions/<session_id>.json` — controller-session ledger
* `<TESSERACT_HOME>/agent_controller/transcripts/<session_id>.jsonl` — typed event stream

Transport: asyncio TCP on 127.0.0.1 (delegation daemon precedent). First
message from any client MUST be `{"auth": "<token>"}` — handshake failure
closes the connection. After auth, line-delimited JSON dispatch by `msg`.

Session lifecycle — Contract #8 parity with the delegation daemon. When a
client disconnects, the daemon does NOT kill the session: it transitions
the session to `detached` and keeps it alive so a future client may
re-attach. Only child workers with `parent_kind == "chat"` are killed on
disconnect (mirrors `delegation/daemon.py::_handle_client` Contract #8
behaviour).

ask_fn routing. The chat brain pulled into the daemon by the entry point
calls back through `_ask_fn` whenever a tool needs an operator decision.
The daemon's resolution path:

* if ANY interactive client is attached to the session → send
  `permission_request` over IPC and `await` the response on a Future; the
  client replies with an `approval` message.
* else (no interactive attach) → return `BLOCKED`. The forced-ASK bash
  checks in headless contexts MUST surface as `BLOCKED` per CLAUDE.md
  hard-rule for `bash_security` checks 8/15/17/18/24.

Module-size cleanup (Task 7.5) split this class's responsibilities across
sibling mixin modules — this file keeps the ``_DISPATCH_TABLE`` + connection
loop + the public surface brain handlers call into (``append_event`` /
``request_permission`` / ...). The ``lane.*`` / ``lane_named_*`` handlers
already lived in ``lane_handlers.py`` / ``named_lane_handlers.py``; the
remaining IPC verb handlers moved to ``session_handlers.py`` and the
boot/shutdown/heartbeat lifecycle moved to ``lifecycle.py``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, ClassVar

from pydantic import ValidationError

from tesseract.kernel.sandbox._ipc_frames import decode_frame, encode_frame
from tesseract.orchestrator.workers.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
)

from . import auth as controller_auth
from .events import (
    BaseTranscriptEvent,
    PermissionRequestEvent,
)
from .lane_handlers import _LaneHandlersMixin
from .lifecycle import _LifecycleMixin
from .named_lane_handlers import _NamedLaneHandlersMixin
from .paths import (
    controller_dir,
    controller_record_path,
    heartbeat_path,
    port_file_path,
)
from .protocol import (
    ActivitySnapshotMessage,
    ApprovalMessage,
    AttachMessage,
    AuthMessage,
    CancelWorkerMessage,
    ControllerAskParkedPush,
    ControllerAskSettledPush,
    DecideParkedAskMessage,
    DeleteSessionMessage,
    DetachMessage,
    ErrorPush,
    LaneAttachMessage,
    LaneCloseMessage,
    LaneInterruptMessage,
    LaneListMessage,
    LaneNamedEnsureMessage,
    LaneNamedGetMessage,
    LaneNamedListMessage,
    LaneOpenMessage,
    LaneReadMessage,
    LaneTurnReadMessage,
    LaneResultPush,
    LaneSendMessage,
    LaneStatusMessage,
    ListSessionsMessage,
    NewSessionMessage,
    ParkedAsksSnapshotMessage,
    ReloadMessage,
    RenameSessionMessage,
    ShutdownMessage,
    TranscriptEventPush,
    UnknownClientMessage,
    UserInputMessage,
    parse_client_message,
)
from .session_handlers import _SessionHandlersMixin
from .sessions import ControllerSessionRecord, SessionRegistry
from .transcript import TranscriptWriter

log = logging.getLogger(__name__)


# Audit-2 M6 — per-client outbound queue cap. A slow / abandoned client
# would otherwise accumulate unbounded transcript pushes and grow the
# daemon's memory. The bound is generous because per-turn pushes are
# small JSON dicts; the cap exists to fire at the worst-case "client
# stopped reading" rather than to tune steady-state throughput. When the
# cap trips the daemon detaches the offending client (see
# ``_overflow_disconnect``) so the rest of the session keeps flowing.
_OUTBOUND_QUEUE_MAX = 1000


# ── Public callback types (entry point wires these to chat brain / kernel)


DispatchTurn = Callable[
    [ControllerSessionRecord, str, "ControllerDaemon"],
    Awaitable[None],
]
"""Called when an authenticated client sends `user_input`. The brain handler
appends an `assistant_text` event (and any `tool_use` / `tool_result`
events) via `daemon.append_event(...)`."""


CancelChildWorker = Callable[[str, str], Awaitable[bool]]
"""Called on `cancel_worker`. Returns True if the cancel was honored."""


ReloadCallback = Callable[[str], Awaitable[dict[str, Any]]]
"""TC-5 — invoked after dispatch turns drain. The callback re-reads
``providers.yaml`` / ``roles.yaml`` / ``permissions.yaml`` etc and
rebuilds whatever the daemon's brain wiring needs (adapter, tool
registry, system prompt). Returns ``{"reloaded": [...], "failed":
[...]}``: each entry is a one-line ``"<part>: <detail>"`` for the
operator's toast.
"""


OnSessionDeleted = Callable[[str], Awaitable[None]]
"""Audit-2 A1 — invoked AFTER ``_on_delete_session`` removes the
session record + transcript. Lets the entry point's
:class:`ControllerRuntime` drop its cached ``ChatSession`` for the
deleted id so the in-memory cache doesn't leak entries that no longer
have a backing transcript. Best-effort: failures are logged and
ignored — they cannot block the delete itself.
"""


@dataclass
class _ClientConn:
    """Per-connection state. Mirrors `_Delegation.listeners` in the
    delegation daemon but rooted on the connection, not the session — one
    client can attach to multiple sessions before detaching."""

    writer_id: int
    outbound: asyncio.Queue
    mode: str = "interactive"  # interactive | observer
    sessions: set[str] = field(default_factory=set)
    writer: asyncio.StreamWriter | None = None


@dataclass
class _ParkedControllerAsk:
    """Option B (2026-07-13) — a controller-side ASK that outlived its
    interactive window (or had none) and now parks awaiting the operator,
    mirroring Mirror's own ``ParkedAsk`` (trio W4). ``future`` is the SAME
    object registered in ``_pending_approvals`` — whichever settles it first,
    an ``approval`` message from a newly-attached interactive client or a
    ``decide_parked_ask`` from Mirror, wins; the other is a no-op."""

    approval_id: str
    session_id: str
    tool: str
    summary: str
    tool_use_id: str
    parked_at: str
    future: asyncio.Future = field(repr=False)

    def to_wire(self) -> dict[str, str]:
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "summary": self.summary,
            "tool_use_id": self.tool_use_id,
            "parked_at": self.parked_at,
        }


class ControllerDaemon(
    _LifecycleMixin, _SessionHandlersMixin, _LaneHandlersMixin, _NamedLaneHandlersMixin
):
    """asyncio TCP server with token auth + transcript fan-out.

    Boot/shutdown/heartbeat lives in :class:`_LifecycleMixin` (``lifecycle.py``);
    the ``new_session``/``attach``/``user_input``/... IPC verb handlers live in
    :class:`_SessionHandlersMixin` (``session_handlers.py``); the ``lane.*`` and
    ``lane_named_*`` IPC verb handlers live in :class:`_LaneHandlersMixin` /
    :class:`_NamedLaneHandlersMixin` (``lane_handlers.py`` / ``named_lane_handlers.py``).
    All four mixins call back into the shared ``_push_lane_result`` /
    ``_push_unwired`` / ``_broadcast_to_all`` / ``_push_or_disconnect`` helpers
    defined here.
    """

    def __init__(
        self,
        *,
        controller_id: str,
        token: str,
        registry: SessionRegistry | None = None,
        dispatch_turn: DispatchTurn | None = None,
        cancel_child: CancelChildWorker | None = None,
        reload_callback: ReloadCallback | None = None,
        on_session_deleted: OnSessionDeleted | None = None,
        lane_manager: Any | None = None,
        named_lane_manager: Any | None = None,
        drain_timeout_seconds: float = 30.0,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._controller_id = controller_id
        self._token = token
        self._registry = registry or SessionRegistry()
        self._dispatch_turn = dispatch_turn
        self._cancel_child = cancel_child
        self._reload_callback = reload_callback
        self._on_session_deleted = on_session_deleted
        # X-4 Session C — controller-owned lane manager. The brain doesn't
        # have to be in-process; Mirror / TUI / scheduled jobs can drive
        # lanes by sending the seven `lane.*` IPC messages. None = no
        # lane surface; every lane.* request errors with `lane_manager_unwired`.
        self._lane_manager: Any | None = lane_manager
        # CV-1 — name→lane_id binding layer (NamedLaneManager). Exposed over
        # IPC so Mirror can resolve + spawn the trio's named lanes. None =
        # lane_named_* requests error with `named_lane_manager_unwired`.
        self._named_lane_manager: Any | None = named_lane_manager
        self._drain_timeout_seconds = float(drain_timeout_seconds)
        self._heartbeat_interval = float(heartbeat_interval)

        # 2026-05-24: operator-initiated shutdown via `shutdown` IPC.
        # ``run_controller`` awaits this event alongside the signal-based
        # stop so a TUI Ctrl+C tears the whole daemon down (matches the
        # claude/codex CLI UX). Headless callers never set it; the
        # signal handler path stays the operative one for the supervisor
        # respawn loop.
        self._operator_shutdown_event: asyncio.Event = asyncio.Event()

        self._server: asyncio.AbstractServer | None = None
        self._address: tuple[str, int] = ("", 0)

        self._next_writer_id = 1
        self._clients: dict[int, _ClientConn] = {}
        self._sessions_attached: dict[str, set[int]] = {}
        self._writers: dict[str, TranscriptWriter] = {}

        # pending_approvals[(session_id, tool_use_id)] → Future awaiting the
        # operator's reply via an `approval` IPC message. The chat brain's
        # ask_fn `await`s this future to resolve a tool gate.
        self._pending_approvals: dict[tuple[str, str], asyncio.Future] = {}

        # Option B (2026-07-13) — parked_asks[approval_id] → the SAME future
        # as the matching `_pending_approvals` entry, plus the display
        # fields Mirror's view needs. Populated by `_park_and_await`,
        # popped in its `finally`. A VIEW only — the future stays here,
        # never crosses processes.
        self._parked_asks: dict[str, _ParkedControllerAsk] = {}

        # TC-5 — every active dispatch-turn task. `_run_dispatch_turn`
        # adds itself; the task's `done_callback` removes itself. Reload
        # drains by waiting on this set up to `drain_timeout_seconds`.
        self._inflight_turns: set[asyncio.Task] = set()

        # TC-5 — only one reload may be in flight at a time. The lock
        # prevents two simultaneous Mirror watcher fires (e.g.
        # providers.yaml + roles.yaml changed within debounce window)
        # from interleaving their drains.
        self._reload_lock = asyncio.Lock()

        self._stop_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        # AS-1 — relays the controller's `activity` bus channel to connected
        # Mirror clients. Started in `start`, cancelled in `stop`.
        self._activity_forwarder_task: asyncio.Task | None = None
        self._port_path: Path = port_file_path()
        self._controller_record_path: Path = controller_record_path()
        self._heartbeat_path: Path = heartbeat_path(controller_id)

    # ── lifecycle properties ─────────────────────────────────────────────

    @property
    def controller_id(self) -> str:
        return self._controller_id

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    @property
    def operator_shutdown_event(self) -> asyncio.Event:
        """Awaitable that fires when an authenticated client sends
        ``shutdown``. ``run_controller`` races this against the OS
        signal handler so a TUI :quit / Ctrl+C tears the daemon down."""
        return self._operator_shutdown_event

    # ── public surface for brain handlers ──────────────────────────────

    def writer_for(self, session_id: str) -> TranscriptWriter:
        writer = self._writers.get(session_id)
        if writer is None:
            writer = TranscriptWriter(session_id)
            self._writers[session_id] = writer
        return writer

    async def append_event(
        self, session_id: str, event: BaseTranscriptEvent
    ) -> int:
        """Persist a transcript event and fan it out to attached clients.

        Persistence is sync (TranscriptWriter `fsync`s every line) so the
        on-disk transcript is authoritative even if the IPC fan-out fails.
        Returns the post-write byte offset.
        """
        writer = self.writer_for(session_id)
        end_offset = await asyncio.to_thread(writer.append, event)
        await self._fanout(session_id, event, end_offset)
        try:
            self._registry.update_session(session_id, touch_last_active=True)
        except KeyError:
            log.debug("controller: append_event for missing session %s", session_id)
        return end_offset

    async def request_permission(
        self,
        session_id: str,
        *,
        tool: str,
        summary: str,
        tool_use_id: str,
        posture: str = "ask",
        timeout_seconds: float = 300.0,
    ) -> bool:
        """Await the operator's decision on a tool gate. Returns True if
        approved, False on deny / park_timeout.

        Option B (2026-07-13) — controller-side ASK parking. The future
        NEVER crosses processes; Mirror only ever sees a view + a verb to
        decide it. Resolution path:

        * interactive client attached → wait up to ``timeout_seconds`` for
          an ``approval`` IPC reply on this future.
        * that wait times out (attached-but-silent), OR no interactive
          client is attached at all (was ``headless_blocked``, an
          immediate deny, pre-2026-07-13) → PARK instead: broadcast
          :class:`ControllerAskParkedPush`, keep awaiting the SAME future
          up to ``runtime.yaml::ask_park_timeout_s`` — the operator settles
          it from Mirror's parked-asks pane, or by attaching and sending
          the normal ``approval`` message.
        * the park window itself times out → deny (``park_timeout``) —
          preserves the no-forever-hang property of the old headless path,
          just on a much longer, operator-visible window.

        Every branch appends transcript rows: a ``resolved=False`` pending
        row up front, a ``resolved=False, resolution="parked"`` marker row
        if/when parking kicks in, and a final ``resolved=True`` row with
        the terminal resolution (``approved``/``denied``/``park_timeout``/
        ``cancelled``) — clients fold the set on ``tool_use_id``."""
        base = PermissionRequestEvent(
            session_id=session_id,
            origin="chat",
            tool=tool,
            summary=summary,
            posture=posture,  # type: ignore[arg-type]
            resolved=False,
        )
        base = base.model_copy(update={"tool_use_id": tool_use_id})
        await self.append_event(session_id, base)

        key = (session_id, tool_use_id)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_approvals[key] = fut
        resolution = "park_timeout"
        approved = False
        try:
            if self._interactive_attached(session_id):
                try:
                    # `shield` is load-bearing here: a bare `wait_for(fut,
                    # ...)` cancels `fut` itself when this timer fires — we
                    # need the SAME future to survive into the park phase
                    # below, not a permanently-cancelled one.
                    approved = bool(
                        await asyncio.wait_for(
                            asyncio.shield(fut), timeout=timeout_seconds
                        )
                    )
                    resolution = "approved" if approved else "denied"
                    return approved
                except asyncio.TimeoutError:
                    if fut.done() and not fut.cancelled():
                        # Settle race: a decision landed at the exact
                        # boundary instant — honor it, don't park it.
                        approved = bool(fut.result())
                        resolution = "approved" if approved else "denied"
                        return approved
                    # attached-but-silent — fall through to parking
            parked_marker = base.model_copy(
                update={"resolved": False, "resolution": "parked"}
            )
            await self.append_event(session_id, parked_marker)
            approved, resolution = await self._park_and_await(
                session_id=session_id,
                tool=tool,
                summary=summary,
                tool_use_id=tool_use_id,
                fut=fut,
            )
            return approved
        except asyncio.CancelledError:
            resolution = "cancelled"
            raise
        finally:
            self._pending_approvals.pop(key, None)
            try:
                resolved_event = base.model_copy(
                    update={"resolved": True, "resolution": resolution}
                )
                await self.append_event(session_id, resolved_event)
            except Exception:  # noqa: BLE001 — never let audit-write kill the ask path
                log.debug(
                    "controller: permission_request resolution append failed "
                    "(session=%s tool_use_id=%s)",
                    session_id, tool_use_id, exc_info=True,
                )

    async def _park_and_await(
        self,
        *,
        session_id: str,
        tool: str,
        summary: str,
        tool_use_id: str,
        fut: asyncio.Future,
    ) -> tuple[bool, str]:
        """Option B — register ``fut`` in the parked-asks view, broadcast
        the park, then keep awaiting the SAME future (still also live in
        ``_pending_approvals``) up to ``runtime.yaml::ask_park_timeout_s``.
        Returns ``(approved, resolution)`` with ``resolution`` one of
        ``"approved"``, ``"denied"``, ``"park_timeout"``, ``"cancelled"``."""
        from tesseract.config.runtime_limits import (
            default_runtime_config_path,
            load_ask_park_timeout_s,
        )

        park_timeout_s = load_ask_park_timeout_s(default_runtime_config_path())
        parked_at = datetime.now(timezone.utc).isoformat()
        entry = _ParkedControllerAsk(
            approval_id=uuid.uuid4().hex,
            session_id=session_id,
            tool=tool,
            summary=summary,
            tool_use_id=tool_use_id,
            parked_at=parked_at,
            future=fut,
        )
        self._parked_asks[entry.approval_id] = entry
        result: tuple[bool, str] = (False, "park_timeout")
        try:
            await self._broadcast_to_all(
                ControllerAskParkedPush(
                    approval_id=entry.approval_id,
                    session_id=session_id,
                    tool=tool,
                    summary=summary,
                    tool_use_id=tool_use_id,
                    parked_at=parked_at,
                ).model_dump(mode="json")
            )
            try:
                # `asyncio.shield` is load-bearing: a bare `wait_for(fut, ...)`
                # cancels `fut` itself when the timer fires, leaving it
                # permanently done-cancelled — a `decide_parked_ask` (or a
                # late `approval`) landing right at the boundary would then
                # have nothing to `set_result` on.
                decision = bool(
                    await asyncio.wait_for(
                        asyncio.shield(fut), timeout=park_timeout_s
                    )
                )
                result = (decision, "approved" if decision else "denied")
            except asyncio.TimeoutError:
                if fut.done() and not fut.cancelled():
                    # Settle race: a decision at the boundary instant is
                    # honored, not discarded.
                    decision = bool(fut.result())
                    result = (decision, "approved" if decision else "denied")
                else:
                    if not fut.done():
                        fut.cancel()
                    result = (False, "park_timeout")
            return result
        except asyncio.CancelledError:
            result = (False, "cancelled")
            raise
        finally:
            self._parked_asks.pop(entry.approval_id, None)
            try:
                await self._broadcast_to_all(
                    ControllerAskSettledPush(
                        approval_id=entry.approval_id,
                        session_id=session_id,
                        tool_use_id=tool_use_id,
                        resolution=result[1],
                    ).model_dump(mode="json")
                )
            except Exception:  # noqa: BLE001 — never let broadcast kill the ask path
                log.debug(
                    "controller: controller_ask_settled broadcast failed "
                    "(approval_id=%s)",
                    entry.approval_id, exc_info=True,
                )

    def _interactive_attached(self, session_id: str) -> bool:
        for writer_id in self._sessions_attached.get(session_id, set()):
            conn = self._clients.get(writer_id)
            if conn is not None and conn.mode == "interactive":
                return True
        return False

    async def _fanout(
        self, session_id: str, event: BaseTranscriptEvent, end_offset: int
    ) -> None:
        push = TranscriptEventPush(
            session_id=session_id,
            transcript_event=event.model_dump(mode="json"),
            end_offset=end_offset,
        ).model_dump(mode="json")
        for writer_id in list(self._sessions_attached.get(session_id, set())):
            conn = self._clients.get(writer_id)
            if conn is None:
                continue
            self._push_or_disconnect(conn, push, source=f"fanout:{session_id}")

    def _push_or_disconnect(
        self, conn: _ClientConn, push: dict[str, Any], *, source: str
    ) -> None:
        """Audit-2 M6 — enqueue ``push`` or detach the client on overflow.

        With a bounded outbound queue, ``put_nowait`` raises
        :class:`asyncio.QueueFull` when a client stops reading. The
        recovery path is to tear the client down (sentinel ``None``
        wakes the writer task; the connection loop's ``finally`` drops
        attachments) so the rest of the session keeps fanning out. The
        sentinel is best-effort: if the queue is already past full we
        also try to drain a slot before dropping the connection."""
        try:
            conn.outbound.put_nowait(push)
            return
        except asyncio.QueueFull:
            pass
        log.warning(
            "controller: outbound queue full for writer=%s (%s) — detaching",
            conn.writer_id,
            source,
        )
        # Audit-2 R1 follow-up — log what got dropped so an operator
        # investigating "why did my client miss event X" has a breadcrumb
        # rather than a silent gap.
        try:
            dropped = conn.outbound.get_nowait()
            dropped_event = (
                dropped.get("event")
                if isinstance(dropped, dict)
                else type(dropped).__name__
            )
            log.warning(
                "controller: dropped push event=%s writer=%s (%s) to make "
                "room for writer-stop sentinel",
                dropped_event,
                conn.writer_id,
                source,
            )
        except asyncio.QueueEmpty:
            pass
        try:
            conn.outbound.put_nowait(None)
        except asyncio.QueueFull:
            log.debug(
                "controller: sentinel push lost for writer=%s; client_writer "
                "will drain on close",
                conn.writer_id,
            )

    async def _broadcast_to_all(
        self, push: dict[str, Any], *, exclude_writer_id: int | None = None
    ) -> None:
        for writer_id, conn in list(self._clients.items()):
            if writer_id == exclude_writer_id:
                continue
            self._push_or_disconnect(conn, push, source="broadcast")

    # ── connection loop ───────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer_id = self._next_writer_id
        self._next_writer_id += 1
        outbound: asyncio.Queue = asyncio.Queue(maxsize=_OUTBOUND_QUEUE_MAX)
        conn = _ClientConn(writer_id=writer_id, outbound=outbound, writer=writer)
        self._clients[writer_id] = conn
        writer_task = asyncio.create_task(self._client_writer(writer, outbound))
        authed = False
        try:
            while True:
                try:
                    payload = await decode_frame(reader)
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                except ValueError as exc:  # oversize / malformed frame
                    await outbound.put(
                        ErrorPush(
                            code="oversize_request", detail=str(exc)
                        ).model_dump(mode="json")
                    )
                    return
                if not authed:
                    try:
                        msg = AuthMessage.model_validate(payload)
                    except ValidationError:
                        await outbound.put(
                            ErrorPush(
                                code="auth_required",
                                detail="first message must be {auth: <token>}",
                            ).model_dump(mode="json")
                        )
                        return
                    if not controller_auth.verify_token(msg.auth, self._token):
                        await outbound.put(
                            ErrorPush(
                                code="auth_failed",
                                detail="invalid token",
                            ).model_dump(mode="json")
                        )
                        return
                    authed = True
                    continue

                try:
                    parsed = parse_client_message(payload)
                except (ValidationError, UnknownClientMessage) as exc:
                    await outbound.put(
                        ErrorPush(
                            code="invalid_message", detail=str(exc)
                        ).model_dump(mode="json")
                    )
                    continue

                try:
                    await self._dispatch(conn, parsed)
                except Exception as exc:  # noqa: BLE001 — never let one client kill the daemon
                    log.exception("controller: dispatch error: %s", exc)
                    await outbound.put(
                        ErrorPush(
                            code="dispatch_error", detail=str(exc)
                        ).model_dump(mode="json")
                    )
        finally:
            for sid in list(conn.sessions):
                self._detach_from_session(writer_id, sid)
            await outbound.put(None)
            try:
                await writer_task
            except Exception:  # noqa: BLE001
                pass
            self._clients.pop(writer_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _client_writer(
        self, writer: asyncio.StreamWriter, outbound: asyncio.Queue
    ) -> None:
        while True:
            item = await outbound.get()
            if item is None:
                return
            try:
                writer.write(encode_frame(item))
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                return

    # ── dispatch ──────────────────────────────────────────────────────

    # Message type → handler-method name. `_dispatch` looks the handler up
    # by exact type (the parsed message is always one concrete protocol
    # type — no inheritance among them, so an exact-type map is equivalent
    # to the former isinstance chain). Handler methods may live on this
    # class or on a mixin; `getattr(self, name)` resolves via the MRO.
    _DISPATCH_TABLE: ClassVar[dict[type, str]] = {
        AttachMessage: "_on_attach",
        NewSessionMessage: "_on_new_session",
        ListSessionsMessage: "_on_list_sessions",
        UserInputMessage: "_on_user_input",
        ApprovalMessage: "_on_approval",
        CancelWorkerMessage: "_on_cancel_worker",
        DetachMessage: "_on_detach",
        DeleteSessionMessage: "_on_delete_session",
        RenameSessionMessage: "_on_rename_session",
        ReloadMessage: "_on_reload",
        ShutdownMessage: "_on_shutdown",
        LaneOpenMessage: "_on_lane_open",
        LaneSendMessage: "_on_lane_send",
        LaneReadMessage: "_on_lane_read",
        LaneTurnReadMessage: "_on_lane_turn_read",
        LaneStatusMessage: "_on_lane_status",
        LaneAttachMessage: "_on_lane_attach",
        LaneCloseMessage: "_on_lane_close",
        LaneInterruptMessage: "_on_lane_interrupt",
        LaneListMessage: "_on_lane_list",
        LaneNamedEnsureMessage: "_on_lane_named_ensure",
        LaneNamedGetMessage: "_on_lane_named_get",
        LaneNamedListMessage: "_on_lane_named_list",
        ActivitySnapshotMessage: "_on_activity_snapshot",
        ParkedAsksSnapshotMessage: "_on_parked_asks_snapshot",
        DecideParkedAskMessage: "_on_decide_parked_ask",
    }

    async def _dispatch(self, conn: _ClientConn, msg: Any) -> None:
        handler_name = self._DISPATCH_TABLE.get(type(msg))
        if handler_name is None:  # pragma: no cover — parse_client_message would have raised
            await conn.outbound.put(
                ErrorPush(
                    code="unsupported_message",
                    detail=f"unknown message type: {type(msg).__name__}",
                ).model_dump(mode="json")
            )
            return
        await getattr(self, handler_name)(conn, msg)

    # ── lane.* result helpers (X-4 Session C) ───────────────────────────

    async def _push_lane_result(
        self,
        conn: _ClientConn,
        *,
        request_id: str,
        verb: str,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        await conn.outbound.put(
            LaneResultPush(
                request_id=request_id,
                verb=verb,  # type: ignore[arg-type]
                ok=ok,
                result=result or {},
                error=error,
            ).model_dump(mode="json")
        )

    async def _push_unwired(
        self,
        conn: _ClientConn,
        request_id: str,
        verb: str,
        *,
        error: str = "lane_manager_unwired",
    ) -> None:
        """Emit the `ok=False` result a lane/named-lane verb returns when its
        manager isn't wired. `error` distinguishes the two managers
        (`lane_manager_unwired` vs `named_lane_manager_unwired`)."""
        await self._push_lane_result(
            conn,
            request_id=request_id,
            verb=verb,
            ok=False,
            error=error,
        )

    # The lane.* and lane_named_* verb handlers are provided by
    # _LaneHandlersMixin / _NamedLaneHandlersMixin (class bases). They call
    # back into _push_lane_result / _push_unwired (above) via the MRO.

    def _detach_from_session(self, writer_id: int, session_id: str) -> None:
        attached = self._sessions_attached.get(session_id)
        if attached is not None:
            attached.discard(writer_id)
            if not attached:
                self._sessions_attached.pop(session_id, None)
                try:
                    self._registry.update_session(
                        session_id, status="detached"
                    )
                except KeyError:
                    pass
        conn = self._clients.get(writer_id)
        if conn is not None:
            conn.sessions.discard(session_id)
        try:
            self._refresh_active_sessions()
        except Exception:  # noqa: BLE001 — best-effort
            log.debug("controller: refresh_active_sessions failed", exc_info=True)


__all__ = [
    "CancelChildWorker",
    "ControllerDaemon",
    "DispatchTurn",
]
