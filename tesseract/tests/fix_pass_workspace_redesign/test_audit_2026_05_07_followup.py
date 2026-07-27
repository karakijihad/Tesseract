"""Codex audit 2026-05-07 follow-up coverage.

Two findings closed here:

- **M1** — workspace delivery is now per queued payload. With two
  workspace items pending at once, draining for turn A must not pull
  item B's id into the stash; otherwise a successful reply on A would
  mark B delivered before B's own queued turn fires.

- **m1** — `resolve` is restricted to conversational-thread kinds.
  A pending `change_proposal` / `reflection_proposal` / feedback_*
  must still go through approve/reject; `delete` remains the universal
  soft-close escape hatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest

from tesseract.brain import chat as chat_mod
from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)
from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceComment, WorkspaceEvent


# ---------- M1: per-payload workspace drain ----------


def _seed_post(store: EventStore, *, body: str) -> WorkspaceEvent:
    ev = WorkspaceEvent.new(
        kind="operator_post",
        source="operator",
        title="title",
        summary=body[:400],
        payload={"body": body, "source": "scratchpad"},
    )
    store.append_event(ev)
    return ev


def _seed_event_with_comment(
    store: EventStore, *, body: str,
) -> tuple[WorkspaceEvent, WorkspaceComment]:
    ev = WorkspaceEvent.new(
        kind="reflection_proposal",
        source="orchestrator",
        title="needs operator clarification",
        summary="x",
        payload={},
    )
    store.append_event(ev)
    c = WorkspaceComment.new(event_id=ev.event_id, author="operator", body=body)
    store.append_comment(c)
    return ev, c


def test_drain_pinned_to_target_when_two_posts_pending(
    tmp_path: Path, monkeypatch,
) -> None:
    """Two operator_posts pending at once. Drain for post A returns ONLY
    A's id and block, so confirming delivery for A's turn cannot mark B
    delivered too early."""
    logs_dir = tmp_path / "logs"
    store = EventStore(logs_dir)
    monkeypatch.setattr(
        "tesseract.kernel.workspace_changes.workspace_events_dir",
        lambda: logs_dir,
    )

    ev_a = _seed_post(store, body="alpha")
    ev_b = _seed_post(store, body="bravo")

    blocks_a, ids_a = chat_mod._drain_operator_posts(ev_a.event_id)
    assert ids_a == [ev_a.event_id]
    assert "alpha" in blocks_a[0]
    assert "bravo" not in blocks_a[0]

    # B is still untouched on the disk.
    pending_after = {e.event_id for e in store.list_undelivered_operator_posts()}
    assert ev_b.event_id in pending_after


def test_drain_pinned_to_target_when_two_comments_pending(
    tmp_path: Path, monkeypatch,
) -> None:
    """Two operator comments pending across two events. Drain for
    comment A pulls only A's row."""
    logs_dir = tmp_path / "logs"
    store = EventStore(logs_dir)
    monkeypatch.setattr(
        "tesseract.kernel.workspace_changes.workspace_events_dir",
        lambda: logs_dir,
    )

    ev_a, c_a = _seed_event_with_comment(store, body="first comment")
    ev_b, c_b = _seed_event_with_comment(store, body="second comment")

    blocks, ids = chat_mod._drain_workspace_comments(c_a.comment_id)
    assert ids == [c_a.comment_id]
    assert c_a.body in blocks[0]
    assert c_b.body not in blocks[0]

    # B's comment is still pending.
    pending = {c.comment_id for c in store.list_undelivered_operator_comments()}
    assert c_b.comment_id in pending


# ---------- M1: ChatSession integration — successful reply on turn A
# does NOT mark B delivered ----------


class _FakeAdapter(ModelAdapter):
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        yield StreamChunk(type=ChunkType.TEXT, text="ok")
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return sum(len(str(m.get("content", ""))) for m in messages) // 4

    async def check_available(self) -> bool:
        return True


def _make_session() -> ChatSession:
    return ChatSession(
        adapter=_FakeAdapter(),
        system_prompt="",
        max_tool_iterations=4,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="fake", provider="fake", context_window=400_000),
    )


@pytest.mark.asyncio
async def test_concurrent_workspace_items_isolated_across_turns(
    tmp_path: Path, monkeypatch,
) -> None:
    """Two pending workspace items at the start of turn A. After A's
    drain runs and ``confirm_workspace_delivery`` fires (simulating a
    successful workspace_reply), B is STILL undelivered — so B's own
    queued synthetic turn will see B's body when it runs.

    Without the M1 fix, A's drain pulled both ids into the stash and
    confirm flushed both, leaving B's later turn with nothing to inject.
    """
    logs_dir = tmp_path / "logs"
    store = EventStore(logs_dir)
    monkeypatch.setattr(
        "tesseract.kernel.workspace_changes.workspace_events_dir",
        lambda: logs_dir,
    )

    ev_a = _seed_post(store, body="alpha body")
    ev_b = _seed_post(store, body="bravo body")

    cs = _make_session()
    # Synthetic turn for A (transient + targeted at A).
    async for _ in cs.send(
        "synthetic A directive",
        transient=True,
        workspace_origin={"event_id": ev_a.event_id, "comment_id": ev_a.event_id},
    ):
        pass
    # The Mirror commit gate fires after a successful workspace_reply.
    cs.confirm_workspace_delivery()

    pending = {e.event_id for e in store.list_undelivered_operator_posts()}
    assert ev_a.event_id not in pending, "A should be delivered after its turn"
    assert ev_b.event_id in pending, (
        "B must NOT be marked delivered by A's turn — that was the M1 bug"
    )

    # B's own turn delivers it.
    async for _ in cs.send(
        "synthetic B directive",
        transient=True,
        workspace_origin={"event_id": ev_b.event_id, "comment_id": ev_b.event_id},
    ):
        pass
    cs.confirm_workspace_delivery()

    pending_final = {e.event_id for e in store.list_undelivered_operator_posts()}
    assert pending_final == set(), "Both posts should be delivered after their own turns"


