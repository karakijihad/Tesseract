"""Audit-2 C-2 + C-3: controller chat-turn wiring.

C-2: ``ControllerRuntime.make_dispatch_turn()`` must construct
``ChatSession`` with EVERY required dataclass field, including
``max_consecutive_adapter_errors``. The previous wiring omitted it and
the next real ``user_input`` turn would have raised ``TypeError`` before
the session yielded any text.

C-3: The controller daemon owns a permission-IPC contract
(``request_permission`` → ``approval`` push), but chat sessions were
constructed without ``ask_fn`` or ``policy``. Tools at ASK posture
denied headlessly instead of prompting the operator. The fix routes
the chat brain's ``ask_fn`` to ``daemon.request_permission`` and plumbs
the loaded ``PermissionPolicy`` into the session.

These tests assert the wiring directly so a future refactor that drops
one of the new kwargs surfaces as a red CI line instead of as a
production TypeError.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller.sessions import (
    ControllerSessionRecord,
)
from tesseract.scripts.tars_controller import (
    ControllerRuntime,
    _make_controller_ask_fn,
)


class _FakeAdapter:
    def stream(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _FakeRegistry:
    pass


class _FakePolicy:
    pass


class _CapturedChatSession:
    """Stand-in for ``tesseract.brain.chat.ChatSession`` — records the
    kwargs it was constructed with AND every method the closure calls
    on it. We assert on both so a future refactor that renames
    ``ChatSession.send`` (or that re-introduces the old ``stream_user``
    typo) surfaces here instead of as a runtime AttributeError on the
    operator's first chat turn (the 2026-05-24 bug)."""

    last_kwargs: dict[str, Any] = {}
    methods_called: list[str] = []

    def __init__(self, **kwargs: Any) -> None:
        _CapturedChatSession.last_kwargs = kwargs
        _CapturedChatSession.methods_called = []

    async def send(self, _text: str) -> Any:
        _CapturedChatSession.methods_called.append("send")
        if False:  # pragma: no cover — make this an async generator
            yield None
        return


