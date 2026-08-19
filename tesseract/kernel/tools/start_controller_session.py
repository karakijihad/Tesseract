"""StartControllerSessionTool — chat-side hand-off to a agent controller.

When a Mirror chat turn decides that the work is heavier than the
in-backend chat brain wants to run inline — long code edit, multi-step
audit, anything where the operator should be able to attach with
``agent --session <id>`` later and watch — the chat brain calls this
tool. The dispatcher mints a fresh controller session, fires the
initial prompt, and returns the ``session_id`` immediately (fire-and-
forget). The chat brain then writes a ``child_transcript_ref`` envelope
into its own transcript so the operator sees a "→ ctrl-... started"
row and can ``agent --session <id>`` to follow along.

Compared to :class:`DelegateAgentControllerTool`:

* ``delegate_agent_controller`` — backgrounds by default (fire-and-track):
  returns a spawn_handle; pass ``background=false`` to wait for
  ``assistant_text`` and return the reply inline.
* ``start_controller_session`` — fire-and-forget; the chat keeps going,
  the controller works in the background, the operator can attach.

This matches the README's "Mirror chat hands heavy work to a controller
session and then follows that session's transcript" design.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.orchestrator.agent_controller.dispatcher import (
    DispatcherError,
    dispatch_to_controller,
)

log = logging.getLogger(__name__)


class StartControllerSessionInput(BaseModel):
    task: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description=(
            "Goal handed to the new controller session. The controller's "
            "chat brain decides how to fulfill it; the caller does not "
            "wait for the reply."
        ),
    )
    title: str | None = Field(
        default=None,
        max_length=200,
        description="Optional human-readable title shown in the session list.",
    )
    mode: str = Field(
        default="chat",
        pattern=r"^(chat|autonomy|scheduler)$",
        description="Session mode. Defaults to ``chat``.",
    )
    launch_terminal: bool = Field(
        default=False,
        description=(
            "When True, after minting the session, open a `agent --session "
            "<id>` viewer PTY so the Mirror Terminal tab shows the live "
            "session. Use for mirror/** edits and any work the operator "
            "should watch."
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


class StartControllerSessionTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "long-running-collaborators"
    summary: ClassVar[str] = (
        "Spawn a whole new agent controller session in the background and fire its first prompt."
    )
    use_when: ClassVar[str] = (
        "Use to hand heavy work to a full controller session the operator can attach to and watch live."
    )
    not_when: ClassVar[str] = (
        "a lane or interactive session you keep driving yourself turn by turn, which is "
        "`lane_turn`/`session_send`."
    )

    @property
    def name(self) -> str:
        return "start_controller_session"

    @property
    def input_schema(self) -> type[BaseModel]:
        return StartControllerSessionInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(
        self, tool_input: BaseModel, context: ToolContext
    ) -> ToolResult:
        assert isinstance(tool_input, StartControllerSessionInput)

        # M5 — admit a spawn slot BEFORE launching the controller. A cap/depth
        # hit must reject the launch, not spawn an untracked session that only
        # fails to register a wake tail afterwards.
        registry = getattr(context, "spawns", None)
        reservation = None
        if registry is not None:
            try:
                reservation = registry.reserve()
            except SpawnCapExceeded as exc:
                return ToolResult(
                    output=(
                        f"controller launch rejected — spawn cap reached ({exc}). "
                        "spawn_await or spawn_cancel existing work first."
                    ),
                    is_error=True,
                    metadata={"reason": "spawn_cap_exceeded"},
                )

        try:
            result = await dispatch_to_controller(
                prompt=tool_input.task,
                origin="mirror",
                title=tool_input.title,
                mode=tool_input.mode,  # type: ignore[arg-type]
                preferred_seat=tool_input.preferred_seat,
                wait_for_completion=False,
                owner_principal=context.caller_principal or "operator",
            )
        except DispatcherError as exc:
            if reservation is not None:
                reservation.release()
            return ToolResult(
                output=f"controller dispatch failed: {exc}",
                is_error=True,
                metadata={"reason": "dispatcher_error"},
            )

        # Audit-2 M5 — surface a concrete child_transcript_path alongside
        # the session_id so frontend consumers can deep-link into
        # ``/ws/controller/{session_id}`` (live observer bridge) AND
        # render a durable "child transcript at <path>" affordance even
        # when the controller is offline. ``transcript_path`` validates
        # the session_id format; a malformed id (test fakes, unexpected
        # dispatcher output) drops the path field but keeps the
        # session_id + ws_path so the operator can still navigate.
        from tesseract.orchestrator.agent_controller.paths import (
            transcript_path,
        )

        metadata: dict[str, Any] = {
            "kind": "child_transcript_ref",
            "session_id": result.session_id,
            "mode": tool_input.mode,
            "detached": True,
            "ws_path": f"/ws/controller/{result.session_id}",
        }
        try:
            metadata["child_transcript_path"] = str(
                transcript_path(result.session_id)
            )
        except ValueError:
            log.debug(
                "start_controller_session: dispatcher returned session_id "
                "%r in non-canonical shape; metadata omits transcript path",
                result.session_id,
            )

        # Detached controller sessions previously
        # had NO SpawnRegistry handle, so their completion never idle-woke
        # The assistant (unlike delegate_agent_controller background). Register a
        # best-effort tail: reattach + await the session's first closed
        # assistant_text; the completion note then wakes the chat. A cap
        # hit only skips the tail — the session itself is already running.
        if registry is not None:
            detached_cancel = asyncio.Event()
            try:
                handle = registry.register(
                    kind=f"controller_session:{result.session_id}",
                    goal=tool_input.task,
                    coro=_tail_controller_session(
                        result.session_id, detached_cancel
                    ),
                    cancel_fn=detached_cancel.set,
                    reservation=reservation,
                )
                metadata["spawn_handle"] = handle.handle_id
            except SpawnCapExceeded as exc:
                # Reserved slot means this path is unreachable in practice; keep
                # the guard so a released reservation still degrades cleanly.
                if reservation is not None:
                    reservation.release()
                metadata["spawn_tail"] = f"skipped: {exc}"
                log.info(
                    "start_controller_session %s: wake tail skipped (%s)",
                    result.session_id,
                    exc,
                )

        if tool_input.launch_terminal and context.pty_dispatcher is not None:
            try:
                await context.pty_dispatcher(
                    "open",
                    {
                        "name": f"ctrl-{result.session_id}",
                        "command": ["agent", "--session", result.session_id],
                    },
                )
                metadata["terminal_launched"] = True
            except Exception:  # noqa: BLE001 — handoff card still returns
                log.warning("launch_terminal: pty open failed", exc_info=True)
                metadata["terminal_launched"] = False

        return ToolResult(
            output=(
                f"started controller session {result.session_id} "
                f"({tool_input.mode}). Attach with: agent --session "
                f"{result.session_id}"
            ),
            metadata=metadata,
        )


async def _tail_controller_session(
    session_id: str, cancel_event: asyncio.Event
) -> ToolResult:
    """Spawn-tracked tail for a detached controller session — reattaches and
    waits for the first closed assistant_text so the spawn's completion note
    idle-wakes the assistant with the session's reply."""
    from tesseract.orchestrator.agent_controller.dispatcher import (
        reattach_to_controller,
    )

    try:
        dr = await reattach_to_controller(session_id, cancel_event=cancel_event)
    except DispatcherError as exc:
        return ToolResult(
            output=f"controller session {session_id} tail failed: {exc}",
            is_error=True,
            metadata={"session_id": session_id},
        )
    return ToolResult(
        output=(
            dr.assistant_text
            or f"controller session {session_id} completed (no reply text)"
        ),
        metadata={"session_id": session_id, **dr.metadata},
    )


__all__ = ["StartControllerSessionInput", "StartControllerSessionTool"]
