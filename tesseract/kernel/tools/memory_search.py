"""memory_search tool — search memory by query.

When ``scope`` is one of ``"source"`` / ``"topic"`` /
``"global"`` the tool reads the corresponding derived tree files under
``memory-store/trees/{source,topic,global}/`` instead of going through
the BM25/FAISS pipeline. ``scope`` unset keeps that pipeline.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.retrieval import (
    TIER_NOTICE,
    UNKNOWN_TIER_LABEL,
    RetrievalPipeline,
)
from tesseract.memory.tree_query import SUPPORTED_SCOPES, query as tree_query
from tesseract.memory.types import MemoryType, SourceTier


class MemorySearchInput(BaseModel):
    query: str = Field(description="Search query for memory retrieval")
    type_filter: str | None = Field(
        default=None,
        description="Optional type filter: user, feedback, project, reference, conscience",
    )
    # Tree-scoped retrieval. Unset keeps the BM25/FAISS pipeline.
    scope: str | None = Field(
        default=None,
        description="Optional tree scope: source, topic, or global",
    )
    entity: str | None = Field(
        default=None,
        description="Entity slug for scope=topic (filters to the matching topic tree)",
    )
    source_slug: str | None = Field(
        default=None,
        description="Slugified source identifier for scope=source",
    )
    since: str | None = Field(
        default=None,
        description="ISO8601 timestamp. Drops tree sections older than this.",
    )
    # Surface non-authoritative session + workshop chunks in a
    # separately-labeled trust block alongside the promoted memory hits.
    # Default ON: the trust-text separation in
    # `RetrievalPipeline.format_for_context` makes the boundary explicit,
    # and per-turn recall is meant to surface work history without being
    # asked. Set False to suppress (e.g. when the
    # caller deliberately wants promoted-memory only).
    include_work_history: bool = Field(
        default=True,
        description=(
            "When true, augment results with non-authoritative session "
            "transcript + workshop chunks under a separate trust block."
        ),
    )


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class MemorySearchTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Search the persistent memory store by query, ranked by relevance."
    use_when: ClassVar[str] = (
        "Use before answering anything that turns on what the operator has "
        "decided, preferred or told you: names, projects, past choices. Their "
        "own records, in their words. Every turn already arrives with a few "
        "in the recalled-memories block, so reach for this when that block is "
        "thin or the question is broader than what was said."
    )
    not_when: ClassVar[str] = (
        "for a subject the library holds RESEARCH on, `vault_query` answers "
        "from the compiled wiki and is faster. For how two records RELATE "
        "rather than what either says, use `atlas_query`. Use `memory_get` "
        "when you already have the path, and `recall_history` for what was "
        "said in a past session, which is recall rather than settled fact."
    )
    depends_on: ClassVar[str] = ""
    receipt_kind: ClassVar[str] = "none"
    recovery_behaviour: ClassVar[str] = "read_only"

    def __init__(self, pipeline: RetrievalPipeline) -> None:
        self._pipeline = pipeline

    @property
    def pipeline(self) -> RetrievalPipeline:
        """Expose the retrieval pipeline so other callers (auto_recall —
        lean-agent-os P1 Task 3) reuse this exact retrieval entry point
        instead of standing up a parallel one."""
        return self._pipeline

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def input_schema(self) -> type[BaseModel]:
        return MemorySearchInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, MemorySearchInput)
            else MemorySearchInput(**tool_input.model_dump())
        )

        if inp.scope:
            return self._run_tree(inp)

        type_filter = None
        if inp.type_filter:
            try:
                type_filter = MemoryType(inp.type_filter)
            except ValueError:
                return ToolResult(
                    output=f"Invalid type filter: {inp.type_filter}",
                    is_error=True,
                )

        packet = await self._pipeline.retrieve(
            inp.query,
            type_filter=type_filter,
            include_work_history=inp.include_work_history,
        )

        work_history_hits = list(getattr(packet, "work_history", []) or [])
        if not packet.results and not work_history_hits:
            return ToolResult(output="No relevant memories found.")

        parts: list[str] = []
        if packet.short_circuited:
            parts.append("(exact slug match — high-trust hit)")
        for r in packet.results:
            via = "+".join(r.provenance) if r.provenance else "unknown"
            # `source deleted` says the conversation this was learned from is
            # gone, so there is nothing to re-read and no follow-up to quote.
            # The fact itself is unchanged — an old lesson, not a doubtful one.
            aged = ", source deleted" if r.source_deleted else ""
            tier = r.source_tier.value if r.source_tier else UNKNOWN_TIER_LABEL
            header = (
                f"[{r.mem_type.value}] {r.title} "
                f"(id: {r.memory_id}, via: {via}, score: {r.score:.2f}, "
                f"confidence: {r.confidence:.2f}, from: {tier}{aged})"
            )
            parts.append(f"{header}\n{r.body}")
        if work_history_hits:
            # Trust-labeled block — keep promoted memory and work
            # history visually distinct so the model can tell.
            wh_lines = [
                f"--- WORK HISTORY (from: {SourceTier.RECALLED.value}) ---",
            ]
            for h in work_history_hits:
                label = f"{h.source}:{h.source_ref}"
                ts_tag = f" @ {h.ts}" if h.ts else ""
                location = h.source_path
                if h.turn_idx is not None:
                    location = f"{location} (turn {h.turn_idx})"
                preview = (h.text or "").strip()
                if len(preview) > 480:
                    preview = preview[:480] + "…"
                wh_lines.append(f"\n[{label}]{ts_tag}  `{location}`\n{preview}")
            parts.append("\n".join(wh_lines))
        # One notice covering every hit, imported rather than restated: this
        # surface and the auto-recall block used to carry two hand-written
        # trust texts that could disagree, and each knew about work history
        # alone.
        parts.append(TIER_NOTICE.strip())
        return ToolResult(output="\n\n---\n\n".join(parts))

    def _run_tree(self, inp: MemorySearchInput) -> ToolResult:
        if inp.scope not in SUPPORTED_SCOPES:
            return ToolResult(
                output=(
                    f"Invalid scope: {inp.scope!r}. "
                    f"Expected one of {sorted(SUPPORTED_SCOPES)}."
                ),
                is_error=True,
            )
        since = _parse_since(inp.since)
        if inp.since and since is None:
            return ToolResult(
                output=f"Invalid since: {inp.since!r} (expected ISO8601)",
                is_error=True,
            )
        hits = tree_query(
            scope=inp.scope,
            query_text=inp.query,
            entity=inp.entity,
            source_slug=inp.source_slug,
            since=since,
        )
        if not hits:
            return ToolResult(output=f"No tree entries found in scope={inp.scope}.")
        parts: list[str] = []
        for hit in hits:
            parts.append(f"## {hit.title}\n\n{hit.body}".rstrip())
        return ToolResult(output="\n\n---\n\n".join(parts))
