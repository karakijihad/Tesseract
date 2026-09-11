"""return_note_read — what changed since the operator was last here.

**One reader, every surface.** The question "what did you do while I was
away" is asked the same way from the cockpit, from a phone and from anywhere
a channel reaches, and the answer has to be the same text. A per-channel
digest, or an adapter that knows what a growth record is, would be a second
answer to one question and the two would drift.

Read-only and AUTO. Every record it reads was written by the runtime's own
writers and is already the operator's; joining them and reading them back
adds no side effect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult


class ReturnNoteInput(BaseModel):
    since: str = Field(
        default="",
        description=(
            "Optional ISO timestamp to measure from. Empty uses the last-seen "
            "marker, which is when the operator last spoke on any surface."
        ),
    )


class ReturnNoteReadTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Say what changed since the operator was last here."
    use_when: ClassVar[str] = (
        "Use when the operator asks what happened, what shipped, what broke or "
        "what you got done while they were away, on any surface. Returns what "
        "shipped and the check that proved it, what failed and why, what is "
        "waiting on them, what was learned, what it spent and what is still "
        "broken, every line naming the record it came from."
    )
    not_when: ClassVar[str] = (
        "for a single day's summary, use `brief_read`. This answers a span "
        "since they were last here, which is usually longer than a day."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, *, ledger: object | None = None) -> None:
        # The live ledger, for the cap the Spent section measures against.
        # Handed in at registration the way `brief_read` is handed its
        # directory, because a tool that built its own would answer a
        # different question from the one the panel and the brief answer.
        self._ledger = ledger

    @property
    def name(self) -> str:
        return "return_note_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ReturnNoteInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        import asyncio

        inp: ReturnNoteInput = tool_input  # type: ignore[assignment]
        since = _parse_since(inp.since)
        if inp.since.strip() and since is None:
            return ToolResult(
                output=f"invalid since {inp.since!r}: expected an ISO timestamp",
                is_error=True,
            )
        # Off the loop: the note opens the agenda history, the archive, the
        # ledger and the inbox, and a chat turn must not wait on that disk.
        body = await asyncio.to_thread(_render, since, self._ledger)
        return ToolResult(output=body, metadata={"chars": len(body)})


def _render(since: datetime | None, ledger: object | None) -> str:
    from tesseract.kernel.workspace_changes import workspace_events_dir
    from tesseract.lib import last_seen
    from tesseract.orchestrator.brief import return_note
    from tesseract.workspace_events.events import EventStore

    return return_note.render(
        since=since if since is not None else last_seen.read(),
        event_store=EventStore(workspace_events_dir()),
        ledger=ledger,
    )


def _parse_since(raw: str) -> datetime | None:
    from tesseract.lib.clock import parse_stamp

    parsed = parse_stamp(raw.strip())
    return parsed.astimezone(timezone.utc) if parsed is not None else None


__all__ = ["ReturnNoteReadTool", "ReturnNoteInput"]
