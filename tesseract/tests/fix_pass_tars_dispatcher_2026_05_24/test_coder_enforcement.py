"""WS-3 — spawned controller session enforces preferred_coder.

Two layers: the pure ``_registry_without`` filter helper, and the
``_build_chat_session`` wiring that uses the filtered registry in BOTH
``registry=`` and ``tool_context.tool_registry_provider`` and appends a
HARD-RULE directive to the per-turn prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import Tool
from tesseract.orchestrator.tars_controller.sessions import ControllerSessionRecord
from tesseract.scripts.tars_controller import ControllerRuntime, _registry_without


class _StubTool(Tool):
    default_posture = "auto"
    risk_class = "autonomous"

    def __init__(self, n: str) -> None:
        self._n = n

    @property
    def name(self) -> str:
        return self._n

    @property
    def description(self) -> str:
        return self._n

    @property
    def input_schema(self) -> type[BaseModel]:
        return BaseModel

    async def run(self, tool_input, context):  # pragma: no cover
        ...


def test_registry_without_excludes_named_tool() -> None:
    reg = ToolRegistry()
    for n in ("delegate_claude", "delegate_codex", "grep"):
        reg.register(_StubTool(n))
    filtered = _registry_without(reg, {"delegate_codex"})
    assert "delegate_codex" not in filtered.tools
    assert "delegate_claude" in filtered.tools
    assert "grep" in filtered.tools


def test_registry_without_returns_same_when_nothing_excluded() -> None:
    reg = ToolRegistry()
    reg.register(_StubTool("grep"))
    assert _registry_without(reg, set()) is reg  # identity: no copy when no-op


class _FakeAdapter:
    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _FakePolicy:
    pass


class _FakeDaemon:
    async def request_permission(self, *_a: Any, **_k: Any) -> bool:
        return False

    async def append_event(self, *_a: Any, **_k: Any) -> int:
        return 0


class _CapturedChatSession:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _CapturedChatSession.last_kwargs = kwargs


def _runtime_with_delegate_tools() -> ControllerRuntime:
    runtime = ControllerRuntime()
    runtime.adapter = _FakeAdapter()
    reg = ToolRegistry()
    for n in ("delegate_claude", "delegate_codex", "grep"):
        reg.register(_StubTool(n))
    runtime.tool_registry = reg
    runtime.system_prompt = "manifest"
    runtime.tool_iteration_cap = 7
    runtime.consecutive_error_cap = 11
    runtime.policy = _FakePolicy()
    return runtime


def test_build_chat_session_filters_opposing_tool_and_adds_directive(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tesseract.brain.chat as _chat_mod

    monkeypatch.setattr(_chat_mod, "ChatSession", _CapturedChatSession)
    runtime = _runtime_with_delegate_tools()
    record = ControllerSessionRecord(
        session_id="sess-ctrl-claude",
        mode="chat",
        origin="cli",
        transcript_path=str(isolated_home / "transcript.jsonl"),
        preferred_coder="claude",
    )

    runtime._build_chat_session(record, _FakeDaemon())  # type: ignore[arg-type]

    kw = _CapturedChatSession.last_kwargs
    # The opposing tool is gone from the session registry AND the provider.
    assert "delegate_codex" not in kw["registry"].tools
    assert "delegate_claude" in kw["registry"].tools
    assert "delegate_codex" not in kw["tool_context"].tool_registry_provider().tools
    # The directive names the allowed coder and forbids the other.
    prompt = kw["prompt_builder"]()
    assert "delegate_claude" in prompt
    assert "delegate_codex" in prompt


def test_build_chat_session_unconstrained_keeps_both_tools(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tesseract.brain.chat as _chat_mod

    monkeypatch.setattr(_chat_mod, "ChatSession", _CapturedChatSession)
    runtime = _runtime_with_delegate_tools()
    record = ControllerSessionRecord(
        session_id="sess-ctrl-free",
        mode="chat",
        origin="cli",
        transcript_path=str(isolated_home / "transcript.jsonl"),
    )

    runtime._build_chat_session(record, _FakeDaemon())  # type: ignore[arg-type]

    kw = _CapturedChatSession.last_kwargs
    # No constraint → identical registry object, both delegate tools present.
    assert kw["registry"] is runtime.tool_registry
    assert "delegate_claude" in kw["registry"].tools
    assert "delegate_codex" in kw["registry"].tools
