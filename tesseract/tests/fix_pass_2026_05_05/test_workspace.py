"""Workspace tab Phase 1 — events, comment round-trip, REST contracts.

Covers the data-model boundary (EventStore round-trip), the two TARS
tools (workspace_post, workspace_reply), and the REST handlers via
in-process aiohttp app construction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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
from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceEvent


def _store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path)


def _seed(store: EventStore, **overrides) -> WorkspaceEvent:
    defaults = dict(
        kind="feedback_proposal",
        source="feedback_consolidator",
        title="merge mem_a into mem_b",
        summary="duplicates",
        payload={"action": "merge_into", "keep": "mem_b", "absorb": ["mem_a"]},
        priority=5,
    )
    defaults.update(overrides)
    return store.append_event(WorkspaceEvent.new(**defaults))


def test_event_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ev = _seed(store)
    rows = store.list_events()
    assert len(rows) == 1
    assert rows[0].event_id == ev.event_id


def test_event_status_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ev = _seed(store)
    updated = store.update_event_status(ev.event_id, "approved", reason="ok")
    assert updated is not None and updated.status == "approved"
    rows = store.list_events()
    assert rows[0].status == "approved"


def test_status_update_unknown_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.update_event_status("evt_missing", "approved") is None


def test_list_events_priority_then_recency(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Explicit, non-colliding timestamps so within-priority ordering is
    # well-defined regardless of monotonic-clock granularity.
    a = WorkspaceEvent(
        event_id="evt_a", ts="2026-05-05T10:00:00+00:00",
        kind="feedback_proposal", source="feedback_consolidator",
        title="low", summary="x", payload={}, priority=3,
    )
    b = WorkspaceEvent(
        event_id="evt_b", ts="2026-05-05T10:00:01+00:00",
        kind="feedback_proposal", source="feedback_consolidator",
        title="high", summary="x", payload={}, priority=9,
    )
    c = WorkspaceEvent(
        event_id="evt_c", ts="2026-05-05T10:00:02+00:00",
        kind="feedback_proposal", source="feedback_consolidator",
        title="low2", summary="x", payload={}, priority=3,
    )
    for ev in (a, b, c):
        store.append_event(ev)
    ids = [e.event_id for e in store.list_events()]
    assert ids[0] == b.event_id
    # Within the priority=3 bucket, newer (c) comes before older (a).
    assert ids.index(c.event_id) < ids.index(a.event_id)


def test_list_events_filter_by_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = _seed(store, title="a")
    b = _seed(store, title="b")
    store.update_event_status(b.event_id, "approved")
    pending = store.list_events(status="pending")
    assert [e.event_id for e in pending] == [a.event_id]


def test_undelivered_comments_drain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ev = _seed(store)
    from tesseract.workspace_events import WorkspaceComment
    c = WorkspaceComment.new(event_id=ev.event_id, author="operator",
                             body="please retry tomorrow")
    store.append_comment(c)
    pending = store.list_undelivered_operator_comments()
    assert len(pending) == 1 and pending[0].comment_id == c.comment_id
    assert store.mark_comment_delivered(c.comment_id) is True
    assert store.list_undelivered_operator_comments() == []
    # idempotent — second mark returns False
    assert store.mark_comment_delivered(c.comment_id) is False


def test_seen_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_seen("inbox", "2026-05-05T10:00:00+00:00")
    assert store.get_seen() == {"inbox": "2026-05-05T10:00:00+00:00"}


@pytest.mark.asyncio
async def test_workspace_post_creates_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tool = WorkspacePostTool(store=store)
    result = await tool.run(
        WorkspacePostInput(
            title="Noticed slow memory_search",
            summary="Latency on memory_search has doubled since the reindex; "
                    "may be worth re-running vault_lint.",
            priority=6,
        ),
        ToolContext(),
    )
    assert result.is_error is False
    assert result.metadata is not None
    event_id = result.metadata["event_id"]
    rows = store.list_events()
    assert len(rows) == 1 and rows[0].event_id == event_id
    assert rows[0].source == "tars"
    assert rows[0].kind == "tars_post"


@pytest.mark.asyncio
async def test_workspace_post_requires_body(tmp_path: Path) -> None:
    tool = WorkspacePostTool(store=_store(tmp_path))
    result = await tool.run(
        WorkspacePostInput(title="hi", summary="   "),
        ToolContext(),
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_workspace_reply_attaches_to_thread(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ev = _seed(store)
    from tesseract.workspace_events import WorkspaceComment
    op_comment = WorkspaceComment.new(
        event_id=ev.event_id, author="operator", body="please clarify",
    )
    store.append_comment(op_comment)

    tool = WorkspaceReplyTool(store=store)
    result = await tool.run(
        WorkspaceReplyInput(
            event_id=ev.event_id,
            comment_id=op_comment.comment_id,
            body="Clarification: the merge would keep mem_b which has higher importance.",
        ),
        ToolContext(),
    )
    assert result.is_error is False
    thread = store.list_comments(ev.event_id)
    assert len(thread) == 2
    reply = thread[1]
    assert reply.author == "tars"
    assert reply.reply_to == op_comment.comment_id


@pytest.mark.asyncio
async def test_workspace_reply_unknown_event_rejected(tmp_path: Path) -> None:
    tool = WorkspaceReplyTool(store=_store(tmp_path))
    result = await tool.run(
        WorkspaceReplyInput(
            event_id="evt_missing", comment_id="cmt_x", body="hi",
        ),
        ToolContext(),
    )
    assert result.is_error is True


# ---------- REST handler tests (direct invocation; no aiohttp test client) ----------


import json as _json
from types import SimpleNamespace


class _StubReq:
    """Minimal aiohttp.web.Request stand-in for the workspace handlers.

    The handlers only touch ``.app``, ``.match_info``, ``.query``, and
    ``.json()`` — replicating those is enough and avoids pulling in
    ``pytest-aiohttp``.
    """

    def __init__(self, store: EventStore, *, match=None, query=None, body=None):
        self.app = {"workspace_event_store": store}
        self.match_info = match or {}
        self.query = query or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _resp_json(resp) -> dict:
    return _json.loads(resp.body.decode("utf-8"))


async def test_inbox_endpoint_direct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed(store, title="A")
    _seed(store, title="B")
    resp = await ws_routes.list_inbox(_StubReq(store))
    data = _resp_json(resp)
    assert data["count"] == 2


async def test_decision_endpoint_direct(tmp_path: Path) -> None:
    """Generic approve/decision flow on a gated, record-only kind.

    ``feedback_proposal`` dispatches to ``memory_promote`` (see
    ``fix_pass_workspace_approve_dispatch_2026_05_24``) and
    ``agent_approval`` dispatches to the Stage 10 promotion core, so to
    exercise just the status-flip path the seed picks ``feedback_sweep``
    which stays record-only on approve.
    """
    store = _store(tmp_path)
    ev = _seed(store, kind="feedback_sweep")
    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"decision": "approve", "reason": "looks right"},
    )
    resp = await ws_routes.post_decision(req)
    data = _resp_json(resp)
    assert resp.status == 200
    assert data["status"] == "approved"
    pending = store.list_events(status="pending")
    assert ev.event_id not in [e.event_id for e in pending]


async def test_concurrent_approve_serializes_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent Approves on the same event must not both run the commit.

    The per-event asyncio.Lock serializes the check-commit-update span: the
    second decision re-reads under the lock, sees the settled status, and
    returns idempotently instead of committing a second time. Without the lock
    both would pass the pending check and the commit would fire twice.
    """
    store = _store(tmp_path)
    ev = _seed(store, kind="change_proposal")

    calls = {"n": 0}

    async def _fake_commit(request, event):  # noqa: ANN001, ARG001
        calls["n"] += 1
        await asyncio.sleep(0.02)  # widen the interleave window
        return None

    async def _noop_record(**_kw):  # noqa: ANN003
        return None

    monkeypatch.setattr(ws_routes, "_commit_change_proposal", _fake_commit)
    monkeypatch.setattr(ws_routes, "record_ask", _noop_record)

    def _req():
        return _StubReq(
            store,
            match={"event_id": ev.event_id},
            body={"decision": "approve"},
        )

    r1, r2 = await asyncio.gather(
        ws_routes.post_decision(_req()),
        ws_routes.post_decision(_req()),
    )

    assert calls["n"] == 1  # commit ran exactly once — lock serialized the span
    statuses = sorted([_resp_json(r1)["status"], _resp_json(r2)["status"]])
    assert statuses == ["approved", "approved"]  # both see the settled event
    assert store.get_event(ev.event_id).status == "approved"


