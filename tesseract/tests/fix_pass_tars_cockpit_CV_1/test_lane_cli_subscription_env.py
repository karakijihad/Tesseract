"""CV-1 follow-up — CLI lanes must use subscription auth, never the API key.

A `claude`/`codex` lane spawns the CLI, which silently prefers
ANTHROPIC_API_KEY / OPENAI_API_KEY (loaded into the backend env from .env for
the SDK adapter path) over its OAuth subscription login — billing API credit
instead of the operator's plan. The lane adapters must strip those keys
(same discipline as delegate_claude / delegate_codex)."""

from __future__ import annotations

import pytest

from tesseract.orchestrator.tars_controller.interactive import cli_adapter
from tesseract.orchestrator.tars_controller.interactive.cli_adapter import (
    ClaudeStreamAdapter,
    CodexStreamAdapter,
    _claude_spawn,
    _codex_spawn,
)


def test_default_spawns_are_subscription_scoped():
    assert ClaudeStreamAdapter()._spawn is _claude_spawn  # noqa: SLF001
    assert CodexStreamAdapter()._spawn is _codex_spawn  # noqa: SLF001


@pytest.mark.asyncio
async def test_claude_spawn_strips_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-be-stripped")
    captured: dict = {}

    async def _capture(argv, cwd, env=None):
        captured["env"] = env
        return object()

    monkeypatch.setattr(cli_adapter, "_default_spawn", _capture)
    await _claude_spawn(["claude", "-p", "hi"], ".")
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in captured["env"]


@pytest.mark.asyncio
async def test_codex_spawn_strips_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-stripped")
    captured: dict = {}

    async def _capture(argv, cwd, env=None):
        captured["env"] = env
        return object()

    monkeypatch.setattr(cli_adapter, "_default_spawn", _capture)
    await _codex_spawn(["codex", "exec", "hi"], ".")
    assert "OPENAI_API_KEY" not in captured["env"]
