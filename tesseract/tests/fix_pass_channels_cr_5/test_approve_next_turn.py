"""CR-5 — operator approves a gated event; the next channel turn
auto-passes the matching ASK without re-emitting a workspace event.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from pydantic import BaseModel

from tesseract.integrations._channel_gate import (
    build_channel_ask_fn,
    consume_approval,
    record_approval,
    reset_per_turn_state,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.workspace_events.events import EventStore


class _Args(BaseModel):
    query: str


class _Tool:
    name = "web_search"


@dataclass
class _S:
    session_id: str = "tg_99_abc"
    chat_session: Any = field(default_factory=MagicMock)


def test_record_then_consume_approval_round_trip():
    session = _S()
    h = record_approval(session, tool_name="web_search", args={"q": 1}, ttl_s=60)
    assert isinstance(h, str) and h
    assert consume_approval(session, tool_name="web_search", args={"q": 1}) is True
    # Single-shot: consumed approval does not fire twice.
    assert consume_approval(session, tool_name="web_search", args={"q": 1}) is False


def test_consume_approval_rejects_mismatched_args():
    session = _S()
    record_approval(session, tool_name="web_search", args={"q": 1}, ttl_s=60)
    assert consume_approval(session, tool_name="web_search", args={"q": 2}) is False
    # The unmatched token remains live so the right call can still match.
    assert consume_approval(session, tool_name="web_search", args={"q": 1}) is True


def test_consume_approval_expires():
    session = _S()
    record_approval(session, tool_name="web_search", args={"q": 1}, ttl_s=0)
    # ttl_s=0 → expires immediately (record_approval clamps at >=0).
    time.sleep(0.01)
    assert consume_approval(session, tool_name="web_search", args={"q": 1}) is False


def test_gate_consumes_approval_on_next_turn(tmp_path):
    store = EventStore(tmp_path / "logs")
    session = _S()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=store,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )
    args = _Args(query="el niño")
    ctx = ToolContext()
    asyncio.run(ask_fn(_Tool(), args, ctx))
    assert len(store.list_events()) == 1

    # Operator approves the gate — stamp the single-shot token onto the session.
    record_approval(session, tool_name="web_search", args={"query": "el niño"}, ttl_s=600)

    # Turn-2: fresh per-turn state; the same ASK now auto-passes without emit.
    reset_per_turn_state(session)
    approved = asyncio.run(ask_fn(_Tool(), args, ctx))
    assert approved is True
    assert len(store.list_events()) == 1  # no new event written
