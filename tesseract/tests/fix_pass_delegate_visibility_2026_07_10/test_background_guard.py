"""Fix 2 — foreground delegate requests past the runtime cap are auto-flipped
to background spawns instead of wedging the chat turn (fix-pass 2026-07-10)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tesseract.kernel.tools import _delegate_runner
from tesseract.kernel.tools.base import ToolContext, ToolResult


class FakeRegistry:
    def __init__(self):
        self.registered = []

    def register(self, *, kind, goal, coro):
        self.registered.append((kind, goal))
        coro.close()
        return SimpleNamespace(handle_id="h-test", started_at="2026-07-10T00:00:00Z")


@pytest.fixture
def guards(monkeypatch):
    monkeypatch.setattr(
        "tesseract.kernel.tools._terminal_handoff_guard.requires_terminal",
        lambda paths: False,
    )
    monkeypatch.setattr(
        "tesseract.kernel.tools.delegate_claude._cli_disabled_reason",
        lambda provider: None,
    )

    async def _noop(kind, root):
        return None

    monkeypatch.setattr(_delegate_runner, "provision_delegate_mcp", _noop)


def _run(tool_input, context):
    return asyncio.run(
        _delegate_runner.run_delegate(
            tool_name="delegate_claude",
            cli_label="claude",
            provider="claude",
            build_argv=lambda: ("claude", "-p", "x"),
            env={},
            tool_input=tool_input,
            context=context,
        )
    )


def _context(tmp_path, registry):
    ctx = ToolContext(workspace_root=str(tmp_path), session_id="s-test")
    ctx.spawns = registry
    return ctx


def test_long_foreground_flips_to_background(tmp_path, guards):
    registry = FakeRegistry()
    inp = SimpleNamespace(task="big", timeout=1200, target_paths=None, background=False)
    result = _run(inp, _context(tmp_path, registry))
    assert not result.is_error
    assert "auto-flipped" in result.output
    assert result.metadata["spawn_handle"] == "h-test"
    assert registry.registered == [("delegate_claude", "big")]


def test_short_foreground_stays_foreground(tmp_path, guards, monkeypatch):
    async def _fake_foreground(**kw):
        return ToolResult(output="ran foreground")

    monkeypatch.setattr(_delegate_runner, "run_delegate_foreground", _fake_foreground)
    registry = FakeRegistry()
    inp = SimpleNamespace(task="quick", timeout=60, target_paths=None, background=False)
    result = _run(inp, _context(tmp_path, registry))
    assert result.output == "ran foreground"
    assert registry.registered == []


def test_no_registry_degrades_to_foreground(tmp_path, guards, monkeypatch):
    """Headless contexts (no SpawnRegistry) keep the P3 contract: foreground
    even for long timeouts — there is no operator waiting on the turn."""
    async def _fake_foreground(**kw):
        return ToolResult(output="ran foreground headless")

    monkeypatch.setattr(_delegate_runner, "run_delegate_foreground", _fake_foreground)
    inp = SimpleNamespace(task="big", timeout=1200, target_paths=None, background=False)
    ctx = ToolContext(workspace_root=str(tmp_path), session_id="s-test")
    result = _run(inp, ctx)
    assert result.output == "ran foreground headless"


def test_explicit_background_has_no_flip_note(tmp_path, guards):
    registry = FakeRegistry()
    inp = SimpleNamespace(task="big", timeout=1200, target_paths=None, background=True)
    result = _run(inp, _context(tmp_path, registry))
    assert "auto-flipped" not in result.output
    assert "spawned in background" in result.output


def test_missing_config_key_surfaces_loudly(tmp_path, guards, monkeypatch):
    def _boom(path):
        raise ValueError("runtime.yaml missing 'max_foreground_delegate_timeout_s'")

    monkeypatch.setattr(
        "tesseract.config.runtime_limits.load_max_foreground_delegate_timeout_s",
        _boom,
    )
    registry = FakeRegistry()
    inp = SimpleNamespace(task="x", timeout=1200, target_paths=None, background=False)
    result = _run(inp, _context(tmp_path, registry))
    assert result.is_error
    assert "config error" in result.output
