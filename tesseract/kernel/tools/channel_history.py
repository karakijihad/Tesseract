"""``channel_history_read`` — agent-facing log reader (2026-05-17).

When the operator asks "what did we discuss yesterday about X" and the
memory pipeline + rolling summary return nothing (because the
reflection writer never fired before a bridge restart, or the topic
falls outside the recent verbatim window), this tool lets the assistant read
the per-day conversation log directly instead of confabulating.

Three lookup modes (mutually exclusive):

- ``date="YYYY-MM-DD"`` — one specific day, chronological.
- ``days_back=N`` (default 1) — concatenate the last N day-files,
  oldest-first; useful for "what did we talk about this week".
- ``substring="X"`` — full-text scan across every day-file for the
  chat, returning matching rows with surrounding context.

``default_posture="auto"`` — pure read of operator-visible logs.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 500
_CONTEXT_DEFAULT = 2


class ChannelHistoryReadInput(BaseModel):
    channel: str = Field(default="telegram")
    chat_ref: str = Field(description="Channel-native chat identifier (Telegram chat_id as string).")
    date: Optional[str] = Field(
        default=None,
        description="YYYY-MM-DD — return every row from that day, chronological.",
    )
    days_back: int = Field(
        default=1, ge=1, le=30,
        description="When `date` and `substring` are both unset, return rows from the last N days.",
    )
    substring: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring search across every day-file; returns matches with context.",
    )
    context_rows: int = Field(
        default=_CONTEXT_DEFAULT, ge=0, le=10,
        description="Rows of context before and after each substring match.",
    )
    limit: int = Field(
        default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX,
        description="Cap on total rows returned across all modes.",
    )


class ChannelHistoryReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "channel_history_read"

    @property
    def description(self) -> str:
        return (
            "Read past conversation history for a channel chat directly "
            "from the per-day JSONL logs. Use when memory_search + rolling "
            "summary return nothing for a topic the operator references "
            "('the trading bot we talked about yesterday'). Three modes: "
            "single `date`, last `days_back` days, or `substring` scan."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ChannelHistoryReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input if isinstance(tool_input, ChannelHistoryReadInput)
            else ChannelHistoryReadInput(**tool_input.model_dump())
        )
        from tesseract.integrations._conversation_store import ConversationStore

        store = ConversationStore()
        out: list[dict[str, Any]] = []

        if inp.substring:
            needle = inp.substring.lower()
            days = store.list_days(inp.channel, inp.chat_ref)
            collected: list[tuple[str, dict[str, Any]]] = []
            for day in reversed(days):  # oldest → newest for sane context ordering
                rows = store.day_rows(inp.channel, inp.chat_ref, date=day)
                for idx, row in enumerate(rows):
                    body = str(row.get("body") or "").lower()
                    if needle not in body:
                        continue
                    lo = max(0, idx - inp.context_rows)
                    hi = min(len(rows), idx + inp.context_rows + 1)
                    for ctx_row in rows[lo:hi]:
                        collected.append((day, ctx_row))
            # Dedup while preserving order (multiple matches may overlap).
            seen: set[int] = set()
            for day, row in collected:
                key = id(row)
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
                if len(out) >= inp.limit:
                    break

        elif inp.date:
            out = store.day_rows(inp.channel, inp.chat_ref, date=inp.date)
            if len(out) > inp.limit:
                out = out[-inp.limit:]

        else:
            days = store.list_days(inp.channel, inp.chat_ref)
            wanted = days[: inp.days_back]
            for day in reversed(wanted):
                out.extend(store.day_rows(inp.channel, inp.chat_ref, date=day))
            if len(out) > inp.limit:
                out = out[-inp.limit:]

        if not out:
            return ToolResult(
                output=f"channel_history_read: no rows for {inp.channel}:{inp.chat_ref} "
                f"({'substring=' + inp.substring if inp.substring else 'date=' + (inp.date or f'last {inp.days_back}d')})",
            )

        formatted = _format_rows(out)
        return ToolResult(
            output=f"{len(out)} row(s) from {inp.channel}:{inp.chat_ref}\n\n{formatted}",
            metadata={"rows_returned": len(out)},
        )


def _format_rows(rows: list[dict[str, Any]]) -> str:
    """Render rows as one-line-per-message chronological digest."""
    lines: list[str] = []
    for row in rows:
        ts = str(row.get("ts") or "")
        direction = str(row.get("direction") or "?")
        body = str(row.get("body") or "").replace("\n", " ").strip()
        if len(body) > 400:
            body = body[:399] + "…"
        marker = "←" if direction == "inbound" else "→"
        lines.append(f"{ts} {marker} {body}")
    return "\n".join(lines)


__all__ = ["ChannelHistoryReadTool"]
