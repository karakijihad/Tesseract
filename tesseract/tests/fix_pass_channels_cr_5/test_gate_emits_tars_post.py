"""CR-5 — fixture-driven tool call on a channel session triggers a
``tars_post`` workspace event with the full payload schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from tesseract.integrations._channel_gate import (
    args_fingerprint,
    build_channel_ask_fn,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.workspace_events.events import EventStore


class _StubArgs(BaseModel):
    query: str


class _StubTool:
    name = "web_search"

    def ask_reason(self, _validated) -> str:
        return "web_search wants the open internet"


@dataclass
class _StubSession:
    session_id: str = "tg_99_abc"
    chat_session: Any = field(default_factory=MagicMock)


def _make_store(tmp_path):
    return EventStore(tmp_path)


@pytest.fixture
def event_store(tmp_path):
    return _make_store(tmp_path / "logs")


def test_gate_emits_tars_post_with_full_payload(tmp_path, event_store):
    session = _StubSession()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=event_store,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )

    import asyncio
    validated = _StubArgs(query="el niño 2026")
    ctx = ToolContext(session_id="tg_99_abc", posture_source="default")
    approved = asyncio.run(ask_fn(_StubTool(), validated, ctx))
    assert approved is False

    events = event_store.list_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "tars_post"
    assert ev.source == "tars"
    assert ev.author_id == "telegram:99"
    payload = ev.payload
    assert payload["channel"] == "telegram"
    assert payload["chat_id"] == "99"
    assert payload["session_id"] == "tg_99_abc"
    assert payload["tool"] == "web_search"
    assert payload["args"] == {"query": "el niño 2026"}
    assert payload["reason"] == "web_search wants the open internet"
    assert payload["approve_next_turn_ttl_s"] == 1800
    assert payload["posture_source"] == "default"
    # args_hash is a stable fingerprint over (tool_name, args).
    assert payload["args_hash"] == args_fingerprint(
        "web_search", {"query": "el niño 2026"}
    )


def test_args_fingerprint_is_order_independent():
    a = args_fingerprint("t", {"a": 1, "b": 2})
    b = args_fingerprint("t", {"b": 2, "a": 1})
    assert a == b
    c = args_fingerprint("t", {"a": 1, "b": 3})
    assert a != c


def test_dedup_within_turn_emits_once(tmp_path, event_store):
    session = _StubSession()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=event_store,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )

    import asyncio
    validated = _StubArgs(query="x")
    ctx = ToolContext()
    # Three identical calls within the same turn — only one tars_post.
    for _ in range(3):
        approved = asyncio.run(ask_fn(_StubTool(), validated, ctx))
        assert approved is False
    assert len(event_store.list_events()) == 1


def test_per_turn_reset_re_emits_on_next_turn(tmp_path, event_store):
    from tesseract.integrations._channel_gate import reset_per_turn_state

    session = _StubSession()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=event_store,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )
    import asyncio
    validated = _StubArgs(query="x")
    ctx = ToolContext()
    asyncio.run(ask_fn(_StubTool(), validated, ctx))
    # Same call mid-turn — dedup.
    asyncio.run(ask_fn(_StubTool(), validated, ctx))
    assert len(event_store.list_events()) == 1
    # Turn-2: reset clears the dedup set; the next call re-emits.
    reset_per_turn_state(session)
    asyncio.run(ask_fn(_StubTool(), validated, ctx))
    assert len(event_store.list_events()) == 2


def test_concurrent_safe_tools_dedup_to_one_event(tmp_path, event_store):
    """Two safe tools fanned out by ``chat.py::_run_one`` enter the gate
    concurrently. The shared per-turn set must guarantee exactly one
    ``tars_post`` lands — not zero (lost-update) and not two (race)."""
    session = _StubSession()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=event_store,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )

    import asyncio
    args = _StubArgs(query="el niño")
    ctx = ToolContext()

    async def _drive():
        return await asyncio.gather(
            ask_fn(_StubTool(), args, ctx),
            ask_fn(_StubTool(), args, ctx),
            ask_fn(_StubTool(), args, ctx),
        )

    results = asyncio.run(_drive())
    assert results == [False, False, False]
    assert len(event_store.list_events()) == 1


def test_record_approval_by_hash_matches_gate_fingerprint(tmp_path, event_store):
    """An operator approval keyed on the *stored* args_hash auto-passes
    the gate even when the args contain values that would coerce to
    something different on a second hash pass."""
    from tesseract.integrations._channel_gate import (
        consume_approval,
        record_approval_by_hash,
    )

    session = _StubSession()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=event_store,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )
    import asyncio
    args = _StubArgs(query="abc")
    ctx = ToolContext()
    asyncio.run(ask_fn(_StubTool(), args, ctx))
    [ev] = event_store.list_events()
    stored_hash = ev.payload["args_hash"]

    # Approve by hash; the next consume_approval call must match.
    record_approval_by_hash(session, args_hash=stored_hash, ttl_s=600)
    assert consume_approval(
        session, tool_name="web_search", args={"query": "abc"},
    ) is True


def test_no_event_store_falls_back_to_deny(tmp_path):
    session = _StubSession()
    ask_fn = build_channel_ask_fn(
        session=session,
        channel="telegram",
        chat_id="99",
        display_name="Telegram",
        event_store=None,
        conversation_store=None,
        approve_next_turn_ttl_s=1800,
    )
    import asyncio
    approved = asyncio.run(
        ask_fn(_StubTool(), _StubArgs(query="x"), ToolContext()),
    )
    assert approved is False
