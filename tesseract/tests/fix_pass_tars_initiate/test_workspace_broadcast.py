"""workspace_post / workspace_reply — live WS broadcast after append.

The append was already correct; this verifies the broadcast hook fires
so an open Mirror Workspace tab re-renders without a manual refresh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.workspace_post import (
    WorkspacePostInput,
    WorkspacePostTool,
)
from tesseract.kernel.tools.workspace_reply import (
    WorkspaceReplyInput,
    WorkspaceReplyTool,
)
from tesseract.workspace_events import EventStore, WorkspaceEvent


class _StubWS:
    closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._last = payload


class _StubSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.event_log: list[dict[str, Any]] = []
        self.ws = _StubWS()


def _store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path)


@pytest.mark.asyncio
async def test_workspace_post_broadcasts(tmp_path: Path) -> None:
    sess = _StubSession("s1")
    app = {"server_sessions": {"s1": sess}}
    store = _store(tmp_path)
    tool = WorkspacePostTool(store=store, app_provider=lambda: app)

    result = await tool.run(
        WorkspacePostInput(title="hello", summary="from TARS", priority=5),
        ToolContext(),
    )

    assert not result.is_error
    # One envelope was forwarded to the open WS — type matches what the
    # frontend dispatch.ts already handles for live Workspace updates.
    assert len(sess.event_log) == 1
    env = sess.event_log[0]
    assert env["type"] == "workspace_event_appended"
    assert env["category"] == "workspace"
    assert env["data"]["title"] == "hello"


@pytest.mark.asyncio
async def test_workspace_post_no_app_provider_still_writes(tmp_path: Path) -> None:
    """REPL / unit-test path — no app provider means no broadcast, but
    the on-disk write must still succeed."""
    store = _store(tmp_path)
    tool = WorkspacePostTool(store=store)

    result = await tool.run(
        WorkspacePostInput(title="hello", summary="from TARS", priority=5),
        ToolContext(),
    )

    assert not result.is_error
    assert len(store.list_events()) == 1


@pytest.mark.asyncio
async def test_workspace_reply_does_not_double_broadcast(tmp_path: Path) -> None:
    """The Mirror ws.py post-tool hook (`_broadcast_workspace_reply`) is the
    sole broadcaster for TARS replies — the tool itself only writes to
    disk so a connected session doesn't see each reply render twice."""
    sess = _StubSession("s1")
    store = _store(tmp_path)
    event = store.append_event(WorkspaceEvent.new(
        kind="tars_post", source="tars", title="x", summary="y",
        payload={}, priority=5,
    ))
    from tesseract.workspace_events import WorkspaceComment
    op_comment = WorkspaceComment.new(
        event_id=event.event_id, author="operator", body="thoughts?",
    )
    store.append_comment(op_comment)

    tool = WorkspaceReplyTool(store=store)
    result = await tool.run(
        WorkspaceReplyInput(
            event_id=event.event_id,
            comment_id=op_comment.comment_id,
            body="my answer",
        ),
        ToolContext(),
    )

    assert not result.is_error
    assert sess.event_log == []
