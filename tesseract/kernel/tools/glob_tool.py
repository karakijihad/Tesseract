"""GlobTool — finds files matching a glob pattern.

Concurrent-safe, read-only.
"""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob pattern to match files (e.g., '**/*.py', 'src/**/*.ts')")
    path: str = Field(default=".", description="Directory to search in (default: workspace root)")


class GlobTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return "Find files matching a glob pattern. Returns matching file paths sorted by modification time."

    @property
    def input_schema(self) -> type[BaseModel]:
        return GlobInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        if context.cancel_event.is_set():
            raise asyncio.CancelledError
        inp = tool_input if isinstance(tool_input, GlobInput) else GlobInput(**tool_input.model_dump())
        search_dir = Path(inp.path)
        if not search_dir.is_absolute():
            search_dir = Path(context.workspace_root) / search_dir

        if not search_dir.exists():
            return ToolResult(output=f"Directory not found: {search_dir}", is_error=True)

        try:
            matches = sorted(search_dir.glob(inp.pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError as e:
            return ToolResult(output=f"Glob error: {e}", is_error=True)

        if not matches:
            return ToolResult(output=f"No files matching '{inp.pattern}' in {search_dir}")

        max_results = 250
        lines = [str(p) for p in matches[:max_results]]
        output = "\n".join(lines)
        if len(matches) > max_results:
            output += f"\n... and {len(matches) - max_results} more"

        return ToolResult(output=output, metadata={"count": len(matches)})
