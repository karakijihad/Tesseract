"""Stage 10 — shared promotion core + proposal-card settle.

`promote_pending_agent` is the single implementation behind BOTH the
`agent_promote` chat tool and the Workspace card's approve route: validate
the pending file parses, move it into the active set, append the INDEX row,
roll the move back if the INDEX write fails. The chat tool additionally
settles any open `agent_approval` card for the promoted name so the two
approval surfaces converge on one state.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel

from tesseract.kernel.tools import agent_promote as agent_promote_module
from tesseract.kernel.tools.agent_promote import (
    AgentPromoteInput,
    AgentPromoteTool,
    promote_pending_agent,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.workspace_events import EventStore, WorkspaceEvent

_AGENT_MD = """---
name: doe-specialist
version: "0.1"
model_role: agents_default
description: John Doe fixture
---

## Role

Fixture stance.
"""


def _seed_pending(agents_dir: Path, name: str = "doe-specialist") -> Path:
    pending = agents_dir / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{name}.md"
    path.write_text(_AGENT_MD, encoding="utf-8")
    return path


def test_promote_core_happy_path(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    _seed_pending(agents_dir)

    loaded, err = promote_pending_agent(agents_dir, "doe-specialist")

    assert err is None
    assert loaded is not None and loaded.name == "doe-specialist"
    assert (agents_dir / "doe-specialist.md").exists()
    assert not (agents_dir / "pending" / "doe-specialist.md").exists()
    index = (agents_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "| doe-specialist | agents_default |" in index


def test_promote_core_missing_pending(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    loaded, err = promote_pending_agent(agents_dir, "ghost")
    assert loaded is None
    assert err is not None and "ghost" in err


def test_promote_core_index_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    agents_dir = tmp_path / "agents"
    _seed_pending(agents_dir)

    def boom(*args, **kwargs):
        raise OSError("index locked")

    monkeypatch.setattr(agent_promote_module, "_append_index_row", boom)
    loaded, err = promote_pending_agent(agents_dir, "doe-specialist")

    assert loaded is None
    assert err is not None and "rolled back" in err
    assert (agents_dir / "pending" / "doe-specialist.md").exists()
    assert not (agents_dir / "doe-specialist.md").exists()


def _run_tool(tool: AgentPromoteTool, name: str):
    inp = AgentPromoteInput(name=name)

    class _Wrap(BaseModel):
        pass

    return asyncio.run(tool.run(inp, ToolContext(workspace_root=".")))


def test_tool_promote_settles_open_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    agents_dir = tmp_path / "agents"
    _seed_pending(agents_dir)
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="agent_approval",
        source="tars",
        title="Agent proposal: doe-specialist",
        summary="why",
        payload={"name": "doe-specialist"},
    ))

    tool = AgentPromoteTool(agents_dir=agents_dir, event_store=store)
    result = _run_tool(tool, "doe-specialist")

    assert not result.is_error, result.output
    settled = [e for e in store.list_events(kinds=("agent_approval",)) if e.event_id == ev.event_id]
    assert settled and settled[0].status == "applied"


def test_tool_promote_survives_store_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    agents_dir = tmp_path / "agents"
    _seed_pending(agents_dir)
    store = EventStore(tmp_path / "logs")

    def boom(*args, **kwargs):
        raise OSError("store broken")

    monkeypatch.setattr(store, "list_events", boom)
    tool = AgentPromoteTool(agents_dir=agents_dir, event_store=store)
    result = _run_tool(tool, "doe-specialist")

    assert not result.is_error, result.output
    assert (agents_dir / "doe-specialist.md").exists()
