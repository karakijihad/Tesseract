"""2026-05-17 — ASK → Telegram round-trip.

When the channel gate fires on a Telegram-originated turn, the bridge:
1. appends a `tars_post` workspace event (existing behavior),
2. broadcasts it to attached Mirror sessions (existing),
3. ALSO pushes an inline-keyboard prompt to the operator's Telegram
   thread so the ASK is actionable from the phone (new).

Tapping the keyboard's "✓ Approve" button arrives as an `update.callback_query`
which the bridge handles: records the approval token on the chat's
ServerSession (same path the Mirror UI uses) and edits the original
prompt to show "Approved".

Operator complaint that motivated this (turn 17:13 svg_to_png event):
*"i was asking thru telegram, i did not recieve any ask"* — the gate
only posted to the Mirror inbox, invisible to the operator on Telegram.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tesseract.integrations._retention import RetentionPolicy
from tesseract.integrations.telegram.bridge import TelegramBridge
from tesseract.integrations.telegram.state import PollState


def _new_bridge(tmp_path, monkeypatch) -> TelegramBridge:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge._token = "fake"
    bridge._app = MagicMock()
    bridge._app.get = lambda k, default=None: default
    bridge._conversations = MagicMock()
    bridge._sessions = {}
    bridge._retention = RetentionPolicy.fallback()
    bridge._poll_task = None
    bridge._stop_event = asyncio.Event()
    bridge._started_at = ""
    bridge._last_poll_at = None
    bridge._error_count = 0
    bridge._bridge_phase = "stopped"
    bridge._last_getme_error = None
    bridge._chat_locks = {}
    bridge._inflight_handlers = set()
    bridge._pending_approval_messages = {}
    bridge._chat_memory = None
    bridge._api = MagicMock()
    bridge._api.send_message = AsyncMock(return_value={"message_id": 555})
    bridge._api.answer_callback_query = AsyncMock()
    bridge._api.edit_message_reply_markup = AsyncMock()
    bridge._api.edit_message_text = AsyncMock()
    state_bundle = MagicMock()
    state_bundle.poll_state = PollState()
    state_bundle.poll_state.user_tier["100"] = "operator"
    bridge._state = state_bundle
    object.__setattr__(bridge, "name", "telegram")
    return bridge


def _event(channel: str, chat_id: str, tool: str = "agent_promote", *, event_id="evt_abc") -> Any:
    ev = MagicMock()
    ev.event_id = event_id
    ev.kind = "tars_post"
    ev.payload = {
        "channel": channel,
        "chat_id": chat_id,
        "tool": tool,
        "args_hash": "abc123def456",
        "reason": "moves a quarantined agent into the active set",
    }
    return ev


@pytest.mark.asyncio
async def test_broadcast_pushes_keyboard_for_own_channel(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    await bridge._send_approval_prompt(_event("telegram", "100"))

    bridge._api.send_message.assert_awaited_once()
    call = bridge._api.send_message.await_args
    assert call.kwargs["chat_id"] == 100
    markup = call.kwargs["reply_markup"]
    rows = markup["inline_keyboard"]
    assert len(rows) == 1 and len(rows[0]) == 2
    approve = rows[0][0]
    reject = rows[0][1]
    assert "Approve" in approve["text"]
    assert "Reject" in reject["text"]
    assert approve["callback_data"] == "g:evt_abc:a"
    assert reject["callback_data"] == "g:evt_abc:r"
    # Pending registry has the row so the callback handler can pair them
    assert "evt_abc" in bridge._pending_approval_messages
    pending = bridge._pending_approval_messages["evt_abc"]
    assert pending["chat_id"] == 100
    assert pending["message_id"] == 555
    assert pending["args_hash"] == "abc123def456"


@pytest.mark.asyncio
async def test_broadcaster_ignores_other_channels(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    broadcaster = bridge._build_workspace_broadcaster()
    # Stub the workspace_event broadcast to not crash on the missing module
    # by routing the event with a non-matching channel — only the second
    # half (send_approval_prompt) is what we're guarding against.
    await broadcaster(_event("signal", "100"))
    bridge._api.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_callback_approve_records_token_and_edits_message(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    from types import SimpleNamespace
    session = SimpleNamespace(session_id="telegram_100_xx")
    bridge._sessions[100] = session
    bridge._pending_approval_messages["evt_abc"] = {
        "chat_id": 100, "message_id": 555,
        "args_hash": "abc123def456", "tool_name": "agent_promote",
    }

    callback = {
        "id": "cb1",
        "from": {"id": 100, "username": "operator"},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "g:evt_abc:a",
    }
    await bridge._handle_callback_query(callback)

    # The approval token landed on the session.
    from tesseract.integrations._channel_gate import consume_approval
    assert consume_approval(session, tool_name="agent_promote", args={}) is False
    # Direct hash lookup confirms the token was stored under the right hash
    approvals = getattr(session, "_channel_gate_pending_approvals", {})
    assert "abc123def456" in approvals
    # Original prompt edited with the "approved" suffix.
    bridge._api.edit_message_text.assert_awaited_once()
    edit_kw = bridge._api.edit_message_text.await_args.kwargs
    assert "Approved" in edit_kw["text"]
    # Spinner dismissed.
    bridge._api.answer_callback_query.assert_awaited_once()
    # Pending entry removed (consumed).
    assert "evt_abc" not in bridge._pending_approval_messages


@pytest.mark.asyncio
async def test_callback_reject_edits_message(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    from types import SimpleNamespace
    session = SimpleNamespace(session_id="telegram_100_xx")
    bridge._sessions[100] = session
    bridge._pending_approval_messages["evt_abc"] = {
        "chat_id": 100, "message_id": 555,
        "args_hash": "abc", "tool_name": "agent_promote",
    }

    callback = {
        "id": "cb2",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "g:evt_abc:r",
    }
    await bridge._handle_callback_query(callback)

    # No approval token stored on reject.
    approvals = getattr(session, "_channel_gate_pending_approvals", {})
    assert "abc" not in approvals
    # Message edited with rejected suffix.
    edit_kw = bridge._api.edit_message_text.await_args.kwargs
    assert "Rejected" in edit_kw["text"]
    bridge._api.answer_callback_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_non_operator_tier_denied(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    bridge._state.poll_state.user_tier["100"] = "friend"
    bridge._pending_approval_messages["evt_abc"] = {
        "chat_id": 100, "message_id": 555, "args_hash": "abc", "tool_name": "t",
    }
    await bridge._handle_callback_query({
        "id": "cb3",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "g:evt_abc:a",
    })
    # Pending entry untouched — friend cannot approve.
    assert "evt_abc" in bridge._pending_approval_messages
    bridge._api.edit_message_text.assert_not_called()
    # Spinner dismissed with the auth error.
    bridge._api.answer_callback_query.assert_awaited_once()
    msg = bridge._api.answer_callback_query.await_args.kwargs.get("text", "")
    assert "authorized" in msg.lower() or "auth" in msg.lower()


@pytest.mark.asyncio
async def test_callback_unknown_event_id_returns_expired(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    await bridge._handle_callback_query({
        "id": "cb4",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "g:unknown_evt:a",
    })
    bridge._api.answer_callback_query.assert_awaited_once()
    msg = bridge._api.answer_callback_query.await_args.kwargs.get("text", "")
    assert "expired" in msg.lower()
    # Keyboard stripped on the original message.
    bridge._api.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_recovers_from_workspace_event_store_after_restart(
    tmp_path, monkeypatch,
) -> None:
    """2026-05-19 — Mirror-backend restart between gate-post and Telegram
    tap voids ``_pending_approval_messages`` (in-memory). The bridge
    now recovers ``args_hash`` from the persisted workspace event so
    the operator's tap still honors the approval."""
    bridge = _new_bridge(tmp_path, monkeypatch)
    from types import SimpleNamespace
    session = SimpleNamespace(session_id="telegram_100_xx")
    bridge._sessions[100] = session

    # In-memory map empty (restart wiped it).
    assert bridge._pending_approval_messages == {}

    # workspace_event_store still carries the pending event.
    persisted_event = MagicMock()
    persisted_event.event_id = "evt_abc"
    persisted_event.kind = "tars_post"
    persisted_event.status = "pending"
    persisted_event.payload = {
        "channel": "telegram",
        "chat_id": "100",
        "tool": "agent_promote",
        "args_hash": "abc123def456",
    }
    store = MagicMock()
    store.get_event = MagicMock(return_value=persisted_event)
    store.update_event_status = MagicMock()
    bridge._app.get = lambda k, default=None: store if k == "workspace_event_store" else default

    callback = {
        "id": "cb_recover",
        "from": {"id": 100, "username": "operator"},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "g:evt_abc:a",
    }
    await bridge._handle_callback_query(callback)

    # Approval landed on the session despite the in-memory miss.
    approvals = getattr(session, "_channel_gate_pending_approvals", {})
    assert "abc123def456" in approvals
    # Event status flipped to approved in the persisted store.
    store.update_event_status.assert_called_once()
    call_args = store.update_event_status.call_args
    assert call_args.args[0] == "evt_abc"
    assert call_args.args[1] == "approved"
    # Spinner dismissed with a positive answer, not "expired".
    bridge._api.answer_callback_query.assert_awaited_once()
    answer_text = bridge._api.answer_callback_query.await_args.kwargs.get("text", "")
    assert "expired" not in answer_text.lower()
    assert answer_text.lower().startswith("approved")


