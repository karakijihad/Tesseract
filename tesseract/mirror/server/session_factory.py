"""Server session factory — WS session creation, chat-session construction, lane providers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from aiohttp import web

from tesseract.brain.boot import build_fallback_adapter
from tesseract.brain.chat import ChatSession
from tesseract.brain.chat_session_factory import ChatSessionWiring, build_chat_session
from tesseract.brain.tools import AskFn
from tesseract.kernel.tools.base import CliSink
from tesseract.mirror.server.ask_gate import (
    ChatInfraNotReady,
    _make_ask_fn,
    _make_cli_sink,
    _make_overage_ask_fn,
    _make_status_emit,
)
from tesseract.mirror.server.chat_restore import _restore_persisted_chats
from tesseract.mirror.server.event_log import EventLog
from tesseract.mirror.server.session_model import ParkedAsk, ServerSession, SessionKind
from tesseract.orchestrator.agent_controller.lanes.ipc_proxy import (
    IpcLaneManager,
    IpcNamedLaneManager,
)
from tesseract.orchestrator.agent_controller.lanes.principals import (
    OPERATOR_PRINCIPAL,
)

log = logging.getLogger(__name__)


def _lane_manager_provider() -> IpcLaneManager:
    """Mirror chat brain drives controller-owned lanes over IPC (conductor).

    the assistant's own turn IS the operator's work — there is no MCP client between
    them — so it names the operator principal outright. Naming it rather than
    letting it default is the point: the daemon refuses an unattested lane
    message, so every real caller has to say who it is."""
    return IpcLaneManager(caller_principal=OPERATOR_PRINCIPAL)


def _named_lane_manager_provider() -> IpcNamedLaneManager:
    """Mirror chat brain drives controller-owned named lanes over IPC (conductor)."""
    return IpcNamedLaneManager(caller_principal=OPERATOR_PRINCIPAL)


def create_server_session(app: web.Application, ws: web.WebSocketResponse) -> ServerSession:
    session_id = uuid.uuid4().hex
    event_log = EventLog()
    # NB: app-dict registration happens AFTER `_build_chat_session` so a boot-race
    # `ChatInfraNotReady` raise leaves no orphaned entries (the ask/sink/status
    # closures below capture the local `event_log`, not the dict slot).

    pending_asks: dict[str, asyncio.Future[bool]] = {}
    pending_overage_asks: dict[str, asyncio.Future[bool]] = {}
    # trio W4 — parked asks live at APP level (shared dict; ParkedAsk carries
    # its session_id): a parked spawn deliberately survives WS disconnects
    # and reconnects, so its entry must outlive this ServerSession.
    parked_asks: dict[str, ParkedAsk] = app.setdefault("parked_asks", {})
    ask_fn = _make_ask_fn(ws, session_id, pending_asks, event_log, parked_asks)
    overage_ask_fn = _make_overage_ask_fn(ws, session_id, pending_overage_asks, event_log)
    cli_sink = _make_cli_sink(ws, session_id, event_log)
    status_emit = _make_status_emit(ws, session_id, event_log)
    chat_session = _build_chat_session(app, session_id, ask_fn, cli_sink, overage_ask_fn, status_emit)

    now = datetime.now(timezone.utc)
    server_session = ServerSession(
        session_id=session_id,
        ws=ws,
        chat_session=chat_session,
        event_log=event_log,
        pending_asks=pending_asks,
        parked_asks=parked_asks,
        pending_overage_asks=pending_overage_asks,
        started_at=now.isoformat(),
        last_turn_at=now,
    )
    app["event_logs"][session_id] = event_log
    app["sessions"][session_id] = ws
    app["server_sessions"][session_id] = server_session
    # P3 — rebuild the open-chat registry from disk so a page reload restores
    # the operator's tabs. Best-effort: a failure leaves the fresh single seed.
    try:
        _restore_persisted_chats(app, server_session)
    except Exception:
        log.exception("chat restore failed for %s; using fresh seed", session_id)
    return server_session


def new_chat_session(
    app: web.Application,
    session: ServerSession,
    *,
    kind: SessionKind = "cockpit",
) -> ChatSession:
    """Build an additional ``ChatSession`` for a live ``ServerSession``.

    The chat.create WS handler uses this to add a chat to an already-connected
    session. ``create_server_session`` builds the FIRST chat inline (before the
    ServerSession exists, so its closures capture local vars); this rebuilds
    the same per-session closures from the live session's fields so every chat
    shares one WS / ask-gate / cli-sink. Raises ``ChatInfraNotReady`` if the
    backend is still booting (same as the first chat).
    """
    ask_fn = _make_ask_fn(
        session.ws, session.session_id, session.pending_asks, session.event_log,
        session.parked_asks,
    )
    overage_ask_fn = _make_overage_ask_fn(
        session.ws, session.session_id, session.pending_overage_asks, session.event_log,
    )
    cli_sink = _make_cli_sink(session.ws, session.session_id, session.event_log)
    status_emit = _make_status_emit(session.ws, session.session_id, session.event_log)
    return _build_chat_session(
        app, session.session_id, ask_fn, cli_sink, overage_ask_fn, status_emit, kind=kind,
    )


def _build_chat_session(
    app: web.Application,
    session_id: str,
    ask_fn: AskFn,
    cli_sink: CliSink,
    overage_ask_fn,
    status_emit=None,
    *,
    kind: SessionKind = "cockpit",
    channel_display_name: str | None = None,
) -> ChatSession:
    # CR-3 — channel sessions get a session-specific ``prompt_builder``
    # that re-assembles the system prompt with the channel overlay inlined
    # *inside* the manifest (before the per-turn "Right now" section). The
    # cockpit path keeps the global ``app["prompt_builder"]`` (no overlay).
    if kind == "channel":
        from tesseract.brain.prompt import assemble_system_prompt
        def _channel_prompt_builder() -> str:
            return assemble_system_prompt(
                channel_name=channel_display_name,
                tool_registry_provider=lambda: app.get("tool_registry"),
            )
        prompt_builder = _channel_prompt_builder
        frozen_prompt = _channel_prompt_builder()
    else:
        prompt_builder = app.get("prompt_builder")
        frozen_prompt = app["system_prompt"]
    chat_cfg = app["adapter_entry"]
    if chat_cfg is None:
        # Boot race — chat infra not built yet. Fail with a clear, catchable
        # signal instead of an AttributeError on `chat_cfg.tool_iteration_cap`.
        raise ChatInfraNotReady(
            "chat infra not ready (adapter_entry is None) — backend still booting"
        )
    pty_manager = app.get("pty_manager")
    pty_dispatcher = pty_manager.dispatch_for_agent if pty_manager is not None else None
    # Audit M3 fix (2026-04-29): wrap the chat_brain chain in
    # FallbackAdapter so a primary failure mid-turn transparently rolls
    # over to the next configured provider. Before, ChatSession only
    # received `app["adapter"]` (the primary) and `app["adapter_chain"]`
    # was stashed but never consumed. Falls back to the bare adapter when
    # the chain is empty (defensive — boot guarantees at least one entry).
    chain = app.get("adapter_chain") or []
    if chain:
        live_adapter = build_fallback_adapter(chain)
    else:
        live_adapter = app["adapter"]
    wiring = ChatSessionWiring(
        adapter=live_adapter,
        system_prompt=frozen_prompt,
        max_tool_iterations=chat_cfg.tool_iteration_cap,
        max_consecutive_adapter_errors=chat_cfg.consecutive_error_cap,
        workspace_root=str(app["repo_root"]),
        session_id=session_id,
        cli_sink=cli_sink,
        scheduler_provider=lambda: app.get("scheduler"),
        tool_registry_provider=lambda: app.get("tool_registry"),
        lane_manager_provider=_lane_manager_provider,
        named_lane_manager_provider=_named_lane_manager_provider,
        ask_fn=ask_fn,
        policy=app["config"].permissions,
        prompt_builder=prompt_builder,
        registry=app["tool_registry"],
        # trio W3 — root chat sessions sit at depth 0; the cap rides the
        # context so sub-agent sessions inherit it via agent_factory.
        spawn_depth_cap=app.get("max_spawn_depth"),
        pty_dispatcher=pty_dispatcher,
        status_emit=status_emit,
        options=app["adapter_options"],
        compact_threshold=chat_cfg.compact_threshold,
        keep_recent_turns=chat_cfg.keep_recent_turns,
        cost_ledger=app.get("cost_ledger"),
        overage_ask_fn=overage_ask_fn,
        session_kind=kind,
        channel_display_name=channel_display_name,
        spawn_stall_seconds=app.get("spawn_stall_seconds"),
        spawn_max_concurrent=app.get("max_concurrent_spawns_per_session"),
    )
    return build_chat_session(wiring)
