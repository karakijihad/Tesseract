"""Agenda comment auto-reply — operator posts a comment, TARS answers.

Problem (operator, 2026-05-24): commenting on an awaiting-approval agenda
item produced no reply. Of every comment surface, only agenda comments
lacked auto-reply (workspace comments + operator posts + telegram already
dispatch a TARS turn). This wires the gap: an operator comment fires a
background dispatch to a fresh controller session which calls the
``agenda_comment`` tool to write its ``role="agent"`` reply durably; the
backend then detects the new comment and broadcasts it (Option-B
durability, matches ``workspace_reply_dispatch.py``).

Design (operator-chosen 2026-05-24): every operator comment triggers a
reply; replies run in parallel (no per-thread serialization); the brain
is a fresh controller session via ``dispatch_to_controller``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.agenda_comments import (
    AgendaComment,
    append_comment,
    list_comments,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.tars_controller.dispatcher import (
    DispatcherError,
    DispatchResult,
)

from tesseract.orchestrator.autonomy.agenda_reply import (
    AgendaReplyConfig,
    build_agenda_reply_prompt,
    dispatch_agenda_reply,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _item() -> AgendaItem:
    when = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
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


# ── config ────────────────────────────────────────────────────────────


def test_config_defaults_when_block_missing() -> None:
    cfg = AgendaReplyConfig.from_yaml_block(None)
    assert cfg.enabled is True
    assert cfg.idle_timeout_seconds > 0


def test_config_parses_enabled_and_timeout() -> None:
    cfg = AgendaReplyConfig.from_yaml_block(
        {"enabled": False, "idle_timeout_seconds": 240}
    )
    assert cfg.enabled is False
    assert cfg.idle_timeout_seconds == 240.0


def test_config_tolerates_bad_types() -> None:
    cfg = AgendaReplyConfig.from_yaml_block(
        {"enabled": "yes", "idle_timeout_seconds": "soon"}
    )
    # Bad values fall back to defaults rather than raising at load.
    assert cfg.enabled is True
    assert cfg.idle_timeout_seconds > 0


# ── prompt builder ──────────────────────────────────────────────────────


def test_prompt_includes_goal_and_latest_operator_comment(isolated_home: Path) -> None:
    item = _item()
    append_comment(item.id, role="operator", by="sess_op", body="so what should we do here?")
    thread = list_comments(item.id)
    prompt = build_agenda_reply_prompt(item, thread)
    assert item.goal in prompt
    assert "so what should we do here?" in prompt


# ── dispatch ────────────────────────────────────────────────────────────


async def test_dispatch_broadcasts_tool_written_comment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controller calls `agenda_comment` (simulated here by writing directly
    inside the fake dispatch, same as the tool would); dispatch_agenda_reply
    must detect + broadcast it without writing a second comment itself."""
    item = _item()
    append_comment(item.id, role="operator", by="sess_op", body="what should we do?")
    thread = list_comments(item.id)

    captured_prompt: dict[str, str] = {}
    written: list[AgendaComment] = []

    async def _fake_dispatch(prompt, **kwargs):
        captured_prompt["text"] = prompt
        c = append_comment(
            item.id, role="agent", by="tars",
            body="Recommend we approve — the cluster is low-risk.",
        )
        written.append(c)
        return DispatchResult(session_id="ctl-1", saw_assistant_text=True)

    broadcasts: list[dict] = []

    async def _fake_broadcast(app, event_type, *, item_id, comment):
        broadcasts.append({"event_type": event_type, "item_id": item_id, "comment": comment})

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _fake_dispatch,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.broadcast_agenda_comment_event",
        _fake_broadcast,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)

    assert result is not None
    assert result.id == written[0].id
    assert result.role == "agent"
    assert result.by == "tars"

    # Exactly one agent comment in the store (controller wrote it via the
    # tool; dispatch_agenda_reply did not write a second one).
    thread_after = list_comments(item.id)
    agent_comments = [c for c in thread_after if c.role == "agent"]
    assert len(agent_comments) == 1

    # Broadcast fired with the tool-written comment.
    assert len(broadcasts) == 1
    assert broadcasts[0]["event_type"] == "agenda_comment_added"
    assert broadcasts[0]["item_id"] == item.id
    assert broadcasts[0]["comment"]["id"] == written[0].id

    # Prompt carried the operator's question + the MUST-call directive with
    # the exact item_id.
    assert "what should we do?" in captured_prompt["text"]
    assert "agenda_comment" in captured_prompt["text"]
    assert f'item_id = "{item.id}"' in captured_prompt["text"]


async def test_dispatch_never_cold_spawns_daemon(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor owns the controller daemon lifecycle. A web-triggered
    reply must pass ``spawn_if_missing=False`` so it never forks a daemon
    subprocess from an HTTP handler (and tests stay subprocess-free)."""
    item = _item()
    append_comment(item.id, role="operator", by="sess_op", body="ping?")
    thread = list_comments(item.id)

    seen: dict[str, object] = {}

    async def _capture(prompt, **kwargs):
        seen.update(kwargs)
        append_comment(item.id, role="agent", by="tars", body="pong")
        return DispatchResult(session_id="ctl-x", saw_assistant_text=True)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _capture,
    )
    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.broadcast_agenda_comment_event",
        lambda *a, **k: _noop(),
    )

    await dispatch_agenda_reply(None, item=item, thread=thread)
    assert seen.get("spawn_if_missing") is False


async def _noop() -> None:
    return None


async def test_dispatch_returns_none_on_dispatcher_error(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _item()
    append_comment(item.id, role="operator", by="sess_op", body="hello?")
    thread = list_comments(item.id)

    async def _boom(prompt, **kwargs):
        raise DispatcherError("daemon unreachable")

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _boom,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)
    assert result is None
    # No agent comment was written.
    assert all(c.role == "operator" for c in list_comments(item.id))


async def test_dispatch_returns_none_when_controller_never_calls_tool(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Controller completes without calling `agenda_comment` (e.g. it just
    chatted instead) — no new agent comment appears, so dispatch must
    return None rather than fabricate a reply from assistant_text."""
    item = _item()
    append_comment(item.id, role="operator", by="sess_op", body="anything?")
    thread = list_comments(item.id)

    async def _no_tool_call(prompt, **kwargs):
        return DispatchResult(session_id="ctl-2", saw_assistant_text=False)

    monkeypatch.setattr(
        "tesseract.orchestrator.autonomy.agenda_reply.dispatch_to_controller",
        _no_tool_call,
    )

    result = await dispatch_agenda_reply(None, item=item, thread=thread)
    assert result is None
    assert all(c.role == "operator" for c in list_comments(item.id))
