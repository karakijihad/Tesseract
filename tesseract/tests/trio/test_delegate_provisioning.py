"""W1 — delegate spawn paths get best-effort MCP hub provisioning
(`run_delegate` + `delegate_codex_exec`), closing the Deferred
"delegate_claude/delegate_codex MCP provisioning" item."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tesseract.kernel.tools import _delegate_runner
from tesseract.kernel.tools.base import ToolContext


def test_provision_delegate_mcp_calls_provision(tmp_path, monkeypatch, mcp_cfg):
    calls = []
    monkeypatch.setattr(
        "tesseract.orchestrator.tars_controller.lanes.mcp_provision.provision",
        lambda working_dir, kind, cfg: calls.append((working_dir, kind)),
    )
    monkeypatch.setattr(
        "tesseract.config.mcp.load_mcp_config", lambda: mcp_cfg
    )
    asyncio.run(_delegate_runner.provision_delegate_mcp("codex", str(tmp_path)))
    assert calls == [(tmp_path, "codex")]


def test_provision_delegate_mcp_swallows_failures(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no token env")

    monkeypatch.setattr(
        "tesseract.orchestrator.tars_controller.lanes.mcp_provision.provision", _boom
    )
    # Must not raise — a dead hub connection never blocks the delegation.
    asyncio.run(_delegate_runner.provision_delegate_mcp("claude", str(tmp_path)))


def _context(tmp_path) -> ToolContext:
    return ToolContext(workspace_root=str(tmp_path), session_id="s-trio-test")


def test_run_delegate_provisions_before_spawn(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(
        _delegate_runner, "provision_delegate_mcp",
        lambda kind, root: _record(order, ("provision", kind)),
    )
    monkeypatch.setattr(
        _delegate_runner, "run_delegate_foreground",
        lambda **kw: _record(order, ("spawn", kw["tool_name"]), result=SimpleNamespace(output="ok")),
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools._terminal_handoff_guard.requires_terminal",
        lambda paths: False,
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_claude._cli_disabled_reason",
        lambda provider: None,
    )
    tool_input = SimpleNamespace(
        task="do it", timeout=5, target_paths=None, background=False
    )
    asyncio.run(
        _delegate_runner.run_delegate(
            tool_name="delegate_codex",
            cli_label="codex",
            provider="codex",
            build_argv=lambda: ("codex", "exec", "do it"),
            env={},
            tool_input=tool_input,
            context=_context(tmp_path),
        )
    )
    assert order == [("provision", "codex"), ("spawn", "delegate_codex")]


async def _record(order, item, result=None):
    order.append(item)
    return result


def test_delegate_codex_exec_provisions(tmp_path, monkeypatch):
    from tesseract.kernel.tools.delegate_codex_exec import (
        DelegateCodexExecInput,
        DelegateCodexExecTool,
    )

    provisioned = []
    monkeypatch.setattr(
        _delegate_runner, "provision_delegate_mcp",
        lambda kind, root: _record(provisioned, (kind, root)),
    )

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"codex says hi", b""

    async def _fake_spawn(*argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

    tool = DelegateCodexExecTool()
    result = asyncio.run(
        tool.run(DelegateCodexExecInput(prompt="audit this"), _context(tmp_path))
    )
    assert not result.is_error
    assert provisioned == [("codex", str(tmp_path))]
