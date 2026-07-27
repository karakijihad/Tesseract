"""Core contract tests for dispatch_agenda_reply (Option-B durability).

Mirrors ``tests/fix_pass_workspace_controller_2026_05_25/
test_dispatch_workspace_reply.py``. Hard requirements verified:

1. Prompt instructs the controller to call `agenda_comment` with the
   exact `item_id` — no reasoning fragility.
2. dispatch_to_controller called with wait_for_completion=True,
   spawn_if_missing=False, origin="mirror", mode="chat".
3. No double-write: the controller (simulated) writes the comment via
   the tool; dispatch_agenda_reply does NOT write a second one.
4. Works with app=None (no Mirror session required).
5. DispatcherError / timed_out -> returns None gracefully.
6. ZERO writes to real tesseract/logs/** (TESSERACT_HOME=tmp_path enforced).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy.agenda_comments import append_comment, list_comments
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.agenda_reply import (
    AgendaReplyConfig,
    build_agenda_reply_prompt,
    dispatch_agenda_reply,
)
from tesseract.orchestrator.tars_controller.dispatcher import DispatcherError, DispatchResult


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _item() -> AgendaItem:
    when = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    return AgendaItem(
        id=mint_agenda_id("review-discovery", now=when),
        source=AgendaSource.MEMORY_SIGNAL,
        goal="Review discovery cluster: local_llama_weekly",
        rationale="Entities crossed the threshold; operator review gate open.",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.AWAITING_OPERATOR,
        created_at=when,
        updated_at=when,
    )


# ── prompt ───────────────────────────────────────────────────────────────


def test_prompt_contains_must_call_directive_and_exact_item_id(isolated_home: Path) -> None:
    item = _item()
    append_comment(item.id, role="operator", by="sess_op", body="what should we do?")
    thread = list_comments(item.id)

    prompt = build_agenda_reply_prompt(item, thread)

    assert "You MUST call the `agenda_comment` tool" in prompt
    assert f'item_id = "{item.id}"' in prompt
    assert "Do NOT produce chat text" in prompt


# ── dispatch ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_calls_controller_with_required_params(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    thread = list_comments(item.id)

    dispatch_kwargs: list[dict[str, Any]] = []

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        dispatch_kwargs.append(kwargs)
        append_comment(item.id, role="agent", by="tars", body="tool-written reply")
        return DispatchResult(session_id="ctl-1", saw_assistant_text=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.broadcast_agenda_comment_event",
        lambda *a, **kw: None,
    )

    cfg = AgendaReplyConfig(enabled=True, idle_timeout_seconds=30.0)
    await dispatch_agenda_reply(None, item=item, thread=thread, config=cfg)

    assert len(dispatch_kwargs) == 1, "dispatch_to_controller must be called exactly once"
    kw = dispatch_kwargs[0]
    assert kw["wait_for_completion"] is True, "must wait for completion (durability)"
    assert kw["spawn_if_missing"] is False, "must not cold-fork daemon from web request"
    assert kw["origin"] == "mirror", f"origin must be 'mirror', got {kw['origin']!r}"
    assert kw["mode"] == "chat", f"mode must be 'chat', got {kw['mode']!r}"
    assert kw["idle_timeout_seconds"] == 30.0


@pytest.mark.asyncio
async def test_no_double_write(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """dispatch_agenda_reply must NOT write the reply comment itself.

    Only the controller (via the agenda_comment tool) writes it. Simulated
    here by the fake dispatch appending the comment directly, the same as
    the tool would.
    """
    item = _item()
    thread = list_comments(item.id)

    written: list[Any] = []

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        c = append_comment(item.id, role="agent", by="tars", body="Here is my reply.")
        written.append(c)
        return DispatchResult(session_id="ctl-1", saw_assistant_text=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )

    broadcast_calls: list[Any] = []

    async def _fake_broadcast(app: Any, event_type: str, *, item_id: str, comment: dict) -> None:
        broadcast_calls.append(comment)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.broadcast_agenda_comment_event",
        _fake_broadcast,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)

    all_comments = list_comments(item.id)
    agent_comments = [c for c in all_comments if c.role == "agent"]
    assert len(agent_comments) == 1, (
        f"Exactly one agent comment expected (no double-write); got {len(agent_comments)}"
    )
    assert agent_comments[0].body == "Here is my reply."

    assert len(broadcast_calls) == 1
    assert broadcast_calls[0]["id"] == written[0].id

    assert result is not None
    assert result.id == written[0].id


@pytest.mark.asyncio
async def test_works_with_no_mirror_session(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """app=None must not raise — dispatch works without a live Mirror session."""
    item = _item()
    thread = list_comments(item.id)

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        append_comment(item.id, role="agent", by="tars", body="Session-independent reply.")
        return DispatchResult(session_id="ctl-2", saw_assistant_text=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )

    broadcast_calls: list[Any] = []

    async def _fake_broadcast(app: Any, event_type: str, *, item_id: str, comment: dict) -> None:
        broadcast_calls.append((app, comment))

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.broadcast_agenda_comment_event",
        _fake_broadcast,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)
    assert result is not None
    assert len(broadcast_calls) == 1
    assert broadcast_calls[0][0] is None


@pytest.mark.asyncio
async def test_dispatcher_error_returns_none(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    thread = list_comments(item.id)

    async def _boom(prompt: str, **kwargs: Any) -> DispatchResult:
        raise DispatcherError("daemon not running")

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _boom,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)
    assert result is None


@pytest.mark.asyncio
async def test_timed_out_returns_none(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    thread = list_comments(item.id)

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        return DispatchResult(session_id="ctl-3", timed_out=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)
    assert result is None


@pytest.mark.asyncio
async def test_no_real_log_writes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify zero writes to real tesseract/logs/** during dispatch."""
    from tesseract.paths import TESSERACT_HOME as live_home

    real_agenda_dir = live_home / "agenda"
    before_files = set(real_agenda_dir.rglob("*")) if real_agenda_dir.exists() else set()

    item = _item()
    thread = list_comments(item.id)

    async def _fake_dispatch(prompt: str, **kwargs: Any) -> DispatchResult:
        append_comment(item.id, role="agent", by="tars", body="ok")
        return DispatchResult(session_id="ctl-4", saw_assistant_text=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.broadcast_agenda_comment_event",
        lambda *a, **kw: None,
    )

    await dispatch_agenda_reply(None, item=item, thread=thread)

    after_files = set(real_agenda_dir.rglob("*")) if real_agenda_dir.exists() else set()
    new_files = after_files - before_files
    assert not new_files, f"Real agenda dir must not be touched by tests; new files: {new_files}"
