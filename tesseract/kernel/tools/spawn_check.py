"""spawn_check — read-only status of a background spawn handle.

Companion to `delegate_coder(background=true)`, `delegate_auditor` and
`invoke_agent`. Returns running / done / failed / cancelled, plus how many
events the spawn has emitted and how long ago the last one landed. It does
NOT return the full output (use `spawn_await` for that) so a polling-style
check stays cheap.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


class SpawnCheckInput(BaseModel):
    handle: str = Field(min_length=1, max_length=120)


class SpawnCheckTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "tracking-spawned-work"
    summary: ClassVar[str] = (
        "A background spawn: running, done, failed or cancelled, "
        "and when it last emitted."
    )
    use_when: ClassVar[str] = (
        # 350 chars is the glossary budget and this line rides every turn, so
        # the long form of the reasoning lives in the RESULT, where a turn
        # reads it at the moment it is deciding whether to check again.
        "Use ONCE to confirm a spawn's state, before steering it with `work_send` "
        "or deciding whether to `spawn_cancel`. If it is still running, say so and "
        "end the turn: the outcome reaches you next turn on its own, and a second "
        "check learns nothing and costs a model call. A spawn-cap error wants an "
        "await or a cancel; a depth-cap error means work inline."
    )
    not_when: ClassVar[str] = (
        "Retrieving a finished spawn's output. That arrives on its own in "
        "your next turn; use `spawn_await` only for the rare case where the "
        "result said it was too large to deliver whole."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    @property
    def name(self) -> str:
        return "spawn_check"

    @property
    def input_schema(self) -> type[BaseModel]:
        return SpawnCheckInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, SpawnCheckInput)
            else SpawnCheckInput(**tool_input.model_dump())
        )
        registry = getattr(context, "spawns", None)
        if registry is None:
            return ToolResult(
                output="spawn_check unavailable: registry not wired",
                is_error=True,
            )
        handle = registry.get(inp.handle)
        if handle is None:
            # Same reconnect fallback as spawn_await (M4-p2): a spawn that
            # survived a same-process reconnect lives in the orphaned old
            # registry — resolve it through the process-global index so
            # check/await/cancel stay consistent on the same handle.
            from tesseract.brain.spawns import find_handle

            handle = find_handle(inp.handle)
        if handle is None:
            return ToolResult(
                output=f"No spawn with handle={inp.handle!r}.",
                is_error=True,
                caller_error=True,
            )
        status = handle.status()
        line = (
            f"{handle.kind} ({inp.handle}) — {status}"
            f" · started {handle.started_at}"
        )
        if handle.finished_at:
            line += f" · finished {handle.finished_at}"
        # Progress, reported as the two numbers and never as a verdict. A long
        # compile is legitimately quiet and calling it stalled here would be
        # wrong; the model has the previous check's counts in its own context,
        # so it can see the difference between six quiet minutes and none.
        # Omitted entirely when the substrate does not report activity, which
        # is not the same fact as "nothing has happened".
        from tesseract.brain.spawns import handle_quiet_seconds

        quiet = handle_quiet_seconds(handle, datetime.now(timezone.utc))
        if quiet is not None:
            line += (
                f" · {handle.activity_events} events, last "
                f"{quiet:.0f}s ago"
            )
        # Said in the RESULT, not only in the schema, because this is what a
        # turn reads while it is deciding what to do next. A running spawn
        # finishes on its own and `ChatSession.ingest_spawn_completion` hands
        # the outcome to the following turn, so checking again inside this one
        # cannot learn anything the wait did not. Measured cost of not saying
        # it: one turn checked the same handle twenty times, and because every
        # check is preceded by a model call carrying the whole conversation,
        # the waiting cost about as much as the work.
        if status == "running":
            line += (
                "\nStill running. It will finish on its own and the outcome "
                "reaches you at the start of your next turn. Do not check it "
                "again in this turn: say what you are waiting for and end the "
                "turn. Use `work_send` to steer it or `spawn_cancel` to stop "
                "it."
            )
        return ToolResult(
            output=line,
            metadata={
                "handle": inp.handle,
                "kind": handle.kind,
                "status": status,
                "started_at": handle.started_at,
                "finished_at": handle.finished_at,
                "last_activity_at": handle.last_activity_at,
                "activity_events": handle.activity_events,
            },
        )
