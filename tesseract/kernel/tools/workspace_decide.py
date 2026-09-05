"""What is waiting on the operator, and answering it, from wherever they are.

Ruling 22: every approval the runtime asks for has to be answerable from a
phone. Measured into AR-13 §4a, fourteen things ask, and until this existed two
of them could be answered away from the desk. The other twelve are a drafted
agent, a drafted skill, a proposed skill revision, a soul edit, a memory merge,
a catalog edit, files waiting to be filed, a collided paragraph, an agent's own
question, a locked-path write, a nudge, and blocked autonomy.

**They were never twelve problems.** Every one is a workspace event with a
pending decision and an approve-or-reject verb, and every one settled through
`routes/workspace.py::post_decision` — a `POST` from a browser on the same
machine. So the fix is not twelve channel features. It is that one call, lifted
out of its HTTP handler (`apply_decision`) and reached through a tool, because
a tool is the thing every surface already has. No adapter learns what a soul
proposal is. Telegram gains nothing but a keyboard it already had.

**The operator decides, never the model.** `workspace_decide` is ASK by
default and stays that way: the model may propose the call, and the human tap
that clears the gate IS the approval. On a channel that tap is the inline
keyboard `_channel_gate` already raises; in the cockpit it is the same card as
any other gated call. A tool that could approve its own soul edit would be the
one hole this whole phase exists to close, so the posture is not a default to
be tuned but the mechanism itself.

`workspace_pending` beside it is the read, `auto`, because seeing what is
waiting decides nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.workspace_events.events import DECIDABLE_KINDS, SETTLED

logger = logging.getLogger(__name__)


#: How many rows one read lists. A phone reading forty is a phone reading none.
PENDING_ROWS = 10


class WorkspacePendingInput(BaseModel):
    kind: Optional[str] = Field(
        default=None,
        description=(
            "Only this kind of thing, for example `change_proposal` for soul "
            "edits or `agent_approval` for drafted agents. Leave it out for "
            "everything waiting."
        ),
    )


class WorkspaceDecideInput(BaseModel):
    event_id: str = Field(
        description="The id of the thing being decided, as `workspace_pending` returns it."
    )
    decision: Literal["approve", "reject", "resolve", "delete"] = Field(
        description=(
            "approve runs whatever the item proposes, reject records the no "
            "and runs nothing, resolve closes an informational thread, delete "
            "takes it out of the inbox."
        ),
    )
    reason: Optional[str] = Field(
        default=None,
        description="Why, in the operator's own words. Recorded with the decision.",
    )


class WorkspacePendingTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "List what is waiting on the operator to decide."
    use_when: ClassVar[str] = (
        "Use when the operator asks what needs them, what is waiting, whether "
        "anything wants approving, or before telling them there is nothing to "
        "do. Read it out with the id beside each one, because deciding needs "
        "the id."
    )
    not_when: ClassVar[str] = (
        "for how the runtime itself is doing, which is `autonomy_read`; for "
        "this machine's health, which is `system_diagnose`."
    )
    depends_on: ClassVar[str] = ""

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "workspace_pending"

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkspacePendingInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del context
        inp = (
            tool_input if isinstance(tool_input, WorkspacePendingInput)
            else WorkspacePendingInput(**tool_input.model_dump())
        )
        store = _store(self._app_provider)
        if store is None:
            return _no_store("workspace_pending")

        kinds = (inp.kind,) if inp.kind else DECIDABLE_KINDS
        try:
            events = store.list_events(kinds=tuple(kinds), limit=200)
        except Exception:
            logger.exception("workspace_pending: the inbox could not be read")
            return ToolResult(
                output=(
                    "The inbox could not be read, so it is not known whether "
                    "anything is waiting. That is not the same as nothing "
                    "waiting."
                ),
                is_error=True,
            )

        waiting = [e for e in events or [] if _is_open(e)]
        if not waiting:
            return ToolResult(output="Nothing is waiting on you.", metadata={"items": []})

        rows = [
            {
                "event_id": getattr(e, "event_id", ""),
                "kind": getattr(e, "kind", ""),
                "title": getattr(e, "title", ""),
                "summary": getattr(e, "summary", ""),
                "at": getattr(e, "created_at", "") or "",
            }
            for e in waiting[:PENDING_ROWS]
        ]
        lines = [
            f"{row['kind'].replace('_', ' ')}: {row['title'] or row['summary']} "
            f"[{row['event_id']}]"
            for row in rows
        ]
        if len(waiting) > PENDING_ROWS:
            lines.append(f"({len(waiting) - PENDING_ROWS} more not listed)")
        return ToolResult(
            output="\n".join(lines),
            metadata={"items": rows, "total": len(waiting)},
        )


class WorkspaceDecideTool(Tool):
    # NOT a default to be tuned. The gate's prompt is where the operator
    # actually makes the decision: the model proposes, the human tap approves,
    # and on a channel that tap is the same inline keyboard a gated tool call
    # raises. Set this to auto and the assistant can approve its own soul edit.
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "reaching-the-operator"
    summary: ClassVar[str] = "Answer one thing waiting in the operator's inbox."
    use_when: ClassVar[str] = (
        "Use when the operator has told you what to do about something "
        "`workspace_pending` listed: approve the drafted agent, reject the "
        "soul change, resolve the thread. One item per call, by its id. The "
        "operator confirms it themselves before it runs, so their words are "
        "the instruction and the confirmation is theirs to give."
    )
    not_when: ClassVar[str] = (
        "to decide something they have not asked you to decide. Proposing this "
        "call is not the same as making the decision, and it must never be "
        "used to clear the inbox on their behalf."
    )
    depends_on: ClassVar[str] = ""

    def __init__(self, app_provider: Optional[Callable[[], Any]] = None) -> None:
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "workspace_decide"

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkspaceDecideInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def ask_reason(self, validated: Any) -> str:
        """What the operator is being asked to confirm, in their words.

        The gate shows this on whatever surface raised it, so it carries the
        verb and the thing rather than the id: an id tells somebody looking at
        a phone nothing about what they are agreeing to.
        """
        inp = validated if isinstance(validated, WorkspaceDecideInput) else None
        if inp is None:
            return ""
        store = _store(self._app_provider)
        what = ""
        if store is not None:
            try:
                ev = store.get_event(inp.event_id)
                what = (getattr(ev, "title", "") or getattr(ev, "summary", "")) if ev else ""
            except Exception:
                logger.exception("workspace_decide: could not read the event to describe it")
        return f"{inp.decision} {what}".strip() or f"{inp.decision} {inp.event_id}"

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        del context
        inp = (
            tool_input if isinstance(tool_input, WorkspaceDecideInput)
            else WorkspaceDecideInput(**tool_input.model_dump())
        )
        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            return _no_store("workspace_decide")

        from tesseract.mirror.server.routes.workspace import (
            DecisionError,
            apply_decision,
        )

        try:
            updated, _comments = await apply_decision(
                app, inp.event_id, inp.decision, reason=inp.reason
            )
        except DecisionError as exc:
            # The route's own words, which say what was wrong with the ask.
            detail = exc.payload.get("detail") or exc.payload.get("error") or "refused"
            return ToolResult(output=f"Not done: {detail}", is_error=True)
        except Exception:
            logger.exception("workspace_decide: the decision failed to apply")
            return ToolResult(
                output=(
                    "The decision did not go through, so nothing changed and "
                    "the item is still waiting."
                ),
                is_error=True,
            )

        kind = str(getattr(updated, "kind", "") or "").replace("_", " ")
        status = str(getattr(updated, "status", "") or "")
        return ToolResult(
            output=f"{kind} is {status}.",
            metadata={
                "event_id": inp.event_id,
                "kind": getattr(updated, "kind", ""),
                "status": status,
            },
        )


def _store(app_provider: Optional[Callable[[], Any]]):
    app = app_provider() if app_provider is not None else None
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("workspace_event_store")


def _is_open(event: Any) -> bool:
    """Anything not settled is still waiting.

    That way round on purpose: a kind that grows a new terminal status
    over-reports by one until this learns it, where the opposite hides work.
    """
    if str(getattr(event, "status", "") or "") in SETTLED:
        return False
    payload = getattr(event, "payload", {}) or {}
    if isinstance(payload, dict) and payload.get("status") in SETTLED:
        return False
    return True


def _no_store(tool: str) -> ToolResult:
    return ToolResult(
        output=(
            f"{tool}: this runtime has no backend attached, so the inbox is "
            "not reachable from here. Nothing is known about what is waiting, "
            "which is not the same as nothing waiting."
        ),
        is_error=True,
    )


__all__ = [
    "DECIDABLE_KINDS",
    "PENDING_ROWS",
    "WorkspaceDecideInput",
    "WorkspaceDecideTool",
    "WorkspacePendingInput",
    "WorkspacePendingTool",
]