@pytest.mark.asyncio
async def test_dispatch_turn_passes_all_required_chatsession_kwargs(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C-2 + C-3 together: the closure must pass ``adapter``,
    ``system_prompt``, ``max_tool_iterations``, ``max_consecutive_adapter_errors``,
    ``registry`` (the dataclass field — NOT ``tool_registry``), ``ask_fn``,
    and ``policy``."""

    # Patch ChatSession at the brain module — the closure imports it
    # by absolute path, so any patch must reach that namespace.
    import tesseract.brain.chat as _chat_mod

    monkeypatch.setattr(_chat_mod, "ChatSession", _CapturedChatSession)

    runtime = ControllerRuntime()
    runtime.adapter = _FakeAdapter()
    runtime.tool_registry = _FakeRegistry()
    runtime.system_prompt = "manifest"
    runtime.tool_iteration_cap = 7
    runtime.consecutive_error_cap = 11
    runtime.policy = _FakePolicy()

    class _FakeDaemon:
        async def request_permission(self, *_a: Any, **_k: Any) -> bool:
            return False

        async def append_event(self, *_a: Any, **_k: Any) -> int:
            return 0

    record = ControllerSessionRecord(
        session_id="sess-ctrl-1",
        mode="chat",
        origin="cli",
        transcript_path=str(isolated_home / "transcript.jsonl"),
    )

    dispatch_turn = runtime.make_dispatch_turn()
    await dispatch_turn(record, "hello", _FakeDaemon())  # type: ignore[arg-type]

    kw = _CapturedChatSession.last_kwargs
    # C-2 — every dataclass field with no default must be present.
    assert "adapter" in kw
    assert "system_prompt" in kw
    assert "max_tool_iterations" in kw
    assert "max_consecutive_adapter_errors" in kw, (
        "ControllerRuntime must pass max_consecutive_adapter_errors "
        "or ChatSession construction TypeError fires on every turn"
    )
    assert kw["max_consecutive_adapter_errors"] == 11
    assert kw["max_tool_iterations"] == 7
    # Reviewer Bug 1 + 3: the dataclass field is named `registry`, not
    # `tool_registry`. Passing the wrong name silently TypeErrors at the
    # real ChatSession; lock the correct name AND forbid the wrong one
    # so a future refactor cannot regress.
    assert "registry" in kw, (
        "ChatSession's tool-registry field is `registry`; controller "
        "passing `tool_registry=` would TypeError on the real dataclass"
    )
    assert "tool_registry" not in kw, (
        "controller must not pass `tool_registry=` — that name doesn't "
        "exist on the ChatSession dataclass"
    )
    assert kw["registry"] is runtime.tool_registry
    # C-3 — ask_fn + policy must reach the session.
    assert kw["ask_fn"] is not None, (
        "controller must wire ask_fn so ASK tools prompt operator via IPC"
    )
    assert kw["policy"] is runtime.policy, (
        "controller must pass the loaded PermissionPolicy"
    )
    # 2026-05-24 regression lock: the closure must actually CALL
    # `session.send(text)`. Earlier code called the non-existent
    # `session.stream_user(text)` and every chat turn AttributeError'd
    # before producing a reply.
    assert "send" in _CapturedChatSession.methods_called, (
        "ControllerRuntime.make_dispatch_turn must call session.send(text) "
        "— ChatSession has no `stream_user` method; the right name is `send`"
    )


@pytest.mark.asyncio
async def test_controller_ask_fn_forwards_to_daemon_request_permission(
    isolated_home: Path,
) -> None:
    """C-3: the ask_fn the controller wires must call
    ``daemon.request_permission(session_id, tool=..., tool_use_id=...,
    posture="ask", summary=...)`` so the daemon's pending-approval map
    can route the operator's ``approval`` IPC back to the future the
    chat brain is awaiting."""

    captured: list[dict[str, Any]] = []
    approve_value = True

    class _FakeDaemon:
        async def request_permission(
            self,
            session_id: str,
            *,
            tool: str,
            summary: str,
            tool_use_id: str,
            posture: str = "ask",
            timeout_seconds: float = 300.0,
        ) -> bool:
            captured.append(
                {
                    "session_id": session_id,
                    "tool": tool,
                    "summary": summary,
                    "tool_use_id": tool_use_id,
                    "posture": posture,
                }
            )
            return approve_value

    ask_fn = _make_controller_ask_fn(
        _FakeDaemon(),  # type: ignore[arg-type]
        session_id="sess-ctrl-7",
    )

    class _FakeTool:
        name = "bash_tool"

    class _FakeInput:
        def model_dump_json(self) -> str:
            return '{"command": "ls"}'

    class _FakeContext:
        current_call_id = "tool_call_abc123"

    approved = await ask_fn(_FakeTool(), _FakeInput(), _FakeContext())

    assert approved is True
    assert len(captured) == 1
    call = captured[0]
    assert call["session_id"] == "sess-ctrl-7"
    assert call["tool"] == "bash_tool"
    assert call["tool_use_id"] == "tool_call_abc123"
    assert call["posture"] == "ask"
    assert "ls" in call["summary"]


@pytest.mark.asyncio
async def test_controller_ask_fn_handles_missing_current_call_id(
    isolated_home: Path,
) -> None:
    """Defensive: ``current_call_id`` should always be pinned by the
    chat-tool split, but if upstream changes drop it the ask_fn must
    still produce a forward-able request rather than raising."""

    captured: list[dict[str, Any]] = []

    class _FakeDaemon:
        async def request_permission(
            self, session_id: str, **kwargs: Any
        ) -> bool:
            captured.append({"session_id": session_id, **kwargs})
            return False

    ask_fn = _make_controller_ask_fn(_FakeDaemon(), session_id="s-1")  # type: ignore[arg-type]

    class _FakeTool:
        name = "file_write"

    class _BareContext:
        pass

    await ask_fn(_FakeTool(), {"path": "x"}, _BareContext())

    assert captured[0]["tool_use_id"] == "unknown"
