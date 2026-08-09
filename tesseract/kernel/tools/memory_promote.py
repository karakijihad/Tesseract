"""memory_promote — operator-driven feedback-memory lifecycle actions.

Executes the merge / archive / bump / soul-growth proposals that the
feedback consolidator (Layer B of the durability plan) hands to the
operator. Each call is a small, traceable mutation:

- ``archive``           — flip ``stability`` → ``archived`` so the record
                          stops appearing in the Operator Directives
                          section but stays in the store for forensics.
- ``merge_into``        — copy the source body + auto_links into ``target``,
                          then archive the source. Target's importance is
                          raised to max(target, source) so the kept record
                          carries the stronger signal.
- ``bump_importance``   — clamp 1-10 then write. No re-embed needed.
- ``propose_soul_growth``— delegate to ``soul_growth_propose`` (passes
                          ``bullet`` through). Used when a feedback
                          pattern has hardened into an identity bullet.

The tool runs at ``ask`` posture by default — operator confirms each
mutation. The consolidator job *never* invokes this directly; it writes
JSONL proposals and emits a WS envelope, and the Workspace Inbox calls
the tool when the operator clicks Approve.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.soul_growth_propose import SoulGrowthProposeTool, SoulGrowthProposeInput
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import Stability

logger = logging.getLogger(__name__)


class MemoryPromoteInput(BaseModel):
    memory_id: str = Field(
        description="The memory ID to act on (e.g. mem_abc12345)."
    )
    action: Literal["archive", "merge_into", "bump_importance", "propose_soul_growth"] = Field(
        description=(
            "archive: stability→archived. "
            "merge_into: append body + links into `target`, archive this id. "
            "bump_importance: set new `importance` (1-10). "
            "propose_soul_growth: delegate to soul_growth_propose with `bullet`."
        )
    )
    target: str | None = Field(
        default=None,
        description="For merge_into: the id of the record that absorbs this one.",
    )
    importance: int | None = Field(
        default=None, ge=1, le=10,
        description="For bump_importance: the new importance value.",
    )
    bullet: str | None = Field(
        default=None,
        description="For propose_soul_growth: distilled bullet (≤240 chars).",
    )


class MemoryPromoteTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        soul_growth_tool: SoulGrowthProposeTool,
    ) -> None:
        self._store = store
        self._index = index
        self._soul = soul_growth_tool

    @property
    def name(self) -> str:
        return "memory_promote"

    @property
    def description(self) -> str:
        return (
            "Operator-confirmed feedback-memory lifecycle actions: archive, "
            "merge_into another record, bump_importance, or propose_soul_growth. "
            "Used by the feedback consolidator's approval flow — the assistant proposes, "
            "operator approves, this tool executes the single approved action."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return MemoryPromoteInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, MemoryPromoteInput)
            else MemoryPromoteInput(**tool_input.model_dump())
        )

        if inp.action == "propose_soul_growth":
            return await self._propose_soul_growth(inp, context)

        existing = self._store.read(inp.memory_id)
        if existing is None:
            return ToolResult(
                output=f"Memory {inp.memory_id} not found.", is_error=True,
            )
        fm, body = existing

        if inp.action == "archive":
            return self._archive(fm, body)
        if inp.action == "bump_importance":
            return self._bump(fm, body, inp.importance)
        if inp.action == "merge_into":
            return self._merge(fm, body, inp.target)

        return ToolResult(output=f"Unknown action: {inp.action}", is_error=True)

    def _archive(self, fm, body: str) -> ToolResult:
        if fm.stability == Stability.ARCHIVED:
            return ToolResult(output=f"{fm.id} already archived.")
        new_fm = fm.model_copy(update={
            "stability": Stability.ARCHIVED,
            "updated_at": datetime.now(timezone.utc),
        })
        if not self._store.write(new_fm, body):
            return ToolResult(output="Archive blocked by WHAT_NOT_TO_SAVE.", is_error=True)
        self._index.add(new_fm)
        return ToolResult(output=f"{fm.id} archived.")

    def _bump(self, fm, body: str, importance: int | None) -> ToolResult:
        if importance is None:
            return ToolResult(
                output="bump_importance requires `importance` (1-10).",
                is_error=True,
            )
        clamped = max(1, min(10, int(importance)))
        if clamped == fm.importance:
            return ToolResult(output=f"{fm.id} already importance={clamped}.")
        new_fm = fm.model_copy(update={
            "importance": clamped,
            "updated_at": datetime.now(timezone.utc),
        })
        if not self._store.write(new_fm, body):
            return ToolResult(output="Bump blocked by WHAT_NOT_TO_SAVE.", is_error=True)
        self._index.add(new_fm)
        return ToolResult(
            output=f"{fm.id} importance {fm.importance} → {clamped}.",
        )

    def _merge(self, source_fm, source_body: str, target_id: str | None) -> ToolResult:
        if not target_id:
            return ToolResult(
                output="merge_into requires `target` id.", is_error=True,
            )
        if target_id == source_fm.id:
            return ToolResult(
                output="Cannot merge a record into itself.", is_error=True,
            )
        target = self._store.read(target_id)
        if target is None:
            return ToolResult(
                output=f"Target {target_id} not found.", is_error=True,
            )
        target_fm, target_body = target

        # Idempotent retry path: if the source is already archived AND
        # the target carries our merge marker, this is a no-op success
        # (a previous run already absorbed it). If the source is
        # archived without the marker, something else archived it —
        # surface as an error so the operator can investigate instead
        # of double-appending the body.
        if source_fm.stability == Stability.ARCHIVED:
            marker = f"--- merged from {source_fm.id} ---"
            if marker in target_body:
                return ToolResult(
                    output=f"{source_fm.id} already merged into {target_id} (no-op).",
                )
            return ToolResult(
                output=(
                    f"{source_fm.id} is archived but no merge marker found in "
                    f"{target_id}; manual review needed before re-merging."
                ),
                is_error=True,
            )

        merged_body = _append_with_separator(target_body, source_body, source_fm.id)
        merged_links = _merge_link_lists(target_fm.auto_links, source_fm.auto_links, source_fm.id)
        merged_importance = max(target_fm.importance, source_fm.importance)

        new_target = target_fm.model_copy(update={
            "auto_links": merged_links,
            "importance": merged_importance,
            "updated_at": datetime.now(timezone.utc),
        })
        if not self._store.write(new_target, merged_body):
            return ToolResult(
                output="Merge blocked by WHAT_NOT_TO_SAVE on target.",
                is_error=True,
            )
        self._index.add(new_target)

        archived_source = source_fm.model_copy(update={
            "stability": Stability.ARCHIVED,
            "updated_at": datetime.now(timezone.utc),
        })
        if not self._store.write(archived_source, source_body):
            # Target merge succeeded; only the source-archive step failed.
            # is_error=False so the Inbox doesn't surface "merge failed" and
            # retry the merge — that would double-append the body. The output
            # makes the residual cleanup explicit.
            return ToolResult(
                output=(
                    f"Merged {source_fm.id} → {target_id}; source archive "
                    f"was blocked — {source_fm.id} still active, manual "
                    "archive needed."
                ),
            )
        self._index.add(archived_source)
        return ToolResult(
            output=f"Merged {source_fm.id} → {target_id}; source archived.",
        )

    async def _propose_soul_growth(self, inp: MemoryPromoteInput, ctx: ToolContext) -> ToolResult:
        if not (inp.bullet or "").strip():
            return ToolResult(
                output="propose_soul_growth requires `bullet`.", is_error=True,
            )
        return await self._soul.run(SoulGrowthProposeInput(bullet=inp.bullet), ctx)


def _append_with_separator(target_body: str, source_body: str, source_id: str) -> str:
    """Append `source_body` to `target_body` with a clear provenance line.

    The marker is plain prose (not YAML, not a wikilink) so the merged
    record reads naturally when the operator opens it. ``source_id`` is
    archived after the merge — the marker is the audit trail.
    """
    target = target_body.rstrip()
    source = source_body.strip()
    if not source:
        return target_body
    marker = f"\n\n--- merged from {source_id} ---\n"
    return f"{target}{marker}{source}\n"


def _merge_link_lists(target: list[str], source: list[str], source_id: str) -> list[str]:
    """Union of `target` + `source`, plus ``source_id`` as a back-pointer.

    Order-preserving: target's existing links come first, then any new
    ids from source, then ``source_id``. Duplicates dropped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for link in (*target, *source, source_id):
        if link and link not in seen:
            out.append(link)
            seen.add(link)
    return out
