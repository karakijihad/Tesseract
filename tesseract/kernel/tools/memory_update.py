"""memory_update tool — update an existing memory.

Re-embedding is best-effort (Ollama optional).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter

logger = logging.getLogger(__name__)


class MemoryUpdateInput(BaseModel):
    memory_id: str = Field(description="The memory ID to update (e.g. mem_abc12345)")
    content: str | None = Field(default=None, description="New body content")
    title: str | None = Field(default=None, description="New title")
    importance: int | None = Field(default=None, ge=1, le=10, description="New importance 1-10")
    tags: list[str] | None = Field(default=None, description="New tags (replaces existing)")
    source_path: str | None = Field(default=None, description="Vault-relative path to link")
    source_url: str | None = Field(default=None, description="Original URL if web source")
    source_type: str | None = Field(default=None, description="Source type: chat, upload, paper, article, data, snapshot, imagination, observation")


class MemoryUpdateTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    def __init__(
        self,
        store: MemoryStore,
        index: MemoryIndex,
        embeddings: EmbeddingIndex | None = None,
        fts_index=None,
    ) -> None:
        self._store = store
        self._index = index
        self._embeddings = embeddings
        self._fts_index = fts_index

    @property
    def name(self) -> str:
        return "memory_update"

    @property
    def description(self) -> str:
        return "Update an existing memory's content, title, importance, or tags."

    @property
    def input_schema(self) -> type[BaseModel]:
        return MemoryUpdateInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, MemoryUpdateInput) else MemoryUpdateInput(**tool_input.model_dump())

        existing = self._store.read(inp.memory_id)
        if existing is None:
            return ToolResult(output=f"Memory {inp.memory_id} not found.", is_error=True)

        fm, body = existing
        now = datetime.now(timezone.utc)

        new_title = inp.title if inp.title is not None else fm.title
        new_importance = inp.importance if inp.importance is not None else fm.importance
        new_tags = inp.tags if inp.tags is not None else fm.tags
        new_body = inp.content if inp.content is not None else body
        new_summary = new_body[:100] if inp.content and len(new_body) > 100 else fm.summary

        new_source_path = inp.source_path if inp.source_path is not None else fm.source_path
        new_source_url = inp.source_url if inp.source_url is not None else fm.source_url
        new_source_type = inp.source_type if inp.source_type is not None else fm.source_type

        new_fm = MemoryFrontmatter(
            id=fm.id,
            type=fm.type,
            title=new_title,
            summary=new_summary,
            created_at=fm.created_at,
            updated_at=now,
            importance=new_importance,
            tags=new_tags,
            entities=fm.entities,
            links=fm.links,
            auto_links=fm.auto_links,
            source_session=fm.source_session,
            source_path=new_source_path,
            source_url=new_source_url,
            source_type=new_source_type,
            stability=fm.stability,
            # Belief-state fields — preserve verbatim. Without these the slug
            # would silently vanish from the canonical decision index on any
            # edit (reviewer finding #1, 2026-04-29).
            slug=fm.slug,
            confidence=fm.confidence,
            expiry_at=fm.expiry_at,
        )

        if new_source_path:
            wikilink = f"[[{new_source_path}]]"
            if wikilink not in new_body:
                # Remove any existing Source: [[...]] line before appending new one
                lines = new_body.split("\n")
                lines = [l for l in lines if not l.startswith("Source: [[")]
                new_body = "\n".join(lines).rstrip()
                new_body = f"{new_body}\n\nSource: {wikilink}"

        if not self._store.write(new_fm, new_body):
            return ToolResult(output="Update blocked by WHAT_NOT_TO_SAVE.", is_error=True)

        if new_fm.id != fm.id:
            self._store.delete(fm.id)

        self._index.add(new_fm)
        if inp.content is not None and self._embeddings is not None:
            try:
                await self._embeddings.add(fm.id, new_body)
            except Exception as e:
                logger.warning("embed on update failed for %s: %s", fm.id, e)

        if self._fts_index is not None:
            try:
                self._fts_index.add(fm.id, new_fm.title, new_body)
            except Exception:
                pass

        return ToolResult(output=f"Memory {fm.id} updated.")