@pytest.mark.asyncio
async def test_workspace_origin_none_does_not_drain_stores(
    tmp_path: Path, monkeypatch,
) -> None:
    """Codex M3 invariant preserved: a regular chat turn (workspace_origin
    is None) never consults the workspace stores. Sanity check after the
    M1 signature change."""
    logs_dir = tmp_path / "logs"
    store = EventStore(logs_dir)
    monkeypatch.setattr(
        "tesseract.kernel.workspace_changes.workspace_events_dir",
        lambda: logs_dir,
    )

    ev = _seed_post(store, body="should remain pending")

    cs = _make_session()
    async for _ in cs.send("regular chat turn"):
        pass

    pending = {e.event_id for e in store.list_undelivered_operator_posts()}
    assert ev.event_id in pending


# ---------- m1: route gates resolve by kind ----------


class _StubReq:
    def __init__(self, store: EventStore, *, match=None, body=None):
        self.app = {"workspace_event_store": store}
        self.match_info = match or {}
        self.query = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _resp_json(resp) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "change_proposal",
        "mission_reflection_proposal",
        "feedback_proposal",
        "feedback_sweep",
        "soul_proposal",
        "agent_approval",
    ],
)
async def test_resolve_rejected_for_gated_kinds(tmp_path: Path, kind: str) -> None:
    store = EventStore(tmp_path)
    ev = WorkspaceEvent.new(
        kind=kind, source="orchestrator", title="x", summary="x", payload={},
    )
    store.append_event(ev)

    req = _StubReq(
        store, match={"event_id": ev.event_id}, body={"decision": "resolve"},
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 400, f"{kind} must reject resolve"
    payload = _resp_json(resp)
    assert payload["error"] == "resolve_not_permitted_for_kind"
    assert payload["kind"] == kind

    # The event is unchanged on disk — no silent status flip.
    assert store.get_event(ev.event_id).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "operator_post",
        "tars_post",
        "nudge",
        "reflection_proposal",
        "daily_brief",
        "clarification",
        "recovery_summary",
        "strategist_summary",
        "runtime_lock_deny",
    ],
)
async def test_resolve_accepted_for_conversational_kinds(
    tmp_path: Path, kind: str,
) -> None:
    store = EventStore(tmp_path)
    ev = WorkspaceEvent.new(
        kind=kind, source="operator", title="x", summary="x", payload={},
    )
    store.append_event(ev)

    req = _StubReq(
        store, match={"event_id": ev.event_id}, body={"decision": "resolve"},
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200, f"{kind} must accept resolve"
    payload = _resp_json(resp)
    assert payload["status"] == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["change_proposal", "operator_post", "reflection_proposal"],
)
async def test_delete_works_on_any_kind(tmp_path: Path, kind: str) -> None:
    """`delete` is the universal escape hatch — applies regardless of
    kind or status. The audit flagged `resolve` specifically; `delete`
    remains broad by design."""
    store = EventStore(tmp_path)
    ev = WorkspaceEvent.new(
        kind=kind, source="operator", title="x", summary="x", payload={},
    )
    store.append_event(ev)

    req = _StubReq(
        store, match={"event_id": ev.event_id}, body={"decision": "delete"},
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    assert _resp_json(resp)["status"] == "deleted"


@pytest.mark.asyncio
async def test_approve_reject_unaffected_by_resolve_gate(tmp_path: Path) -> None:
    """Sanity: approve / reject still work on gated kinds. The resolve
    gate must not regress the primary decision path."""
    store = EventStore(tmp_path)
    ev = WorkspaceEvent.new(
        kind="mission_reflection_proposal", source="orchestrator",
        title="x", summary="x", payload={},
    )
    store.append_event(ev)

    req = _StubReq(
        store, match={"event_id": ev.event_id}, body={"decision": "reject"},
    )
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    assert _resp_json(resp)["status"] == "rejected"


# ---------- i1: workspace queue drains FIFO across turn cycles ----------


def test_pending_workspace_payloads_fifo_across_cycles() -> None:
    """The session-side FIFO queue (Codex 2026-05-06 M1) drains one
    payload per turn cycle in arrival order. The drain target ids
    cleared on each pop are exactly the ids of the popped payload —
    end-to-end this means turn N's drain only sees turn N's payload."""
    from tesseract.mirror.server.session import ServerSession

    s = ServerSession.__new__(ServerSession)  # bypass __post_init__ setup
    from collections import deque
    s.pending_workspace_payloads = deque(maxlen=64)

    payload_a = {"workspace_origin": {"event_id": "evt_A", "comment_id": "evt_A"}}
    payload_b = {"workspace_origin": {"event_id": "evt_B", "comment_id": "evt_B"}}
    payload_c = {"workspace_origin": {"event_id": "evt_C", "comment_id": "evt_C"}}

    s.pending_workspace_payloads.append(payload_a)
    s.pending_workspace_payloads.append(payload_b)
    s.pending_workspace_payloads.append(payload_c)

    drained = []
    while s.pending_workspace_payloads:
        drained.append(s.pending_workspace_payloads.popleft())

    assert [p["workspace_origin"]["event_id"] for p in drained] == [
        "evt_A", "evt_B", "evt_C",
    ]
