"""Stage 10 — agent_create headless quarantine proposals.

Unattended `agent_create` proceeds through the decide.py carve-out
(`headless_quarantine_write`), writes ONLY to `agents/pending/`, and files
a durable `agent_approval` Workspace event (the operator's proposal card).
Guards pinned here: the headless pending cap, rejected-name dedup, and
fail-soft event emission (the pending write is canonical; a card failure
must not lose the proposal file).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import tesseract.kernel.tools.agent_create as agent_create_module
from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.agent_create import AgentCreateTool
from tesseract.kernel.tools.base import ToolContext
from tesseract.workspace_events import EventStore

_MODELS_CFG = {"roles": {"agents_default": {}}}

_INPUT = {
    "name": "doe-specialist",
    "model_role": "agents_default",
    "description": "John Doe test specialist",
    "role_body": "You are a fixture.",
    "prompt_sections": {"Check Prompt": "Verify the fixture."},
    "rationale": "Recurring fixture-shaped gap in tests.",
    "proposer": "entity",
}


def _run(tool: AgentCreateTool, ask_fn=None, tool_input: dict | None = None):
    registry = ToolRegistry()
    registry.register(tool)
    return asyncio.run(execute_tool(
        registry=registry,
        tool_name="agent_create",
        tool_input=tool_input or dict(_INPUT),
        context=ToolContext(workspace_root=".", session_id="stage10-test"),
        ask_fn=ask_fn,
        policy=None,
    ))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    store = EventStore(tmp_path / "logs")
    tool = AgentCreateTool(
        agents_dir=agents_dir, models_config=_MODELS_CFG, event_store=store,
    )
    return agents_dir, store, tool


def _proposal_events(store: EventStore):
    return store.list_events(kinds=("agent_approval",))


def test_headless_creates_pending_and_files_proposal(env) -> None:
    agents_dir, store, tool = env
    result = _run(tool)

    assert not result.is_error, result.output
    assert (agents_dir / "pending" / "doe-specialist.md").exists()
    assert not (agents_dir / "doe-specialist.md").exists()

    events = _proposal_events(store)
    assert len(events) == 1
    ev = events[0]
    assert ev.status == "pending"
    assert ev.source == "tars"
    assert ev.payload["name"] == "doe-specialist"
    assert ev.payload["model_role"] == "agents_default"
    assert ev.payload["proposer"] == "entity"
    assert ev.payload["rationale"] == _INPUT["rationale"]
    assert ev.payload["session_id"] == "stage10-test"
    assert ev.payload["rendered_markdown"].startswith("---")
    assert ev.event_id in result.output


def test_attended_create_also_files_proposal(env) -> None:
    agents_dir, store, tool = env

    async def approve(t, validated, context):
        return True

    result = _run(tool, ask_fn=approve)
    assert not result.is_error
    assert (agents_dir / "pending" / "doe-specialist.md").exists()
    assert len(_proposal_events(store)) == 1


def test_headless_cap_blocks(env, monkeypatch) -> None:
    agents_dir, store, tool = env
    monkeypatch.setattr(agent_create_module, "load_agent_pending_cap", lambda _p: 2)
    pending = agents_dir / "pending"
    pending.mkdir()
    (pending / "one.md").write_text("x", encoding="utf-8")
    (pending / "two.md").write_text("x", encoding="utf-8")

    result = _run(tool)
    assert result.is_error
    assert "cap" in result.output
    assert not (pending / "doe-specialist.md").exists()
    assert _proposal_events(store) == []


def test_attended_create_not_capped(env, monkeypatch) -> None:
    agents_dir, store, tool = env
    monkeypatch.setattr(agent_create_module, "load_agent_pending_cap", lambda _p: 1)
    pending = agents_dir / "pending"
    pending.mkdir()
    (pending / "one.md").write_text("x", encoding="utf-8")

    async def approve(t, validated, context):
        return True

    result = _run(tool, ask_fn=approve)
    assert not result.is_error
    assert (pending / "doe-specialist.md").exists()


def test_rejected_name_blocked_with_reason(env) -> None:
    agents_dir, store, tool = env
    rejected = agents_dir / "rejected"
    rejected.mkdir()
    (rejected / "doe-specialist.md").write_text("x", encoding="utf-8")
    (rejected / "doe-specialist.reason.txt").write_text(
        "too niche for the fleet", encoding="utf-8",
    )

    result = _run(tool)
    assert result.is_error
    assert "too niche for the fleet" in result.output
    assert not (agents_dir / "pending" / "doe-specialist.md").exists()
    assert _proposal_events(store) == []


def test_event_store_failure_is_soft(env, monkeypatch) -> None:
    agents_dir, store, tool = env

    def boom(_event):
        raise OSError("disk full")

    monkeypatch.setattr(store, "append_event", boom)
    result = _run(tool)
    assert not result.is_error
    assert "WARNING" in result.output
    assert (agents_dir / "pending" / "doe-specialist.md").exists()
