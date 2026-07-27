"""Tests for tesseract.brain.agent_factory.

Contract: unknown/missing agents raise AgentBuildError.
"""

from __future__ import annotations

import pytest

from tesseract.brain.agent_factory import AgentBuildError, build_agent_session
from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.base import ToolContext


class _StubAdapter(ModelAdapter):
    async def stream(self, messages, tools=None, options=None):
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

    def count_tokens(self, messages) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def test_unknown_agent_raises(tmp_path):
    with pytest.raises(AgentBuildError, match="Unknown agent"):
        build_agent_session(
            name="nope",
            agents_dir=tmp_path,
            parent_adapter=_StubAdapter(),
            parent_options=AdapterOptions(),
            parent_registry=ToolRegistry(),
            max_tool_iterations=8,
            max_consecutive_adapter_errors=3,
            tool_context=None,
            policy=None,
            ask_fn=None,
        )


def test_disabled_agent_raises(tmp_path):
    """A disabled agent must raise AgentBuildError before constructing a session."""
    agent_file = tmp_path / "myagent.md"
    agent_file.write_text(
        "---\nname: myagent\nmodel_role: chat_brain\ndisabled: true\n---\n## Role\n\nTester.\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentBuildError, match="disabled"):
        build_agent_session(
            name="myagent",
            agents_dir=tmp_path,
            parent_adapter=_StubAdapter(),
            parent_options=AdapterOptions(),
            parent_registry=ToolRegistry(),
            max_tool_iterations=8,
            max_consecutive_adapter_errors=3,
            tool_context=None,
            policy=None,
            ask_fn=None,
        )


def test_cli_role_agent_raises(tmp_path):
    """An agent with a CLI-role model_role must raise AgentBuildError."""
    agent_file = tmp_path / "cliagent.md"
    agent_file.write_text(
        "---\nname: cliagent\nmodel_role: claude_cli\n---\n## Role\n\nCLI tester.\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentBuildError, match="CLI"):
        build_agent_session(
            name="cliagent",
            agents_dir=tmp_path,
            parent_adapter=_StubAdapter(),
            parent_options=AdapterOptions(),
            parent_registry=ToolRegistry(),
            max_tool_iterations=8,
            max_consecutive_adapter_errors=3,
            tool_context=None,
            policy=None,
            ask_fn=None,
        )


def _write_chat_agent(tmp_path):
    agent_file = tmp_path / "helper.md"
    agent_file.write_text(
        "---\nname: helper\nmodel_role: chat_brain\n---\n## Role\n\nHelper.\n",
        encoding="utf-8",
    )


def test_build_does_not_overwrite_parent_registries(tmp_path):
    """Codex audit C-2: building an agent session must NOT replace the parent
    ToolContext's spawns / interactive_sessions.

    The nested ChatSession's __post_init__ assigns its own fresh registries
    onto its tool_context. If the factory hands it the parent context, those
    assignments clobber the parent's registries — orphaning any handle the
    caller already registered. The factory must isolate the child context.
    """
    _write_chat_agent(tmp_path)

    parent_spawns = object()
    parent_sessions = object()
    parent_ctx = ToolContext()
    parent_ctx.spawns = parent_spawns
    parent_ctx.interactive_sessions = parent_sessions

    session = build_agent_session(
        name="helper",
        agents_dir=tmp_path,
        parent_adapter=_StubAdapter(),
        parent_options=AdapterOptions(),
        parent_registry=ToolRegistry(),
        max_tool_iterations=8,
        max_consecutive_adapter_errors=3,
        tool_context=parent_ctx,
        policy=None,
        ask_fn=None,
    )

    assert parent_ctx.spawns is parent_spawns
    assert parent_ctx.interactive_sessions is parent_sessions
    # The child session got its OWN registries, distinct from the parent's.
    assert session.tool_context is not parent_ctx
    assert session.tool_context.spawns is not parent_spawns
    assert session.tool_context.interactive_sessions is not parent_sessions


def test_build_shares_parent_plumbing_fields(tmp_path):
    """The isolated child context must still carry the parent's plumbing
    (ask_fn, workspace_root, session_id, pty_dispatcher) so the sub-agent
    runs in the same operational context — only the per-session registries
    are private."""
    _write_chat_agent(tmp_path)

    async def _ask(_tool, _inp, _ctx):  # pragma: no cover - identity check only
        return True

    parent_ctx = ToolContext(
        workspace_root="/some/root",
        session_id="sess-helper",
    )
    parent_ctx.ask_fn = _ask

    session = build_agent_session(
        name="helper",
        agents_dir=tmp_path,
        parent_adapter=_StubAdapter(),
        parent_options=AdapterOptions(),
        parent_registry=ToolRegistry(),
        max_tool_iterations=8,
        max_consecutive_adapter_errors=3,
        tool_context=parent_ctx,
        policy=None,
        ask_fn=None,
    )

    child = session.tool_context
    assert child.workspace_root == "/some/root"
    assert child.session_id == "sess-helper"
    assert child.ask_fn is _ask


def test_child_inherits_spawn_max_concurrent(tmp_path):
    """M5: a sub-agent session must inherit the parent's concurrent-spawn cap
    (published onto the context), not run with an uncapped registry."""
    _write_chat_agent(tmp_path)

    parent_ctx = ToolContext(workspace_root="/root", session_id="sess-cap")
    parent_ctx.spawn_max_concurrent = 5  # published by the parent ChatSession

    session = build_agent_session(
        name="helper",
        agents_dir=tmp_path,
        parent_adapter=_StubAdapter(),
        parent_options=AdapterOptions(),
        parent_registry=ToolRegistry(),
        max_tool_iterations=8,
        max_consecutive_adapter_errors=3,
        tool_context=parent_ctx,
        policy=None,
        ask_fn=None,
    )

    assert session.spawn_max_concurrent == 5
    assert session.spawns.max_concurrent == 5
