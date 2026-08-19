"""memory_forget tool — request deletion of a memory."""

from __future__ import annotations

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.memory.embeddings import EmbeddingIndex
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore


class MemoryForgetInput(BaseModel):
    memory_id: str = Field(description="The memory ID to delete (e.g. mem_abc12345)")


class MemoryForgetTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Permanently delete a memory: its file, index entry, and embedding."
    use_when: ClassVar[str] = (
        "Use sparingly and only on operator request to remove a memory by id. "
        "Deletion is irreversible — the file is removed from disk."
    )
    not_when: ClassVar[str] = (
        "use `memory_promote` action=archive to retire a memory while keeping "
        "it for forensics."
    )

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
        return "memory_forget"

    @property
    def input_schema(self) -> type[BaseModel]:
        return MemoryForgetInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, MemoryForgetInput) else MemoryForgetInput(**tool_input.model_dump())

        if not self._store.delete(inp.memory_id):
            return ToolResult(output=f"Memory {inp.memory_id} not found.", is_error=True)

        self._index.remove(inp.memory_id)
        if self._embeddings is not None:
            try:
                self._embeddings.remove(inp.memory_id)
            except Exception:
                pass  # vector removal is best-effort; store truth is the file

        if self._fts_index is not None:
            try:
                self._fts_index.delete(inp.memory_id)
            except Exception:
                pass

        return ToolResult(output=f"Memory {inp.memory_id} deleted.")
