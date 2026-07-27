"""Audit M6 regression — `agent_create.check_permissions` must return ASK
unconditionally, so `permissions.yaml` headless overrides
(`agent_create: auto`) never reach the policy layer.

Before 2026-04-29, `check_permissions` returned PASSTHROUGH and the tool
relied entirely on the policy layer; in headless mode the executor's
"no ask_fn → auto-allow" fallthrough could write an agent with no operator
involvement.

Stage 10 amendment (2026-07-16): the headless path now PROCEEDS via the
executor's quarantine-write carve-out (`headless_quarantine_write`
ClassVar, honored from the tool CLASS only) — but writes exclusively to
`agents/pending/`, which the runtime never invokes from (W7-A). The
operator gate moved from the write to ACTIVATION (`agent_promote` / the
Workspace proposal card). What this file still pins:

1. `check_permissions` returns ASK directly — yaml overrides stay inert.
2. Headless writes land in quarantine ONLY; the active set is untouched.
3. Attended sessions still route through the operator's ASK.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.agent_create import AgentCreateInput, AgentCreateTool
from tesseract.kernel.tools.base import PermissionResult, ToolContext
from tesseract.permissions.policy import PermissionPolicy


_MIN_MODELS_CFG = {
    "roles": {
        "chat_brain": {"primary": {"provider": "openai", "model": "gpt-5-mini"}},
    }
}


def _build_tool(agents_dir: Path) -> AgentCreateTool:
    return AgentCreateTool(agents_dir=agents_dir, models_config=_MIN_MODELS_CFG)


def test_check_permissions_returns_ask(tmp_path: Path) -> None:
    tool = _build_tool(tmp_path)
    inp = AgentCreateInput(
        name="test-agent",
        model_role="chat_brain",
        description="x",
        role_body="x",
        prompt_sections={"Body": "x"},
        rationale="x",
    )
    decision = tool.check_permissions(inp, ToolContext(workspace_root=str(tmp_path)))
    assert decision == PermissionResult.ASK


def test_headless_writes_quarantine_only(tmp_path: Path, monkeypatch) -> None:
    """Stage 10: headless agent_create proceeds via the quarantine-write
    carve-out — into agents/pending/ ONLY. The yaml `headless.agent_create:
    auto` override still has no say (check_permissions returns ASK directly;
    policy is consulted only on PASSTHROUGH), and the active set stays
    untouched."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    registry = ToolRegistry()
    registry.register(_build_tool(agents_dir))

    policy = PermissionPolicy(
        tools_defaults={"agent_create": "ask"},
        modes={
            "max": {"overrides": {}},
            "headless": {"overrides": {"agent_create": "auto"}},
        },
        path_overrides={},
        current_mode="headless",
        workspace_root=str(tmp_path),
    )

    result = asyncio.run(execute_tool(
        registry=registry,
        tool_name="agent_create",
        tool_input={
            "name": "test-agent",
            "model_role": "chat_brain",
            "description": "d",
            "role_body": "r",
            "prompt_sections": {"Body": "b"},
            "rationale": "why",
        },
        context=ToolContext(workspace_root=str(tmp_path)),
        ask_fn=None,
        policy=policy,
    ))

    assert not result.is_error, result.output
    assert (agents_dir / "pending" / "test-agent.md").exists()
    assert not (agents_dir / "test-agent.md").exists(), (
        "headless create must never touch the active set"
    )


def test_agent_create_allowed_when_operator_approves(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    registry = ToolRegistry()
    registry.register(_build_tool(agents_dir))

    policy = PermissionPolicy(
        tools_defaults={"agent_create": "ask"},
        modes={"max": {"overrides": {}}},
        path_overrides={},
        current_mode="max",
        workspace_root=str(tmp_path),
    )

    async def approve(tool, validated, context):
        return True

    result = asyncio.run(execute_tool(
        registry=registry,
        tool_name="agent_create",
        tool_input={
            "name": "test-agent",
            "model_role": "chat_brain",
            "description": "d",
            "role_body": "r",
            "prompt_sections": {"Body": "b"},
            "rationale": "why",
        },
        context=ToolContext(workspace_root=str(tmp_path)),
        ask_fn=approve,
        policy=policy,
    ))

    assert not result.is_error
    # W7-A quarantine: agent_create lands the file in agents/pending/,
    # not the active set. agent_promote is the second step.
    written = agents_dir / "pending" / "test-agent.md"
    assert written.exists()
    parsed = yaml.safe_load(written.read_text(encoding="utf-8").split("---", 2)[1])
    assert parsed["name"] == "test-agent"
