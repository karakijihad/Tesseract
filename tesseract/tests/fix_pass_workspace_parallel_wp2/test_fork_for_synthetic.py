"""WP-2 — `ChatSession.fork_for_synthetic` returns an isolated ephemeral session.

Audit-driven invariants the fork must hold:
  * History is a deep copy — mutations on the fork don't leak back.
  * `tool_context.cancel_event` is independent — cancelling the
    synthetic turn doesn't cancel the chat turn.
  * `tool_context.todos` is fresh and independent.
  * `tool_context.spawns` is fresh — synthetic spawns don't mingle with chat.
  * `ToolRegistry` excludes `set_mood`/`set_state` (mis-annotated as
    concurrency-safe pre-2026-05-22; their state holders are shared
    mutable singletons on the tool instance per WP-1 audit §D).
  * Shared infrastructure (cost_ledger, ask_fn, policy, prompt_builder)
    is passed by reference — synthetic turns bill into the same ledger
    and use the same operator approval gate.
  * The FallbackAdapter is forked (fresh breaker state); the underlying
    chain is shared (stateless wrappers).
"""

from __future__ import annotations

from typing import Any

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class _NullAdapter:
    """Minimal ModelAdapter stand-in — never actually streams."""

    async def stream(self, *args, **kwargs):  # pragma: no cover — not exercised here
        if False:
            yield None


class _FakeForkableAdapter(_NullAdapter):
    """Adapter that records whether its `fork()` was called."""

    def __init__(self) -> None:
        self.fork_called = False

    def fork(self) -> "_FakeForkableAdapter":
        self.fork_called = True
        clone = _FakeForkableAdapter()
        # Track lineage so the test can assert the clone is distinct.
        clone.parent = self  # type: ignore[attr-defined]
        return clone


class _StubTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub tool {self._name}"

    @property
    def input_schema(self):  # type: ignore[override]
        from pydantic import BaseModel

        class _Empty(BaseModel):
            pass

        return _Empty

    async def run(self, tool_input, context: ToolContext) -> ToolResult:  # pragma: no cover
        return ToolResult(output=f"ran {self._name}")


def _build_session() -> ChatSession:
    reg = ToolRegistry()
    reg.register(_StubTool("set_mood"))
    reg.register(_StubTool("set_state"))
    reg.register(_StubTool("memory_save"))
    reg.register(_StubTool("workspace_reply"))
    return ChatSession(
        adapter=_FakeForkableAdapter(),
        system_prompt="parent prompt",
        max_tool_iterations=3,
        max_consecutive_adapter_errors=3,
        history=[{"role": "user", "content": "hello"}],
        registry=reg,
        session_kind="cockpit",
    )


def test_fork_history_is_independent_deep_copy() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic()

    assert fork.history == parent.history
    assert fork.history is not parent.history

    # Mutate the fork — parent stays untouched.
    fork.history.append({"role": "assistant", "content": "synthetic reply"})
    assert len(parent.history) == 1
    assert len(fork.history) == 2


def test_fork_cancel_event_is_independent() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic()

    fork.tool_context.cancel_event.set()
    assert fork.tool_context.cancel_event.is_set()
    assert not parent.tool_context.cancel_event.is_set()


def test_fork_excludes_set_mood_and_set_state_by_default() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic()

    assert fork.registry is not None
    fork_names = set(fork.registry.names())
    assert "set_mood" not in fork_names
    assert "set_state" not in fork_names
    # Other tools survive.
    assert "memory_save" in fork_names
    assert "workspace_reply" in fork_names


def test_fork_excludes_can_be_overridden() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic(synthetic_excluded_tools=("memory_save",))

    assert fork.registry is not None
    names = set(fork.registry.names())
    assert "memory_save" not in names
    assert "set_mood" in names  # not excluded this call


def test_fork_todos_independent() -> None:
    parent = _build_session()
    parent.tool_context.todos.append({"id": "1", "title": "chat task", "status": "pending"})

    fork = parent.fork_for_synthetic()
    assert fork.tool_context.todos == []
    fork.tool_context.todos.append({"id": "x", "title": "syn task", "status": "pending"})
    assert len(parent.tool_context.todos) == 1
    assert parent.tool_context.todos[0]["title"] == "chat task"


def test_fork_spawns_independent() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic()
    assert fork.spawns is not parent.spawns
    assert fork.tool_context.spawns is fork.spawns


def test_fork_adapter_is_forked_when_supported() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic()

    # Parent has a forkable adapter — fork() should have been called and
    # the synthetic session gets a distinct instance.
    assert isinstance(parent.adapter, _FakeForkableAdapter)
    assert parent.adapter.fork_called is True
    assert fork.adapter is not parent.adapter


def test_fork_adapter_passes_through_when_not_forkable() -> None:
    parent = _build_session()
    parent.adapter = _NullAdapter()  # no fork() method
    fork = parent.fork_for_synthetic()
    # Non-forkable adapter is shared — test stubs that don't implement
    # fork() shouldn't crash the fork path.
    assert fork.adapter is parent.adapter


def test_fork_shares_read_only_infrastructure() -> None:
    parent = _build_session()
    parent.system_prompt = "shared identity"

    class _Policy:
        pass
    parent.policy = _Policy()  # type: ignore[assignment]

    fork = parent.fork_for_synthetic()
    assert fork.system_prompt == parent.system_prompt
    assert fork.policy is parent.policy
    assert fork.max_tool_iterations == parent.max_tool_iterations
    assert fork.session_kind == parent.session_kind


def test_fork_pending_queues_start_empty() -> None:
    parent = _build_session()
    fork = parent.fork_for_synthetic()
    assert list(fork._pending_suggestions) == []
    assert list(fork._pending_conscience) == []
    assert fork.pending_injected_messages == []
    assert fork._pending_workspace_comment_ids == []
    assert fork._pending_workspace_event_ids == []


def test_fork_gets_its_own_failures_scope_id_despite_sharing_session_id() -> None:
    """Whole-phase review fix (2026-07-06): `tool_context.session_id` is
    deliberately copied verbatim into the fork (spawn journaling needs the
    parent's session id) — but a synthetic turn runs CONCURRENTLY with its
    parent chat turn (`mirror/server/session.py::synthetic_turn_tasks`), so
    the `failures_signal` tool-error-streak scope must NOT be the shared
    `session_id`, or a fork's tool call could clear/collide with its
    parent's still-unresolved streak. `_failures_scope_id` is minted fresh
    per `ChatSession.__init__` (including the fork's own constructor call),
    so it must differ even though `session_id` matches."""
    parent = _build_session()
    parent.tool_context.session_id = "shared-session"

    fork = parent.fork_for_synthetic()

    assert fork.tool_context.session_id == parent.tool_context.session_id
    assert fork._failures_scope_id != parent._failures_scope_id
