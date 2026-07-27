"""Phase 18.5 W7-A — agent_create writes to agents/pending/, not the
active set. invoke_agent cannot reach pending agents until agent_promote
moves them. This is defense in depth around audit M6: even if the ASK
gate is bypassed, generated agents are not callable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from tesseract.agents.loader import list_agents, list_pending_agents, load_agent
from tesseract.kernel.tools.agent_create import AgentCreateInput, AgentCreateTool
from tesseract.kernel.tools.agent_promote import AgentPromoteInput, AgentPromoteTool
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.invoke_agent import InvokeAgentTool


_MODELS = {
    "roles": {
        "chat_brain": {
            "models": [
                {"name": "test-model", "provider": "openai"},
            ],
        },
    },
}


def _seed_agents_dir(tmp_path: Path) -> Path:
    """Create an empty `agents/` with a stub INDEX.md."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "INDEX.md").write_text(
        "| name | model_role | description |\n", encoding="utf-8",
    )
    return agents_dir


async def _create(tool: AgentCreateTool, name: str) -> None:
    inp = AgentCreateInput(
        name=name,
        model_role="chat_brain",
        description="test agent",
        role_body="Stand up and answer.",
        prompt_sections={"Plan": "Say hello and stop."},
        rationale="regression test",
    )
    result = await tool.run(inp, ToolContext(workspace_root="/"))
    assert not result.is_error, result.output


def test_agent_create_writes_to_pending(tmp_path: Path) -> None:
    agents_dir = _seed_agents_dir(tmp_path)
    tool = AgentCreateTool(agents_dir=agents_dir, models_config=_MODELS)
    asyncio.run(_create(tool, "qa-test-bot"))

    pending = agents_dir / "pending" / "qa-test-bot.md"
    active = agents_dir / "qa-test-bot.md"
    assert pending.exists(), "expected agent in agents/pending/"
    assert not active.exists(), "agent must NOT land in active set on create"


def test_create_does_not_touch_index_md(tmp_path: Path) -> None:
    agents_dir = _seed_agents_dir(tmp_path)
    tool = AgentCreateTool(agents_dir=agents_dir, models_config=_MODELS)
    before = (agents_dir / "INDEX.md").read_text(encoding="utf-8")
    asyncio.run(_create(tool, "qa-test-bot"))
    after = (agents_dir / "INDEX.md").read_text(encoding="utf-8")
    assert before == after, "INDEX.md must only be appended on promote"


def test_list_agents_default_excludes_pending(tmp_path: Path) -> None:
    agents_dir = _seed_agents_dir(tmp_path)
    tool = AgentCreateTool(agents_dir=agents_dir, models_config=_MODELS)
    asyncio.run(_create(tool, "qa-test-bot"))

    assert "qa-test-bot" not in list_agents(agents_dir)
    assert "qa-test-bot" in list_pending_agents(agents_dir)
    assert "qa-test-bot" in list_agents(agents_dir, include_pending=True)


def test_invoke_agent_cannot_load_pending(tmp_path: Path) -> None:
    """invoke_agent uses the default `load_agent`, which excludes pending.
    A pending agent must surface a 'not found' error, not get invoked."""
    agents_dir = _seed_agents_dir(tmp_path)
    create_tool = AgentCreateTool(agents_dir=agents_dir, models_config=_MODELS)
    asyncio.run(_create(create_tool, "qa-test-bot"))

    with pytest.raises(FileNotFoundError):
        load_agent("qa-test-bot", agents_dir=agents_dir)


def test_promote_moves_pending_to_active_and_appends_index(tmp_path: Path) -> None:
    agents_dir = _seed_agents_dir(tmp_path)
    create_tool = AgentCreateTool(agents_dir=agents_dir, models_config=_MODELS)
    promote_tool = AgentPromoteTool(agents_dir=agents_dir)
    asyncio.run(_create(create_tool, "qa-test-bot"))

    result = asyncio.run(promote_tool.run(
        AgentPromoteInput(name="qa-test-bot"),
        ToolContext(workspace_root="/"),
    ))
    assert not result.is_error, result.output

    assert (agents_dir / "qa-test-bot.md").exists()
    assert not (agents_dir / "pending" / "qa-test-bot.md").exists()
    assert "qa-test-bot" in list_agents(agents_dir)
    assert "qa-test-bot" not in list_pending_agents(agents_dir)

    index = (agents_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "qa-test-bot" in index


def test_promote_unknown_agent_errors(tmp_path: Path) -> None:
    agents_dir = _seed_agents_dir(tmp_path)
    promote_tool = AgentPromoteTool(agents_dir=agents_dir)
    result = asyncio.run(promote_tool.run(
        AgentPromoteInput(name="nope-bot"),
        ToolContext(workspace_root="/"),
    ))
    assert result.is_error
    assert "no pending" in result.output.lower()


def test_builtins_reachable_after_seeding_relocated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex follow-up (2026-04-29) established that built-in agents must
    remain reachable when TESSERACT_HOME is relocated. Distributable-app
    Task 3 replaced the loader's runtime code-tree fallback with mandatory
    one-time seeding via `config_seed.ensure_agents_seeded()` — the single
    source of truth is now TESSERACT_HOME/agents, populated once at boot."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.config_seed import ensure_agents_seeded

    ensure_agents_seeded()
    agents_dir = tmp_path / "agents"

    names = set(list_agents(agents_dir))
    assert "observer" in names, f"built-in observer missing after seed: {names}"
    assert "vault-lint" in names, f"built-in vault-lint missing after seed: {names}"

    agent = load_agent("observer", agents_dir=agents_dir)
    assert agent.name == "observer"


def test_loader_does_not_fall_back_to_code_tree(tmp_path: Path) -> None:
    """A card missing from an (unseeded) home agents dir must raise, not
    silently resolve from the code tree — a dual-source lookup would make
    it impossible to reason about which card is live."""
    agents_dir = _seed_agents_dir(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_agent("observer", agents_dir=agents_dir)
    assert "observer" not in set(list_agents(agents_dir))


def test_user_agent_shadows_builtin_on_collision(tmp_path: Path) -> None:
    """Primary wins on stem collision so an operator-created agent
    named after a built-in overrides it (without mutating the source)."""
    agents_dir = _seed_agents_dir(tmp_path)
    # Write a fake "observer.md" into the user-state directory.
    (agents_dir / "observer.md").write_text(
        "---\nname: observer\nmodel_role: chat_brain\ndescription: shadow\n---\n## Role\nshadowed.\n",
        encoding="utf-8",
    )
    agent = load_agent("observer", agents_dir=agents_dir)
    # The user-state version's body distinguishes it from the built-in.
    assert "shadowed" in agent.get_section("Role").lower()


def test_create_rejects_duplicate_pending(tmp_path: Path) -> None:
    agents_dir = _seed_agents_dir(tmp_path)
    create_tool = AgentCreateTool(agents_dir=agents_dir, models_config=_MODELS)
    asyncio.run(_create(create_tool, "qa-test-bot"))

    inp = AgentCreateInput(
        name="qa-test-bot",
        model_role="chat_brain",
        description="duplicate",
        role_body="role",
        prompt_sections={"Plan": "plan"},
        rationale="dup",
    )
    result = asyncio.run(create_tool.run(inp, ToolContext(workspace_root="/")))
    assert result.is_error
    assert "pending" in result.output.lower()
