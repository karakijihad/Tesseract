"""DelegateCoderTool — hands a task to whichever CLI holds the coder seat.

Named for the job, not the vendor. `roles.yaml::coder` decides which CLI
fills the seat and which model it runs; nothing here names one, so moving
the seat to a different provider is a config edit rather than a code change.

`provider` borrows the other CLI for a single call — the seats are a
default, not a constraint. Either CLI can code or review.

The task runs as one turn on an ephemeral lane (`_lane_delegate`), so it
has the same identity, event stream, interrupt and wait primitive as any
`lane_turn` — there is one delegation system, not two. The lane is closed
when the turn ends.

A chat brain that wants a controller-managed *session* (one that survives a
backend restart, with a chat brain of its own driving further work) still
wants `delegate_agent_controller`.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools._delegate_runner import run_delegate

log = logging.getLogger(__name__)


class DelegateCoderInput(BaseModel):
    task: str = Field(description="Task prompt to send to the coder")
    timeout: float = Field(
        default=300.0,
        ge=10,
        le=1800,
        description=(
            "Stall ceiling in seconds (10-1800, default 300): give up only "
            "after this long with NO activity from the CLI. An actively "
            "working delegate keeps going — lane waits bound silence, not "
            "total duration, because a wall-clock cap abandons healthy long "
            "tasks."
        ),
    )
    provider: str | None = Field(
        default=None,
        description=(
            "Borrow a specific CLI for this one call. Leave unset to use "
            "whichever CLI holds the coder seat in roles.yaml — that is the "
            "normal path. Set it only when this particular task wants a "
            "different one. Any provider with a `<name>_cli` role in "
            "roles.yaml is borrowable; an unknown name is refused with the "
            "list of the ones that are."
        ),
    )
    target_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths this task will edit. Declare them for edit "
            "tasks. If any path is under tesseract/mirror/**, this headless "
            "tool refuses and you must use "
            "start_controller_session(launch_terminal=True) instead."
        ),
    )
    background: bool = Field(
        default=True,
        description=(
            "True (DEFAULT): the lane turn runs as a background asyncio "
            "task; the tool returns a `spawn_handle` immediately so the assistant can "
            "keep chatting with the operator and dispatch other work in "
            "parallel. Use spawn_check (poll) or spawn_await (block) to "
            "retrieve the result later. Set false ONLY when the next step in "
            "the same turn must consume the result immediately and there's "
            "nothing else to do meanwhile — the foreground path blocks the "
            "entire turn for the full delegate duration (up to `timeout` "
            "seconds). Foreground requests whose timeout exceeds "
            "runtime.yaml::max_foreground_delegate_timeout_s are "
            "auto-flipped to background."
        ),
    )


class DelegateCoderTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    # A CLI's reply is whatever it read out of a repository, a web page, or
    # a compromised model — untrusted by origin, however trusted the tool
    # that fetched it. Set here so the foreground result and `spawn_await`'s
    # retrieval get the same envelope the background completion delivery
    # applies in `chat.py::_format_spawn_completion`; wrapping only the one
    # path left the other two open.
    untrusted_source: ClassVar[bool] = True

    @property
    def name(self) -> str:
        return "delegate_coder"

    @property
    def description(self) -> str:
        return (
            "Delegate a coding task to the coder seat — large reads, "
            "multi-file refactors, careful builds. Which CLI fills the seat "
            "is config (roles.yaml::coder); pass `provider` only to borrow "
            "the other one for this call. Returns final output text."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateCoderInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(
        self, tool_input: BaseModel, context: ToolContext
    ) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, DelegateCoderInput)
            else DelegateCoderInput(**tool_input.model_dump())
        )
        # argv, subscription env and stream parsing belong to the lane's
        # adapter — the same driver a named lane uses, so a delegation and a
        # lane turn cannot diverge in how they invoke the CLI.
        return await run_delegate(
            tool_name="delegate_coder",
            seat="coder",
            tool_input=inp,
            context=context,
        )


__all__ = ["DelegateCoderTool", "DelegateCoderInput"]
