"""DelegateAuditorTool — hands a review to whichever CLI holds the auditor seat.

Named for the job, not the vendor. `roles.yaml::auditor` decides which CLI
fills the seat and which model it runs; nothing here names one, so moving
the seat to a different provider is a config edit rather than a code change.

`provider` borrows the other CLI for a single call — the seats are a
default, not a constraint. Either CLI can review or code.

**This tool does not enforce a read-only boundary.** A delegation opens an
ephemeral writeable lane, as every delegation always has — and it must,
because a read-only lane only exists for codex (`manager.py` raises for a
read-only claude lane), so forcing it here would make `provider="claude"`
unusable for review. Ask for findings rather than fixes in the brief. For a
boundary that holds by construction, open a named lane with
`read_only=True` (`lane_named_ensure`) — that is the operator's own lane and
the sandbox is the CLI's, not this tool's.

The task runs as one turn on an ephemeral lane (`_lane_delegate`), so it has
the same identity, event stream, interrupt and wait primitive as any
`lane_turn` — there is one delegation system, not two.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools._delegate_runner import run_delegate

log = logging.getLogger(__name__)


class DelegateAuditorInput(BaseModel):
    task: str = Field(description="Review brief to send to the auditor")
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
            "whichever CLI holds the auditor seat in roles.yaml — that is "
            "the normal path, and a cross-family review is the point, so "
            "prefer the seat default over matching the coder. Any provider "
            "with a `<name>_cli` role in roles.yaml is borrowable; an unknown "
            "name is refused with the list of the ones that are."
        ),
    )
    target_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths this review covers. If any path is under "
            "tesseract/mirror/**, this headless tool refuses and you must "
            "use start_controller_session(launch_terminal=True) instead."
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


class DelegateAuditorTool(Tool):
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
        return "delegate_auditor"

    @property
    def description(self) -> str:
        return (
            "Delegate a review to the auditor seat — code review, "
            "verification of your own reasoning, second opinions, scope "
            "checks. Which CLI fills the seat is config "
            "(roles.yaml::auditor); pass `provider` only to borrow the other "
            "one for this call. Returns final output text."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateAuditorInput

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
            if isinstance(tool_input, DelegateAuditorInput)
            else DelegateAuditorInput(**tool_input.model_dump())
        )
        return await run_delegate(
            tool_name="delegate_auditor",
            seat="auditor",
            tool_input=inp,
            context=context,
        )


__all__ = ["DelegateAuditorTool", "DelegateAuditorInput"]
