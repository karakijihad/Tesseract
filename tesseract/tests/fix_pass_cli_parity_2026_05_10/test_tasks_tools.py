"""Phase 3 (CLI parity) — tasks_set / tasks_update tools.

State lives on ToolContext.todos. Tools mutate the shared list reference;
WS reads it after TOOL_RESULT to fire `tasks_state` envelope.
"""

from __future__ import annotations

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.tasks_set import (
    TasksSetInput,
    TasksSetTool,
    TodoItem,
)
from tesseract.kernel.tools.tasks_update import (
    TasksUpdateInput,
    TasksUpdateTool,
)


@pytest.mark.asyncio
async def test_tasks_set_replaces_full_list_on_tool_context():
    tool = TasksSetTool()
    ctx = ToolContext(workspace_root=".", session_id="phase3-tasks-test")
    ctx.todos.extend([{"id": "old", "title": "stale", "status": "completed"}])

    inp = TasksSetInput(
        items=[
            TodoItem(id="1", title="Plan refactor", status="in_progress"),
            TodoItem(id="2", title="Write tests", status="pending"),
            TodoItem(id="3", title="Update docs", status="pending"),
        ],
    )
    result = await tool.run(inp, ctx)

    assert not result.is_error
    assert [t["id"] for t in ctx.todos] == ["1", "2", "3"]
    assert ctx.todos[0]["status"] == "in_progress"
    assert ctx.todos[0]["title"] == "Plan refactor"
    assert "todos" in (result.metadata or {})
    assert len(result.metadata["todos"]) == 3


@pytest.mark.asyncio
async def test_tasks_set_warns_when_multiple_in_progress():
    tool = TasksSetTool()
    ctx = ToolContext(workspace_root=".", session_id="phase3-tasks-test")
    inp = TasksSetInput(
        items=[
            TodoItem(id="1", title="A", status="in_progress"),
            TodoItem(id="2", title="B", status="in_progress"),
        ],
    )
    result = await tool.run(inp, ctx)
    assert not result.is_error
    assert "warning" in result.output.lower()


@pytest.mark.asyncio
async def test_tasks_update_flips_status_by_id():
    set_tool = TasksSetTool()
    update_tool = TasksUpdateTool()
    ctx = ToolContext(workspace_root=".", session_id="phase3-tasks-test")

    await set_tool.run(
        TasksSetInput(items=[
            TodoItem(id="1", title="A", status="pending"),
            TodoItem(id="2", title="B", status="pending"),
        ]),
        ctx,
    )

    result = await update_tool.run(
        TasksUpdateInput(id="2", status="in_progress"),
        ctx,
    )
    assert not result.is_error
    assert ctx.todos[1]["status"] == "in_progress"
    assert ctx.todos[0]["status"] == "pending"
    assert result.metadata["todos"][1]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_tasks_update_can_rename_title():
    set_tool = TasksSetTool()
    update_tool = TasksUpdateTool()
    ctx = ToolContext(workspace_root=".", session_id="phase3-tasks-test")

    await set_tool.run(
        TasksSetInput(items=[TodoItem(id="x", title="initial", status="pending")]),
        ctx,
    )

    result = await update_tool.run(
        TasksUpdateInput(id="x", title="renamed"),
        ctx,
    )
    assert not result.is_error
    assert ctx.todos[0]["title"] == "renamed"
    assert ctx.todos[0]["status"] == "pending"  # unchanged


@pytest.mark.asyncio
async def test_tasks_update_unknown_id_returns_error():
    update_tool = TasksUpdateTool()
    ctx = ToolContext(workspace_root=".", session_id="phase3-tasks-test")

    result = await update_tool.run(
        TasksUpdateInput(id="ghost", status="completed"),
        ctx,
    )
    assert result.is_error
    assert "ghost" in result.output


@pytest.mark.asyncio
async def test_tasks_set_replaces_then_update_targets_new_list():
    set_tool = TasksSetTool()
    update_tool = TasksUpdateTool()
    ctx = ToolContext(workspace_root=".", session_id="phase3-tasks-test")

    await set_tool.run(
        TasksSetInput(items=[TodoItem(id="old", title="old", status="pending")]),
        ctx,
    )
    await set_tool.run(
        TasksSetInput(items=[TodoItem(id="new", title="new", status="pending")]),
        ctx,
    )

    # Old id is gone
    result_old = await update_tool.run(
        TasksUpdateInput(id="old", status="completed"),
        ctx,
    )
    assert result_old.is_error

    # New id works
    result_new = await update_tool.run(
        TasksUpdateInput(id="new", status="completed"),
        ctx,
    )
    assert not result_new.is_error
    assert ctx.todos[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_chat_session_reset_clears_todos():
    """ChatSession.reset() must wipe the operator-visible todo list so a
    `/reset` command doesn't leave stale checklist items dangling."""
    from tesseract.brain.chat import ChatSession
    from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk
    from typing import AsyncGenerator

    class _StubAdapter(ModelAdapter):
        async def generate(self, prompt: str, options: AdapterOptions | None = None) -> str:
            return ""

        async def stream(self, messages, tools=None, options=None) -> AsyncGenerator[StreamChunk, None]:
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn")

        def count_tokens(self, messages) -> int:
            return 0

        async def check_available(self) -> bool:
            return True

    sess = ChatSession(
        adapter=_StubAdapter(),
        system_prompt="",
        max_tool_iterations=5,
        max_consecutive_adapter_errors=3,
    )
    sess.tool_context.todos.extend([
        {"id": "1", "title": "wip", "status": "in_progress"},
    ])

    sess.reset()

    assert sess.tool_context.todos == []
