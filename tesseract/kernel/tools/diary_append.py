"""diary_append tool — TARS's first-person reflection log.

Different from `memory_save`. Memory captures *facts about the world*
(operator preferences, project state, references). The diary captures
*facts about TARS* — what he noticed about himself in a session, what
landed, what felt off, what he'd do differently.

Storage: `tesseract/memory-store/diary/YYYY-MM-DD.md`, one file per day,
append-only. The librarian reads recent entries during heartbeat and
drafts candidate `SOUL.md Growth` bullets; TARS decides on `/reflect`
which candidates stay.

Not retrieved by `memory_search` — the diary is intentionally walled off
from routine context so personality reflection doesn't pollute every
turn. See `tesseract/workspace/DIARY.md` for the full contract.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_DIARY_REL = "memory-store/diary"
_MAX_ENTRY_CHARS = 1200


class DiaryAppendInput(BaseModel):
    text: str = Field(
        description=(
            "First-person reflection (1-3 short sentences ideal). "
            "What you noticed about yourself this turn or session — "
            "what landed, what felt off, what you'd do differently. "
            "Not a fact about the operator (use memory_save for that)."
        )
    )


class DiaryAppendTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    """Append a first-person reflection entry to today's diary file.

    AUTO permission — this is TARS's own file. No operator approval
    needed. Writes to `<repo_root>/tesseract/memory-store/diary/<date>.md`.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    @property
    def name(self) -> str:
        return "diary_append"

    @property
    def description(self) -> str:
        return (
            "Append a first-person reflection to your private diary. "
            "Use for self-observations: what landed, what felt off, "
            "patterns you're noticing about yourself. Not for facts "
            "about the operator (use memory_save). Read DIARY.md before "
            "first use."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DiaryAppendInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, DiaryAppendInput)
            else DiaryAppendInput(**tool_input.model_dump())
        )

        text = (inp.text or "").strip()
        if not text:
            return ToolResult(
                output="Diary entry empty — nothing written.",
                is_error=True,
            )

        if len(text) > _MAX_ENTRY_CHARS:
            return ToolResult(
                output=(
                    f"Diary entry too long ({len(text)} chars > {_MAX_ENTRY_CHARS}). "
                    "Diary is for short reflections, not reports. Trim and retry."
                ),
                is_error=True,
            )

        now = datetime.now(timezone.utc).astimezone()
        diary_dir = self._repo_root / _DIARY_REL
        try:
            diary_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("diary mkdir failed: %s", exc)
            return ToolResult(output=f"Diary write failed: {exc}", is_error=True)

        filename = now.strftime("%Y-%m-%d") + ".md"
        path = diary_dir / filename
        timestamp = now.strftime("%H:%M")

        new_file = not path.exists()
        try:
            with path.open("a", encoding="utf-8") as f:
                if new_file:
                    f.write(f"# Diary — {now.strftime('%Y-%m-%d')}\n\n")
                f.write(f"**{timestamp}**  {text}\n\n")
        except OSError as exc:
            logger.warning("diary append failed: %s", exc)
            return ToolResult(output=f"Diary write failed: {exc}", is_error=True)

        return ToolResult(
            output=f"Diary entry logged to {filename} at {timestamp}.",
            metadata={"path": str(path), "timestamp": timestamp},
        )
