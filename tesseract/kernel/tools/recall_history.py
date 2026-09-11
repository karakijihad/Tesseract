"""recall_history — read-only retrieval over session + workshop chunks.

Hits carry ``session:`` / ``workshop:`` provenance
labels and source paths so the model can ``file_read`` for the full
context.

**One principal.** The index is install-wide and the search takes no owner or
chat predicate, because an install has exactly one person in it. A channel
allowlist is a trust boundary, not a permission tier: approving someone gives
them the assistant, and the assistant remembers everything the operator has
done. That is the reason ``run()`` ignores the ``chat_id`` its ``ToolContext``
carries, and it is stated in ``SECURITY.md`` where a reader can act on it. Trust-text reminds the caller that work-history hits are
non-authoritative — they are suggestions for recall, NOT promoted
facts. Promotion to memory still goes through the librarian /
reflection paths.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.work_index import WorkIndex


_TRUST_PREAMBLE = (
    "Work-history hits — these are non-authoritative suggestions from "
    "session transcripts and workshop artifacts. They are NOT promoted "
    "memory; treat them as recall, not truth. For full context, "
    "`file_read` the source path of an interesting hit."
)


class RecallHistoryInput(BaseModel):
    query: str = Field(description="What to recall (free text). Required.")
    source: Literal["session", "workshop", "both"] = Field(
        default="both",
        description="Limit hits to one source kind. Default 'both'.",
    )
    since: str | None = Field(
        default=None,
        description="ISO-8601 lower bound on chunk timestamp (e.g. '2026-05-01').",
    )
    until: str | None = Field(
        default=None,
        description="ISO-8601 upper bound on chunk timestamp.",
    )
    top_k: int = Field(default=5, ge=1, le=25,
                       description="Max number of hits to return (1-25).")


class RecallHistoryTool(Tool):
    """Search the work-history index. Always read-only, never mutates."""

    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Search past session transcripts and workshop artifacts by free text."
    use_when: ClassVar[str] = (
        "Use to recall what was said or done in a prior session or workshop "
        "file. Returns ranked hits with a path to `file_read` for the whole "
        "thing."
    )
    not_when: ClassVar[str] = (
        "for what the operator has settled on, use `memory_search`: a hit "
        "here is what was said once, not what was decided, so never quote it "
        "back as a decision. For research the library keeps, use "
        "`vault_query`."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, index: WorkIndex) -> None:
        self._index = index

    @property
    def name(self) -> str:
        return "recall_history"

    @property
    def input_schema(self) -> type[BaseModel]:
        return RecallHistoryInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, RecallHistoryInput)
            else RecallHistoryInput(**tool_input.model_dump())
        )
        query = inp.query.strip()
        if not query:
            return ToolResult(
                output="recall_history: query is required",
                is_error=True,
            )
        hits = self._index.search(
            query,
            source=inp.source,
            since=inp.since,
            until=inp.until,
            top_k=inp.top_k,
        )
        if not hits:
            return ToolResult(
                output=f"recall_history: no results for {query!r}.",
                is_error=False,
            )
        lines = [_TRUST_PREAMBLE, ""]
        for h in hits:
            label = f"{h.source}:" + (h.source_ref or "")
            ts_tag = f" @ {h.ts}" if h.ts else ""
            location = h.source_path
            if h.turn_idx is not None:
                location = f"{location} (turn {h.turn_idx})"
            preview = h.text.strip()
            if len(preview) > 480:
                preview = preview[:480] + "…"
            lines.append(f"- **{label}**{ts_tag} — `{location}`")
            lines.append(f"  {preview}")
        return ToolResult(
            output="\n".join(lines),
            is_error=False,
            metadata={"hits": len(hits)},
        )
