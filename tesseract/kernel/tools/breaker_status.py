"""breaker_status tool — what the runtime is currently refusing to do.

A circuit breaker takes one capability off the board and everything else keeps
running, so from the outside an open breaker looks exactly like a quiet day.
This says which ones are open, what each of them has turned away, and when the
next attempt is due. Read-only.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class BreakerStatusInput(BaseModel):
    pass


class BreakerStatusTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Says which parts of the runtime have stopped trying, and what that has cost."
    use_when: ClassVar[str] = (
        "Use when something the runtime does on its own has gone quiet, when the operator asks why "
        "nothing came back from background work, or before clearing a breaker with `breaker_reset` "
        "so the answer names what is actually open."
    )
    not_when: ClassVar[str] = (
        "checking whether this machine is well (GPU, models, disk), which is `system_diagnose`; "
        "checking behavioural drift, which is `conscience_status`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    @property
    def name(self) -> str:
        return "breaker_status"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BreakerStatusInput

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.context.circuit_breaker import breaker_report, retry_phrase
        from tesseract.paths import log_dir

        rows = breaker_report(log_dir("circuit-breakers"))
        if not rows:
            return ToolResult(output="nothing has ever tripped on this machine")

        open_rows = [r for r in rows if r["tripped"]]
        if not open_rows:
            names = ", ".join(r["name"] for r in rows)
            return ToolResult(
                output=f"no breakers open. Tracked: {names}",
                metadata={"breakers": rows},
            )

        lines: list[str] = []
        for row in open_rows:
            # A breaker that has never written a file has no trip time to show:
            # it is open in this process and that is all anyone can say.
            since = f"open since {row['tripped_at']}" if row["tripped_at"] else "open"
            lines.append(f"{row['name']}: {since}, {retry_phrase(row['retry_in_seconds'])}.")
            if row["error"]:
                lines.append(f"  what it said: {row['error']}")
            for refused in row["refused"]:
                lines.append(
                    f"  turned away: {refused['subject']} "
                    f"({refused['times']} time(s) since {refused['since']})"
                )
            if not row["refused"]:
                lines.append("  nothing has been turned away by it yet")
        lines.append("Use breaker_reset to clear one now rather than waiting.")
        return ToolResult(output="\n".join(lines), metadata={"breakers": rows})
