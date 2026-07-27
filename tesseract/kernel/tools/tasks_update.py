"""tasks_update — flip a single todo's status (or rename its title).

Claude Code parity sibling of tasks_set. Use as work advances:
- `tasks_update {id: "2", status: "in_progress"}` when starting a step.
- `tasks_update {id: "2", status: "completed"}` when done.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)


_TodoStatus = Literal["pending", "in_progress", "completed"]


class TasksUpdateInput(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    status: Optional[_TodoStatus] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)


class TasksUpdateTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "tasks_update"

    @property
    def description(self) -> str:
        return (
            "Update one todo by id — flip its status (pending / "
            "in_progress / completed) and optionally rename it. Use as "
            "work advances. Returns the full new list so the operator's "
            "checklist re-renders inline in chat."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return TasksUpdateInput

    def is_concurrency_safe(self) -> bool:
        # Mutates the shared `context.todos` list. See comment on
        # TasksSetTool.is_concurrency_safe — keep serialised.
        return False

    def check_permissions(
        self, tool_input: BaseModel, context: ToolContext
    ) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, TasksUpdateInput)
            else TasksUpdateInput(**tool_input.model_dump())
        )
        for item in context.todos:
            if item.get("id") == inp.id:
                if inp.status is not None:
                    item["status"] = inp.status
                if inp.title is not None:
                    item["title"] = inp.title
                break
        else:
            return ToolResult(
                output=(
                    f"No todo with id={inp.id!r}. Call tasks_set first to "
                    "establish the checklist, then update by id."
                ),
                is_error=True,
            )

        snapshot = [dict(t) for t in context.todos]
        return ToolResult(
            output=f"Updated todo id={inp.id!r}.",
            metadata={"todos": snapshot},
        )