@pytest.mark.asyncio
async def test_callback_recovery_skips_already_decided_event(
    tmp_path, monkeypatch,
) -> None:
    """If the workspace event has already been approved/rejected/deleted,
    the recovery path must NOT re-flip the token — the operator already
    decided via Mirror or a prior tap."""
    bridge = _new_bridge(tmp_path, monkeypatch)
    from types import SimpleNamespace
    session = SimpleNamespace(session_id="telegram_100_xx")
    bridge._sessions[100] = session

    persisted_event = MagicMock()
    persisted_event.event_id = "evt_abc"
    persisted_event.kind = "tars_post"
    persisted_event.status = "approved"  # already decided
    persisted_event.payload = {
        "channel": "telegram",
        "chat_id": "100",
        "tool": "agent_promote",
        "args_hash": "abc123def456",
    }
    store = MagicMock()
    store.get_event = MagicMock(return_value=persisted_event)
    bridge._app.get = lambda k, default=None: store if k == "workspace_event_store" else default

    await bridge._handle_callback_query({
        "id": "cb_already",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "g:evt_abc:a",
    })

    # No token recorded — the decision is locked.
    approvals = getattr(session, "_channel_gate_pending_approvals", {})
    assert "abc123def456" not in approvals
    # Operator told the prompt expired.
    msg = bridge._api.answer_callback_query.await_args.kwargs.get("text", "")
    assert "expired" in msg.lower()


@pytest.mark.asyncio
async def test_callback_malformed_data_rejected(tmp_path, monkeypatch) -> None:
    bridge = _new_bridge(tmp_path, monkeypatch)
    await bridge._handle_callback_query({
        "id": "cb5",
        "from": {"id": 100},
        "message": {"chat": {"id": 100}, "message_id": 555},
        "data": "garbage",
    })
    bridge._api.answer_callback_query.assert_awaited_once()
    # No state mutated, no edit fired.
    bridge._api.edit_message_text.assert_not_called()
    bridge._api.edit_message_reply_markup.assert_not_called()
