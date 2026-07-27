"""Core contract tests for dispatch_workspace_reply.

Hard requirements verified:
1. dispatch_to_controller called with wait_for_completion=True,
   spawn_if_missing=False, origin="mirror", mode="chat"
2. No double-write: controller writes comment; backend does NOT write it
3. Works with app=None (no Mirror session required)
4. Timestamp-based detection: new tars comments found and broadcast after dispatch
5. ZERO writes to real tesseract/logs/** (TESSERACT_HOME=tmp_path enforced)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tesseract.workspace_events.events import EventStore, WorkspaceComment, WorkspaceEvent


# ── helpers ────────────────────────────────────────────────────────────


def _seed_event(store: EventStore) -> WorkspaceEvent:
    ev = WorkspaceEvent.new(
        kind="reflection_proposal",
        source="orchestrator",
        title="Test event",
        summary="Summary for workspace reply test",
        payload={},
    )
    store.append_event(ev)
    return ev


@dataclass
class _FakeDispatchResult:
    session_id: str = "sess-wr-test"
    assistant_text: str = ""
    saw_assistant_text: bool = True
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_calls_controller_with_required_params(
    tmp_path: Path, monkeypatch,
) -> None:
    """dispatch_to_controller must be called with wait_for_completion=True,
    spawn_if_missing=False, origin='mirror', mode='chat'."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    dispatch_kwargs: list[dict[str, Any]] = []

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        dispatch_kwargs.append(kwargs)
        return _FakeDispatchResult()

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )
    # Suppress broadcast (no real app)
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.broadcast_comment_appended",
        lambda *a, **kw: None,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    await dispatch_workspace_reply(
        None,  # app=None — session-independent
        event_id=ev.event_id,
        comment_id="cmt-001",
        event=ev,
        kind="comment",
        comment_text="What's the plan?",
        config=cfg,
    )

    assert len(dispatch_kwargs) == 1, "dispatch_to_controller must be called exactly once"
    kw = dispatch_kwargs[0]
    assert kw["wait_for_completion"] is True, "must wait for completion (durability)"
    assert kw["spawn_if_missing"] is False, "must not cold-fork daemon from web request"
    assert kw["origin"] == "mirror", f"origin must be 'mirror', got {kw['origin']!r}"
    assert kw["mode"] == "chat", f"mode must be 'chat', got {kw['mode']!r}"
    assert kw["idle_timeout_seconds"] == 30.0


@pytest.mark.asyncio
async def test_no_double_write(tmp_path: Path, monkeypatch) -> None:
    """Backend must NOT write the reply comment. Only the controller writes it.

    We simulate the controller having already written a tars comment before
    dispatch_workspace_reply reads it. The function must broadcast that
    comment without writing a second one.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    written: list[WorkspaceComment] = []

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        # Controller "writes" its comment here (after dispatch_start is recorded).
        # Creating the comment INSIDE the dispatch so its ts >= dispatch_start.
        c = WorkspaceComment.new(
            event_id=ev.event_id,
            author="tars",
            body="Here is my reply.",
        )
        store.append_comment(c)
        written.append(c)
        return _FakeDispatchResult()

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )

    broadcast_calls: list[WorkspaceComment] = []

    async def _fake_broadcast(app: Any, comment: WorkspaceComment) -> None:
        broadcast_calls.append(comment)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.broadcast_comment_appended",
        _fake_broadcast,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    result = await dispatch_workspace_reply(
        None,
        event_id=ev.event_id,
        comment_id="cmt-001",
        event=ev,
        kind="comment",
        comment_text="What's the plan?",
        config=cfg,
    )

    # Exactly one tars comment in the store (controller wrote it, backend did not).
    all_comments = store.list_comments(ev.event_id)
    tars_comments = [c for c in all_comments if c.author == "tars"]
    assert len(tars_comments) == 1, (
        f"Exactly one tars comment expected (no double-write); got {len(tars_comments)}"
    )
    assert tars_comments[0].body == "Here is my reply."

    # Backend broadcast the controller-written comment, not a new one.
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0].comment_id == written[0].comment_id

    # Return value is the controller-written comment.
    assert result is not None
    assert result.comment_id == written[0].comment_id


@pytest.mark.asyncio
async def test_works_with_no_mirror_session(tmp_path: Path, monkeypatch) -> None:
    """app=None must not raise — dispatch works without a live Mirror session."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    written: list[WorkspaceComment] = []

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        # Create comment INSIDE dispatch so ts >= dispatch_start.
        c = WorkspaceComment.new(
            event_id=ev.event_id, author="tars", body="Session-independent reply."
        )
        store.append_comment(c)
        written.append(c)
        return _FakeDispatchResult()

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )

    broadcast_calls: list[Any] = []

    async def _fake_broadcast(app: Any, comment: Any) -> None:
        # app=None must be passed through without error.
        broadcast_calls.append((app, comment))

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.broadcast_comment_appended",
        _fake_broadcast,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    # Must not raise with app=None.
    result = await dispatch_workspace_reply(
        None,
        event_id=ev.event_id,
        comment_id="cmt-002",
        event=ev,
        kind="comment",
        comment_text="hello",
        config=cfg,
    )
    assert result is not None
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0][0] is None  # app=None passed through
    assert broadcast_calls[0][1].comment_id == written[0].comment_id