async def test_decision_invalid_direct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ev = _seed(store)
    req = _StubReq(
        store, match={"event_id": ev.event_id}, body={"decision": "maybe"},
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 400


async def test_comment_endpoint_direct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ev = _seed(store)
    req = _StubReq(
        store,
        match={"event_id": ev.event_id},
        body={"body": "Need more context — what does mem_a actually say?"},
    )
    resp = await ws_routes.post_comment(req)
    data = _resp_json(resp)
    assert resp.status == 201
    assert data["author"] == "operator"
    assert data["delivered_to_tars"] is False


async def test_seen_endpoint_direct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    req = _StubReq(
        store,
        body={"panel": "inbox", "last_seen_at": "2026-05-05T12:00:00+00:00"},
    )
    resp = await ws_routes.post_seen(req)
    assert resp.status == 200
    resp2 = await ws_routes.get_seen(_StubReq(store))
    payload = _resp_json(resp2)
    assert payload["inbox"] == "2026-05-05T12:00:00+00:00"


async def test_chat_drain_workspace_comments(tmp_path: Path, monkeypatch) -> None:
    """The chat.py drain helper picks up undelivered operator comments
    and formats them as `[workspace_comment_on_<event_id>]` blocks. Codex
    2026-05-06 M2: marking delivered is now caller-driven; the drain
    returns the IDs and `_mark_workspace_delivered` commits them only
    after a successful workspace_reply."""
    store = _store(tmp_path)
    ev = _seed(store)
    from tesseract.workspace_events import WorkspaceComment
    op_comment = WorkspaceComment.new(
        event_id=ev.event_id, author="operator",
        body="What was the trigger here?",
    )
    store.append_comment(op_comment)

    # Redirect TESSERACT_HOME used inside the drain helper.
    import tesseract.brain.chat as chat_mod
    import tesseract.workspace_events as ws_mod

    class _StoreFactory:
        def __init__(self, base: Path):
            self._store = EventStore(base)

        def __call__(self, _path: Path) -> EventStore:
            return self._store

    factory = _StoreFactory(tmp_path)
    monkeypatch.setattr(ws_mod, "EventStore", factory)
    monkeypatch.setattr(chat_mod, "logger", chat_mod.logger)  # noop, ensures attr

    blocks, ids = chat_mod._drain_workspace_comments(op_comment.comment_id)
    assert len(blocks) == 1
    assert f"[workspace_comment_on_{ev.event_id}]" in blocks[0]
    assert "What was the trigger here?" in blocks[0]
    assert ids == [op_comment.comment_id]
    # Idempotent until caller marks delivered.
    blocks2, ids2 = chat_mod._drain_workspace_comments(op_comment.comment_id)
    assert blocks2 == blocks and ids2 == ids
    # After explicit mark, future drains are empty.
    chat_mod._mark_workspace_delivered(ids, [])
    blocks3, ids3 = chat_mod._drain_workspace_comments(op_comment.comment_id)
    assert blocks3 == [] and ids3 == []
    # Codex 2026-05-07 M1: the drain is no longer used as a global
    # flush — without a target id it returns nothing.
    assert chat_mod._drain_workspace_comments(None) == ([], [])
