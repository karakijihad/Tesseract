"""brief_read — return today's daily brief as plain text.

Operator-facing surface for the voice route in MO-9-9:
``read brief`` (voice) → STT → chat_brain → ``brief_read`` tool →
plain-text body → chat_brain reads it back through the normal TTS
streaming pipeline.

Read-only, AUTO-gated. The brief file was already operator-attended at
write time (``brief_render`` ASK / cron). Reading it back later is
equivalent to ``memory_get`` on the same path — no new side effect.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.paths import TESSERACT_HOME

logger = logging.getLogger(__name__)


class BriefReadInput(BaseModel):
    date: str = Field(
        default="",
        description=(
            "Target ISO date (YYYY-MM-DD). Empty = today UTC. Reads "
            "``memory-store/daily/briefs/<iso-date>.md`` and returns the "
            "body markdown (frontmatter stripped) ready for TTS."
        ),
    )


class BriefReadTool(Tool):
    default_posture: ClassVar[str] = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(self, *, briefs_dir: Path | None = None) -> None:
        # Default resolved at call time via ``_resolve_briefs_dir`` so
        # tests that monkeypatch ``TESSERACT_HOME`` reach the right tree.
        self._override_dir = briefs_dir

    @property
    def name(self) -> str:
        return "brief_read"

    @property
    def description(self) -> str:
        return (
            "Return the daily brief for a given ISO date (defaults to "
            "today UTC) as plain markdown body, frontmatter stripped. "
            "Voice-friendly. Use when the operator asks to hear, read, or "
            "summarise the daily brief — TARS reads the returned text "
            "verbatim back to the operator through the normal TTS lane."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return BriefReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: BriefReadInput = tool_input  # type: ignore[assignment]
        target = _parse_target_date(inp.date)
        if target is None:
            return ToolResult(
                output=f"invalid date {inp.date!r}: expected YYYY-MM-DD",
                is_error=True,
            )
        path = self._resolve_briefs_dir() / f"{target.isoformat()}.md"
        if not path.exists():
            return ToolResult(
                output=(
                    f"no brief for {target.isoformat()} — run /brief or wait for "
                    "the daily cron to fire."
                ),
                is_error=True,
                metadata={"date": target.isoformat(), "path": str(path)},
            )
        text = path.read_text(encoding="utf-8")
        body = _strip_markdown_links(_strip_frontmatter(text))
        return ToolResult(
            output=body,
            metadata={
                "date": target.isoformat(),
                "path": str(path),
                "char_count": len(body),
            },
        )

    def _resolve_briefs_dir(self) -> Path:
        if self._override_dir is not None:
            return Path(self._override_dir)
        import os

        home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
        return home / "memory-store" / "daily" / "briefs"


def _parse_target_date(raw: str) -> date | None:
    stripped = raw.strip()
    if not stripped:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        return None


def _strip_frontmatter(text: str) -> str:
    """Same partition as the renderer: drop the ``---\\n<yaml>---\\n\\n``
    prefix so TTS does not narrate ``minus minus minus`` plus the YAML
    keys before the body."""
    if not text.startswith("---\n"):
        return text
    _head, sep, body = text[4:].partition("---\n")
    if not sep:
        return text
    return body.lstrip("\n")


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _strip_markdown_links(text: str) -> str:
    """Collapse ``[label](url)`` to ``label`` so TTS does not narrate
    ``open bracket … close paren`` plus the URL when the voice route
    reads the brief verbatim. The visual surface keeps the original
    markdown; this only applies to the body returned for TTS."""
    return _MD_LINK_RE.sub(r"\1", text)


__all__ = ["BriefReadTool", "BriefReadInput"]
