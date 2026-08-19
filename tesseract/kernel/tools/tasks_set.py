"""tasks_set — replace the operator-visible task checklist for this session.

Claude Code parity: TodoWrite analog. Use when a turn requires more than
~3 distinct steps so the operator can follow progress without having to
infer it from tool calls. Ephemeral per session — not persisted across
sessions (yet — Phase 1 of the CLI-parity plan folds into schema-2
day-files).

Each item: {id, title, status} where status ∈ {pending, in_progress,
completed}. Calling `tasks_set` REPLACES the entire list. Use
`tasks_update` to flip a single item's status as work advances.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


_TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoItem(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    status: _TodoStatus = "pending"


class TasksSetInput(BaseModel):
    items: list[TodoItem] = Field(
        description=(
            "Full replacement list. Order is the operator's reading order. "
            "Use stable, short ids ('1', '2', 'compact', 'reflect') so "
            "tasks_update can target by id without you having to repeat "
            "the title."
        ),
        max_length=20,
    )


class TasksSetTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "being-present"
    summary: ClassVar[str] = "Replaces the full operator-visible task checklist for this turn."
    use_when: ClassVar[str] = (
        "Use when a turn needs more than a few distinct steps, so the operator can follow progress "
        "instead of inferring it from tool calls. Skip for single-tool or trivial answers. Give "
        "each step a short stable id ('1', '2', 'verify') — `tasks_update` addresses them by id, "
        "so an id that changes between calls loses the step."
    )
    not_when: ClassVar[str] = (
        "flipping one item's status, which is `tasks_update` — calling `tasks_set` again replaces "
        "the whole list."
    )

    @property
    def name(self) -> str:
        return "tasks_set"

    @property
    def input_schema(self) -> type[BaseModel]:
        return TasksSetInput

    def is_concurrency_safe(self) -> bool:
        # Mutates shared `context.todos`. Today this is safe under the
        # cooperative single-threaded asyncio loop because there's no
        # await between read and write — but declaring True invites a
        # future refactor to add an await mid-loop and silently
        # introduce a race. Keep False so `_run_pending_calls`
        # serialises tasks_set calls.
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
            if isinstance(tool_input, TasksSetInput)
            else TasksSetInput(**tool_input.model_dump())
        )
        # Mutate in place so any other code holding the same list
        # reference (ChatSession, future persistence) sees the update.
        items = [item.model_dump() for item in inp.items]
        context.todos.clear()
        context.todos.extend(items)

        active = sum(1 for t in items if t["status"] == "in_progress")
        if active > 1:
            # Soft warning only — model can still proceed but should
            # normally pick exactly one in_progress at a time.
            note = f" (warning: {active} items marked in_progress; usually pick one)"
        else:
            note = ""
        return ToolResult(
            output=f"Set {len(items)} todo{'s' if len(items) != 1 else ''}{note}.",
            metadata={"todos": items},
        )
