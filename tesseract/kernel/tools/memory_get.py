"""memory_get tool — read-only path-scoped fetch from ``memory-store/``.

Audit-4 §M3 ask: the assistant needs an inspectable, narrow read for memory
files (path + line range) instead of widening ``file_read`` to cover
the memory store. ``memory_get`` enforces three rules:

1. The resolved path must live under ``TESSERACT_HOME/memory-store/``.
2. The path must end in ``.md`` (memory store is markdown-only).
3. Identity files (``MEMORY.md``, ``WHAT_NOT_TO_SAVE.md``) are off-
   limits — they encode the assistant's promoted memory and exclusion policy
   respectively; reflection-driven workflows must not introspect them.

Returns the requested line slice (1-based, inclusive) with a header
echoing the resolved path and total line count.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.paths import TESSERACT_HOME

MEMORY_STORE_DIRNAME = "memory-store"
_IDENTITY_FILES: frozenset[str] = frozenset({"MEMORY.md", "WHAT_NOT_TO_SAVE.md"})


class MemoryGetInput(BaseModel):
    path: str = Field(
        description="Memory-store-relative path (e.g. 'project/foo.md') ending in .md.",
    )
    line_start: int = Field(
        default=1, ge=1, description="1-based starting line (default 1)."
    )
    line_end: int = Field(
        default=0,
        ge=0,
        description="1-based inclusive ending line. 0 = end-of-file (default).",
    )


def _resolve_memory_path(raw: str, *, memory_root: Path) -> Path:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("path is empty")
    if not cleaned.endswith(".md"):
        raise ValueError(f"memory_get path must end in .md: {cleaned!r}")
    rel = cleaned.lstrip("/").replace("\\", "/")
    prefix = f"tesseract/{MEMORY_STORE_DIRNAME}/"
    if rel.startswith(prefix):
        rel = rel[len(prefix):]
    elif rel.startswith(f"{MEMORY_STORE_DIRNAME}/"):
        rel = rel[len(MEMORY_STORE_DIRNAME) + 1:]
    if not rel:
        raise ValueError("path resolves to memory-store root")
    candidate = (memory_root / rel).resolve()
    try:
        candidate.relative_to(memory_root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes memory-store: {raw!r}") from exc
    if candidate.name in _IDENTITY_FILES:
        raise ValueError(f"identity file off-limits: {candidate.name}")
    return candidate


class MemoryGetTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = "Read a line-range slice of one known memory-store markdown file."
    use_when: ClassVar[str] = (
        "Use when you already know the memory-store path and want an exact, "
        "line-numbered slice rather than a ranked search."
    )
    not_when: ClassVar[str] = (
        "use `memory_search` when you don't already have the path. Refuses "
        "identity files (MEMORY.md, WHAT_NOT_TO_SAVE.md)."
    )

    def __init__(self, *, memory_root: Path | None = None) -> None:
        self._memory_root = memory_root if memory_root is not None else TESSERACT_HOME / MEMORY_STORE_DIRNAME

    @property
    def name(self) -> str:
        return "memory_get"

    @property
    def input_schema(self) -> type[BaseModel]:
        return MemoryGetInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: MemoryGetInput = tool_input  # type: ignore[assignment]
        try:
            path = _resolve_memory_path(inp.path, memory_root=self._memory_root)
        except ValueError as exc:
            return ToolResult(output=f"memory_get rejected: {exc}", is_error=True)
        if not path.is_file():
            return ToolResult(output=f"memory_get: not found: {inp.path}", is_error=True)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(output=f"memory_get: read error: {exc}", is_error=True)
        lines = text.splitlines()
        total = len(lines)
        start_idx = max(0, inp.line_start - 1)
        end_idx = total if inp.line_end == 0 else min(total, inp.line_end)
        if start_idx >= total:
            selected: list[str] = []
        else:
            selected = lines[start_idx:end_idx]
        rel_display = path.relative_to(self._memory_root.resolve()).as_posix()
        header = f"memory-store/{rel_display} ({total} lines)"
        if inp.line_start > 1 or (inp.line_end and inp.line_end < total):
            header += f", showing lines {start_idx + 1}-{end_idx}"
        body_lines = [f"{start_idx + i + 1}\t{ln}" for i, ln in enumerate(selected)]
        return ToolResult(
            output=header + "\n" + "\n".join(body_lines),
            metadata={
                "path": rel_display,
                "total_lines": total,
                "line_start": start_idx + 1,
                "line_end": end_idx,
            },
        )


__all__ = ["MemoryGetTool", "MemoryGetInput"]
