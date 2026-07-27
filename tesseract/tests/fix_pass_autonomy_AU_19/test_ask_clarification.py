"""AU-19 — ask_clarification round-trip via EventStore + comment thread.

Covers:
- new ``clarification`` EventKind + ``agent`` EventSource
- urgency → priority mapping
- expires_at injection
- empty question rejected
- operator comment threads back through ``list_comments`` (the live
  delivery substrate is ``_start_workspace_turn`` / drain — this test
  exercises the storage seam the worker would poll)
- workspace ``post_decision`` accepts ``resolve`` on the new kind
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tesseract.kernel.tools.ask_clarification import (
    AskClarificationInput,
    AskClarificationTool,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.workspace_events import EventStore, WorkspaceComment


def _store(tmp_path: Path) -> EventStore:
    return EventStore(logs_dir=tmp_path)


async def test_class_metadata_pinned() -> None:
    assert AskClarificationTool.default_posture == "auto"
    assert AskClarificationTool.risk_class == "autonomous"


async def test_posts_clarification_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tool = AskClarificationTool(store=store)
    ctx = ToolContext(session_id="sess-abc")
    result = await tool.run(
        AskClarificationInput(
            question="Should I use Tavily or Brave for this search?",
            context="Need a fresh news result for the morning brief.",
            urgency="normal",
        ),
        ctx,
    )
    assert not result.is_error
    event_id = result.metadata["event_id"]
    events = store.list_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_id == event_id
    assert ev.kind == "clarification"
    assert ev.source == "agent"
    assert ev.priority == 5  # urgency=normal
    assert ev.payload["question"].startswith("Should I use Tavily")
    assert ev.payload["urgency"] == "normal"
    assert ev.payload["session_id"] == "sess-abc"
    # expires_at parses as ISO8601 in the future
    expires = dt.datetime.fromisoformat(ev.payload["expires_at"])
    assert expires > dt.datetime.now(dt.timezone.utc)


@pytest.mark.parametrize(
    "urgency, expected_priority",
    [("low", 3), ("normal", 5), ("high", 8)],
)
async def test_urgency_maps_to_priority(tmp_path, urgency, expected_priority) -> None:
    store = _store(tmp_path)
    tool = AskClarificationTool(store=store)
    result = await tool.run(
        AskClarificationInput(question="q?", urgency=urgency),
        ToolContext(),
    )
    assert not result.is_error
    ev = store.list_events()[0]
    assert ev.priority == expected_priority


async def test_empty_question_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tool = AskClarificationTool(store=store)
    result = await tool.run(
        AskClarificationInput(question="   "),
        ToolContext(),
    )
    assert result.is_error
    assert "non-empty" in result.output


async def test_operator_reply_round_trip_via_comment_thread(tmp_path: Path) -> None:
    """Worker side polls list_comments(event_id) to pick up the answer.

    The live broadcast substrate (``_start_workspace_turn``) is what
    delivers the comment as a fresh chat turn in Mirror — this test
    proves the storage seam works end-to-end."""
    store = _store(tmp_path)
    tool = AskClarificationTool(store=store)
    result = await tool.run(
        AskClarificationInput(question="Brave or Tavily?"),
        ToolContext(session_id="worker-sess"),
    )
    event_id = result.metadata["event_id"]
    comment = WorkspaceComment.new(
        event_id=event_id,
        author="operator",
        body="Use Tavily — the brief needs deep extraction.",
    )
    store.append_comment(comment)
    thread = store.list_comments(event_id)
    assert len(thread) == 1
    assert thread[0].author == "operator"
    assert "Tavily" in thread[0].body
    undelivered = store.list_undelivered_operator_comments()
    assert any(c.comment_id == comment.comment_id for c in undelivered)


async def test_resolve_is_permitted_on_clarification(tmp_path: Path) -> None:
    """post_decision's _RESOLVABLE_KINDS gate must include 'clarification'.

    Imports the constant directly to avoid spinning up an aiohttp test
    harness — the gate is a pure set membership check.
    """
    from tesseract.mirror.server.routes.workspace import _RESOLVABLE_KINDS

    assert "clarification" in _RESOLVABLE_KINDS


async def test_expires_in_hours_bounds_enforced() -> None:
    with pytest.raises(ValueError):
        AskClarificationInput(question="q?", expires_in_hours=0)
    with pytest.raises(ValueError):
        AskClarificationInput(question="q?", expires_in_hours=200)
