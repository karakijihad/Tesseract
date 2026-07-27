"""Audit C2 regression — Mirror's tool registry must register
`InvokeAgentTool` with the parent permission policy so nested
sub-agent sessions inherit it.

Before 2026-04-29, `tesseract/brain/boot.py:780` constructed
`InvokeAgentTool(...)` without passing `policy=`. `tars_repl.py` passed
it correctly. The asymmetry meant Mirror sub-agents (terminal-operator,
vault-librarian) ran with `self._policy is None`, so their nested
`ChatSession` had no policy at all and `execute_tool` skipped the
operator-config layer entirely.

Tests construct InvokeAgentTool directly with a stub adapter so they
run in CI without an API key (W1 reviewer follow-up).
"""

from __future__ import annotations

from pathlib import Path

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.invoke_agent import InvokeAgentTool
from tesseract.permissions.policy import PermissionPolicy


def _stub_policy() -> PermissionPolicy:
    return PermissionPolicy(
        tools_defaults={"file_write": "ask"},
        modes={"max": {"overrides": {}}},
        path_overrides={},
        current_mode="max",
        workspace_root=".",
    )


class _StubAdapter(ModelAdapter):
    """Minimal ModelAdapter that never streams. Construction-only — no
    network, no test resources. Used to verify wiring invariants without
    requiring an API key in the env."""

    async def stream(self, messages, tools=None, options=None):
        if False:  # pragma: no cover — never actually iterated in these tests
            yield  # type: ignore[unreachable]

    def count_tokens(self, messages):
        return 0

    async def check_available(self):
        return True


def test_invoke_agent_constructed_with_policy(tmp_path: Path) -> None:
    """Mirror-equivalent construction must thread policy into the tool."""
    policy = _stub_policy()
    tool = InvokeAgentTool(
        agents_dir=tmp_path,
        adapter=_StubAdapter(),
        options=AdapterOptions(),
        parent_registry=ToolRegistry(),
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        policy=policy,
    )
    assert tool._policy is policy  # type: ignore[attr-defined]


def test_invoke_agent_constructed_without_policy(tmp_path: Path) -> None:
    """Back-compat: omitting policy preserves None (REPL re-registers
    later with its own ask_fn)."""
    tool = InvokeAgentTool(
        agents_dir=tmp_path,
        adapter=_StubAdapter(),
        options=AdapterOptions(),
        parent_registry=ToolRegistry(),
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
    )
    assert tool._policy is None  # type: ignore[attr-defined]


def test_build_tool_registry_passes_policy_through() -> None:
    """End-to-end check: build_tool_registry(policy=policy) must result
    in InvokeAgentTool._policy is policy. Skips if invoke_agent isn't
    registered (no chat_adapter — env without API key)."""
    from tesseract.brain.boot import build_tool_registry

    policy = _stub_policy()
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry(policy=policy)
    invoke = registry.tools.get("invoke_agent")
    if invoke is None:
        import pytest

        pytest.skip("invoke_agent not registered (no chat adapter — env without API key)")
    assert invoke._policy is policy  # type: ignore[attr-defined]
