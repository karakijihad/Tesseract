"""agent_ask — a running sub-agent asks its parent a question and waits.

The other half of `work_send`. Steering sends an instruction down into
running work; this sends a question up. Both travel the one channel, so an
answer is delivered exactly the way a course-correction is — the parent
replies with `work_send(target=<handle>, message=...)` and the sub-agent's
next turn opens with it.

Only a BACKGROUND sub-agent has a parent to ask. A foreground invocation is
already inside the caller's own turn: the caller is blocked waiting for it,
so a question would deadlock. That case is refused, not queued.

The `spawn:<handle_id>` task name is the resolution path, the same
load-bearing contract the Mirror ask_fn uses to park unattended ASKs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)

_TASK_NAME_PREFIX = "spawn:"


class AgentAskInput(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "What you need from whoever dispatched you. Be specific and "
            "self-contained — they cannot see your reasoning, only this. "
            "Ask when a wrong assumption would waste the rest of the run; "
            "otherwise state your assumption and keep going."
        ),
    )


def _own_handle():
    """The SpawnHandle for the task this call is running inside, or None."""
    from tesseract.brain.spawns import find_handle

    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    name = task.get_name() if task is not None else ""
    if not name.startswith(_TASK_NAME_PREFIX):
        return None
    # ChatSession runs a concurrency-safe tool in a CHILD task named
    # `spawn:<handle_id>|tool:<name>:<call_id>` (chat.py::_run_pending_calls),
    # so the id ends at the first `|`. Taking the whole remainder looks up a
    # key that cannot exist and silently reports "no parent".
    return find_handle(name[len(_TASK_NAME_PREFIX):].split("|", 1)[0])


class AgentAskTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "handing-work-off"
    summary: ClassVar[str] = (
        "Lets a background sub-agent ask its dispatcher a question and wait "
        "for the reply."
    )
    use_when: ClassVar[str] = (
        "Use when a wrong assumption would waste the rest of a background "
        "run; otherwise state the assumption and continue."
    )
    not_when: ClassVar[str] = (
        "A foreground call has no parent to ask and is refused. Sending an "
        "instruction downward instead — use `work_send`."
    )

    @property
    def name(self) -> str:
        return "agent_ask"

    @property
    def input_schema(self) -> type[BaseModel]:
        return AgentAskInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        """Touches no file, socket, or subprocess — it posts a question onto
        its own spawn handle and waits. Declaring it read-only also gives the
        headless path the right behaviour: auto-allowed, then refused on the
        merits (`no_parent`) rather than denied for a write it never does."""
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        assert isinstance(tool_input, AgentAskInput)
        handle = _own_handle()
        if handle is None:
            return ToolResult(
                output=(
                    "agent_ask is only available to a background sub-agent — "
                    "there is no parent waiting to answer. State your "
                    "assumption explicitly in your answer and continue."
                ),
                is_error=True,
                metadata={"reason": "no_parent"},
            )

        handle.question = tool_input.question.strip()
        handle.input_required = True
        log.info("agent_ask: %s posted a question to its parent", handle.handle_id)
        # End the turn rather than asking the model to volunteer. Left to
        # prose, a model that keeps calling tools would leave the handle
        # advertising input_required while it is in fact still working, and
        # the eventual answer would cut short a turn nobody expected to be
        # interrupted. The spawn's own loop treats this as a turn boundary
        # and parks for the reply.
        turn_cancel = getattr(context, "cancel_event", None)
        if turn_cancel is not None:
            turn_cancel.set()
        return ToolResult(
            output=(
                "Question posted to the caller; this turn ends here. Their "
                "reply arrives as your next instruction — continue then."
            ),
            metadata={
                "spawn_handle": handle.handle_id,
                "question": handle.question,
                "status": "input_required",
            },
        )
