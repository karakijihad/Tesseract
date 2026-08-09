"""spawn_check — read-only status of a background spawn handle.

Phase 4 of the assistant reboot CLI-parity plan. Companion to
`delegate_coder(background=true)` and (in a follow-up pass)
`delegate_auditor` / `invoke_agent`. Returns running / done / failed /
cancelled — does NOT return the full output (use `spawn_await` for
that) so a polling-style check stays cheap.
"""

from __future__ import annotations

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
    @property
    def name(self) -> str:
        return "spawn_check"

    @property
    def description(self) -> str:
        return (
            "Check the status of a background spawn (delegate_* or "
            "invoke_agent with background=true). Returns running / "
            "done / failed / cancelled, plus started_at and "
            "finished_at. Does not return the full output — call "
            "spawn_await when you need the result."
        )

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
            )
        status = handle.status()
        line = (
            f"{handle.kind} ({inp.handle}) — {status}"
            f" · started {handle.started_at}"
        )
        if handle.finished_at:
            line += f" · finished {handle.finished_at}"
        return ToolResult(
            output=line,
            metadata={
                "handle": inp.handle,
                "kind": handle.kind,
                "status": status,
                "started_at": handle.started_at,
                "finished_at": handle.finished_at,
            },
        )