@pytest.mark.asyncio
async def test_timestamp_based_detection(tmp_path: Path, monkeypatch) -> None:
    """Only tars comments written AFTER dispatch_start are broadcast.

    A pre-existing tars comment must NOT be re-broadcast.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    # Pre-existing tars comment (e.g. from a previous turn).
    old_comment = WorkspaceComment.new(
        event_id=ev.event_id, author="tars", body="Old reply from before."
    )
    store.append_comment(old_comment)

    written: list[WorkspaceComment] = []

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        # Controller writes a new comment INSIDE dispatch (ts >= dispatch_start).
        c = WorkspaceComment.new(
            event_id=ev.event_id, author="tars", body="Fresh reply from controller."
        )
        store.append_comment(c)
        written.append(c)
        return _FakeDispatchResult()

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )

    broadcast_calls: list[WorkspaceComment] = []

    async def _fake_broadcast(app: Any, comment: WorkspaceComment) -> None:
        broadcast_calls.append(comment)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.broadcast_comment_appended",
        _fake_broadcast,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    await dispatch_workspace_reply(
        None,
        event_id=ev.event_id,
        comment_id="cmt-003",
        event=ev,
        kind="comment",
        comment_text="follow-up",
        config=cfg,
    )

    # Only the new comment (written during dispatch) is broadcast.
    # The old pre-existing tars comment must NOT be re-broadcast.
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0].comment_id == written[0].comment_id
    assert broadcast_calls[0].body == "Fresh reply from controller."


@pytest.mark.asyncio
async def test_dispatcher_error_returns_none(tmp_path: Path, monkeypatch) -> None:
    """DispatcherError → returns None gracefully, no raise."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    from tesseract.orchestrator.tars_controller.dispatcher import DispatcherError

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        raise DispatcherError("daemon not running")

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    result = await dispatch_workspace_reply(
        None,
        event_id=ev.event_id,
        comment_id="cmt-004",
        event=ev,
        kind="comment",
        comment_text="will fail",
        config=cfg,
    )
    assert result is None


@pytest.mark.asyncio
async def test_timed_out_returns_none(tmp_path: Path, monkeypatch) -> None:
    """Timed-out dispatch → returns None gracefully."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        return _FakeDispatchResult(timed_out=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    result = await dispatch_workspace_reply(
        None,
        event_id=ev.event_id,
        comment_id="cmt-005",
        event=ev,
        kind="comment",
        comment_text="slow",
        config=cfg,
    )
    assert result is None


@pytest.mark.asyncio
async def test_no_real_log_writes(tmp_path: Path, monkeypatch) -> None:
    """Verify zero writes to real tesseract/logs/workspace/ during dispatch."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    from tesseract.paths import TESSERACT_HOME as live_home
    real_ws_dir = live_home / "logs" / "workspace"

    # Baseline: count files in real workspace log dir before test.
    before_files = set(real_ws_dir.glob("*.jsonl")) if real_ws_dir.exists() else set()

    store = EventStore(tmp_path / "logs")
    ev = _seed_event(store)

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> _FakeDispatchResult:
        return _FakeDispatchResult()

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.dispatch_to_controller",
        _fake_dispatch,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.workspace_reply_dispatch.broadcast_comment_appended",
        lambda *a, **kw: None,
    )

    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        WorkspaceReplyConfig,
        dispatch_workspace_reply,
    )

    cfg = WorkspaceReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    await dispatch_workspace_reply(
        None,
        event_id=ev.event_id,
        comment_id="cmt-006",
        event=ev,
        kind="comment",
        comment_text="test",
        config=cfg,
    )

    after_files = set(real_ws_dir.glob("*.jsonl")) if real_ws_dir.exists() else set()
    new_files = after_files - before_files
    assert not new_files, f"Real log dir must not be touched by tests; new files: {new_files}"
