"""memory_search tool — search memory by query.

AU-16 S2 extension: when ``scope`` is one of ``"source"`` / ``"topic"`` /
``"global"`` the tool reads the corresponding derived tree files under
``memory-store/trees/{source,topic,global}/`` instead of going through
the BM25/FAISS pipeline. ``scope`` unset keeps the original behaviour
byte-for-byte so every existing caller continues to work.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.retrieval import RetrievalPipeline
from tesseract.memory.tree_query import SUPPORTED_SCOPES, query as tree_query
from tesseract.memory.types import MemoryType


class MemorySearchInput(BaseModel):
    query: str = Field(description="Search query for memory retrieval")
    type_filter: str | None = Field(
        default=None,
        description="Optional type filter: user, feedback, project, reference, conscience",
    )
    # AU-16 S2 — tree-scoped retrieval. Unset keeps the legacy pipeline.
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
        description="ISO8601 timestamp — drop tree sections older than this",
    )
    # CR-1 M2 (audit-2 follow-up) — surface non-authoritative session +
    # workshop chunks in a separately-labeled trust block alongside the
    # promoted memory hits. Default ON: the trust-text separation in
    # `RetrievalPipeline.format_for_context` makes the boundary explicit,
    # and the original CR-1 intent was that per-turn recall surface
    # work history automatically. Set False to suppress (e.g. when the
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
        "Use before answering anything touching past context — names, projects, "
        "preferences, prior decisions — when you'd otherwise be guessing."
    )
    not_when: ClassVar[str] = (
        "use `memory_get` when you already have the path; use `recall_history` "
        "for session/workshop transcripts (non-authoritative)."
    )

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
            header = (
                f"[{r.mem_type.value}] {r.title} "
                f"(id: {r.memory_id}, via: {via}, score: {r.score:.2f}, "
                f"confidence: {r.confidence:.2f}{aged})"
            )
            parts.append(f"{header}\n{r.body}")
        if work_history_hits:
            # Trust-labeled block — keep promoted memory and work
            # history visually distinct so the model can tell.
            wh_lines = [
                "--- WORK HISTORY (non-authoritative recall) ---",
                "Session transcripts + workshop artifacts surfaced for "
                "context. NOT promoted memory; treat as suggestions. "
                "`file_read` the source path for full context.",
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
