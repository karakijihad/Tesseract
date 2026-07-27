"""Re-export barrel — session data model, ask-gate closures, and session lifecycle functions."""

from __future__ import annotations

from tesseract.mirror.server.ask_gate import (
    ASK_GRACE_SECONDS,
    ASK_TIMEOUT_SECONDS,
    ChatInfraNotReady,
    _make_ask_fn,
    _make_cli_sink,
    _make_overage_ask_fn,
    _make_status_emit,
    _spawn_handle_id_of_current_task,
)
from tesseract.mirror.server.chat_restore import _restore_persisted_chats
from tesseract.mirror.server.session_cleanup import cleanup_session
from tesseract.mirror.server.session_factory import (
    _build_chat_session,
    _lane_manager_provider,
    _named_lane_manager_provider,
    create_server_session,
    new_chat_session,
)
from tesseract.mirror.server.session_model import (
    MAX_OPEN_CHATS,
    ChatMeta,
    ParkedAsk,
    ServerSession,
    SessionKind,
    _new_chat_meta,
    send_envelope,
)

__all__ = [
    "ASK_GRACE_SECONDS",
    "ASK_TIMEOUT_SECONDS",
    "ChatInfraNotReady",
    "ChatMeta",
    "MAX_OPEN_CHATS",
    "ParkedAsk",
    "ServerSession",
    "SessionKind",
    "_build_chat_session",
    "_lane_manager_provider",
    "_make_ask_fn",
    "_make_cli_sink",
    "_make_overage_ask_fn",
    "_make_status_emit",
    "_named_lane_manager_provider",
    "_new_chat_meta",
    "_restore_persisted_chats",
    "_spawn_handle_id_of_current_task",
    "cleanup_session",
    "create_server_session",
    "new_chat_session",
    "send_envelope",
]
