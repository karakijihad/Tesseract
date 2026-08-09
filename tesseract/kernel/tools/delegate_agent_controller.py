"""DelegateAgentControllerTool — hand a task to a fresh controller session.

Live callers (2026-07-08): the chat brain (the assistant picks the tool,
background fire-and-track by default), the autonomy kernel runner
(``orchestrator/autonomy/kernel_worker_runner.py`` routes
``WorkerKind.AGENT_CONTROLLER`` items here with ``background: False`` —
its wrapping task is already the background), and the ``code-fixer``
agent card (`tesseract/agents/audits/code-fixer.md`, pins
``background: false`` for its verify-each-fix loop). Inside the
controller, the chat brain takes over — calling ``delegate_coder``,
``delegate_auditor``, ``invoke_agent``, etc. — and streams the
resulting ``assistant_text`` back here.

Compared to ``delegate_coder``:

* That tool runs the coder seat as a single one-shot lane turn.
* This tool spawns/reuses a controller session whose chat brain is the
  full the assistant brain — same tool registry, same memory, same workspace
  state. The controller itself decides which sub-agents to call.

Implementation is intentionally thin: every step
(``ensure_daemon_running``, ``new_session``, ``user_input``,
``tail_until_assistant_text``) lives in
:mod:`tesseract.orchestrator.agent_controller.dispatcher`. This tool is
just the policy + schema wrapper that lets the kernel runner reach the
dispatcher through the normal ``invoke_agent``-style call path.

ASK posture matches ``delegate_coder``: the autonomy runner
won't trigger this without an accepted OPERATOR_GATE item, but a chat
brain that picks the same tool name must still flow through approval.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    Tool,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)
from tesseract.orchestrator.agent_controller.dispatcher import (
    DispatcherError,
    dispatch_to_controller,
)

log = logging.getLogger(__name__)


_DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0


class DelegateAgentControllerInput(BaseModel):
    task: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description=(
            "Goal handed to a fresh agent controller session. The "
            "controller's chat brain decides how to fulfill it — "
            "delegate to a seat, invoke an agent, etc."
        ),
    )
    title: str | None = Field(
        default=None,
        max_length=200,
        description="Optional human-readable title for the session.",
    )
    idle_timeout_seconds: float = Field(
        default=_DEFAULT_IDLE_TIMEOUT_SECONDS,
        ge=5.0,
        le=3600.0,
        description=(
            "Per-push idle budget. Each transcript event resets the "
            "clock; this many seconds of silence terminates the wait."
        ),
    )
    preferred_seat: str | None = Field(
        default=None,
        description=(
            "Hard seat constraint for the spawned controller session — a "
            "delegation seat name ('coder' or 'auditor'). When set, the "
            "other seats' delegate tools are removed from the session and a "
            "directive is added. Which provider fills the seat is roles.yaml. "
            "Leave unset to keep every seat available."
        ),
    )
    background: bool = Field(
        default=True,
        description=(
            "Fire-and-track (default): dispatch the controller session, "
            "return a spawn_handle immediately; retrieve the accumulated "
            "reply via spawn_check / spawn_await, or wait for the "
            "completion note. Pass false only when the very next step in "
            "THIS turn must consume the controller's reply inline."
        ),
    )


class DelegateAgentControllerTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "operator_gate"

    @property
    def name(self) -> str:
        return "delegate_agent_controller"

    @property
    def description(self) -> str:
        return (
            "Dispatch a task to a fresh agent controller session. The "
            "controller's chat brain orchestrates delegation seats "
            "and agents to complete the work. Backgrounds by default: "
            "returns a spawn_handle immediately; the accumulated "
            "assistant_text reply arrives via spawn_check / spawn_await "
            "or the completion note. With background=false, blocks and "
            "returns the reply inline."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateAgentControllerInput

    def is_concurrency_safe(self) -> bool:
        # Each call mints its own session — concurrent calls don't
        # share state on the controller side. The daemon's per-session
        # locks handle ordering inside one session.
        return True

    async def run(
        self, tool_input: BaseModel, context: ToolContext
    ) -> ToolResult:
        assert isinstance(tool_input, DelegateAgentControllerInput)
        if tool_input.background:
            registry = getattr(context, "spawns", None)
            if registry is not None:
                # Reviewer finding (2026-07-09): a detached spawn must NOT
                # inherit the session-lifetime cancel_event — the Stop button
                # on a later, unrelated turn sets that same object and would
                # silently kill this dispatch mid-flight. Fresh event per
                # spawn; spawn_cancel still reaches it via cancel_fn.
                detached_cancel = asyncio.Event()
                spawn_context = dataclasses.replace(
                    context, cancel_event=detached_cancel
                )
                try:
                    handle = registry.register(
                        kind="delegate_agent_controller",
                        goal=tool_input.task,
                        coro=self._run_foreground(tool_input, spawn_context),
                        cancel_fn=detached_cancel.set,
                    )
                except SpawnCapExceeded as exc:
                    return spawn_cap_tool_result(exc)
                return ToolResult(
                    output=(
                        f"delegate_agent_controller spawned in background: "
                        f"handle={handle.handle_id}. Use spawn_check or "
                        f"spawn_await to retrieve the reply."
                    ),
                    metadata={
                        "spawn_handle": handle.handle_id,
                        "spawn_kind": "delegate_agent_controller",
                        "started_at": handle.started_at,
                        "status": "running",
                    },
                )
            # Headless / REPL contexts carry no SpawnRegistry — degrade to
            # the inline path rather than failing the call.

        return await self._run_foreground(tool_input, context)

    async def _run_foreground(
        self, tool_input: DelegateAgentControllerInput, context: ToolContext
    ) -> ToolResult:
        try:
            result = await dispatch_to_controller(
                prompt=tool_input.task,
                origin="autonomy",
                title=tool_input.title,
                mode="autonomy",
                preferred_seat=tool_input.preferred_seat,
                wait_for_completion=True,
                idle_timeout_seconds=tool_input.idle_timeout_seconds,
                cancel_event=context.cancel_event,
            )
        except DispatcherError as exc:
            return ToolResult(
                output=f"controller dispatch failed: {exc}",
                is_error=True,
                metadata={"reason": "dispatcher_error"},
            )

        metadata = {"session_id": result.session_id, **result.metadata}
        if result.timed_out:
            return ToolResult(
                output=result.assistant_text
                or f"controller idle timeout after {tool_input.idle_timeout_seconds:.0f}s",
                is_error=True,
                timed_out=True,
                metadata={**metadata, "timed_out": True},
            )
        if result.cancelled:
            return ToolResult(
                output=result.assistant_text or "cancelled",
                is_error=True,
                metadata={**metadata, "cancelled": True},
            )
        if not result.saw_assistant_text:
            return ToolResult(
                output=result.assistant_text or "no assistant_text received",
                is_error=True,
                metadata=metadata,
            )
        return ToolResult(
            output=result.assistant_text,
            metadata=metadata,
        )


__all__ = ["DelegateAgentControllerInput", "DelegateAgentControllerTool"]
