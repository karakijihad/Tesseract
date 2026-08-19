"""FileReadTool — reads a file and returns its contents.

Concurrent-safe, read-only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools._path_anchor import ReadPathRefused, anchor_read_path
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


def _log_skill_read(path: Path, session_id: str, *, is_error: bool) -> None:
    """Phase 4 4a — best-effort skill-usage telemetry when a SKILL.md body is
    read. Imported lazily so `file_read` carries no telemetry import cost on
    the common (non-skill) path and never fails on a telemetry hiccup."""
    try:
        from tesseract.brain.skill_usage import maybe_log_skill_load

        maybe_log_skill_load(path, session_id, is_error=is_error)
    except Exception:  # noqa: BLE001 — telemetry is never load-bearing
        pass


class FileReadInput(BaseModel):
    file_path: str = Field(description="Absolute or workspace-relative path to the file to read")
    offset: int = Field(default=0, ge=0, description="Line number to start reading from (0-based)")
    limit: int = Field(default=2000, gt=0, le=10000, description="Maximum number of lines to read")


class FileReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    # Audit-3 M9 — file contents are untrusted: any markdown file in the
    # repo / vault could carry a prompt-injection payload that the model
    # would otherwise treat as fresh system text. ChatSession wraps the
    # output in the UNTRUSTED_TOOL_OUTPUT envelope before history append.
    untrusted_source: ClassVar[bool] = True

    group: ClassVar[str] = "files-on-disk"
    summary: ClassVar[str] = "Read a file's contents as numbered lines."
    use_when: ClassVar[str] = (
        "You know the path and want its contents, or a specific line range of them."
    )
    not_when: ClassVar[str] = (
        "Use `grep` when you want the matching lines rather than the whole file, "
        "`glob` when you don't know the path yet, and `pdf_read` for a PDF."
    )

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return FileReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        inp = tool_input if isinstance(tool_input, FileReadInput) else FileReadInput(**tool_input.model_dump())
        try:
            path = anchor_read_path(inp.file_path, context.workspace_root)
        except ReadPathRefused as exc:
            return ToolResult(output=str(exc), is_error=True)

        if not path.exists():
            _log_skill_read(path, context.session_id, is_error=True)
            return ToolResult(output=f"File not found: {path}", is_error=True)
        if not path.is_file():
            _log_skill_read(path, context.session_id, is_error=True)
            return ToolResult(output=f"Not a file: {path}", is_error=True)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _log_skill_read(path, context.session_id, is_error=True)
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        _log_skill_read(path, context.session_id, is_error=False)

        lines = text.splitlines()
        selected = lines[inp.offset : inp.offset + inp.limit]
        numbered = [f"{i + inp.offset + 1}\t{line}" for i, line in enumerate(selected)]

        total = len(lines)
        header = f"File: {path} ({total} lines total)"
        if inp.offset > 0 or inp.offset + inp.limit < total:
            header += f", showing lines {inp.offset + 1}-{min(inp.offset + inp.limit, total)}"

        return ToolResult(output=header + "\n" + "\n".join(numbered))
