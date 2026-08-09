"""GrepTool — searches file contents for a regex pattern via ripgrep.

Concurrent-safe, read-only. Spawns `rg` as a subprocess; cancel_event
triggers proc.terminate() for instant interrupt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

from tesseract.kernel.tools._path_anchor import anchor_read_path
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult


class GrepInput(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="File or directory to search in")
    glob: str = Field(default="**/*", description="Glob filter for files (e.g., '*.py')")
    context: int = Field(default=0, ge=0, le=10, description="Number of context lines before and after each match")
    max_results: int = Field(default=250, gt=0, le=1000, description="Maximum number of matches to return")


_WINGET_RG = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft" / "WinGet" / "Packages"
    / "BurntSushi.ripgrep.MSVC_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "ripgrep-15.1.0-x86_64-pc-windows-msvc" / "rg.exe"
)


@lru_cache(maxsize=1)
def _resolve_rg() -> str | None:
    hit = shutil.which("rg")
    if hit:
        return hit
    if _WINGET_RG.exists():
        return str(_WINGET_RG)
    return None


def _translate_glob(glob: str) -> str:
    """Translate Pythonic glob to rg-friendly form.

    `**/*` matches all files by default in rg; drop the prefix.
    Anything else passes through — rg's glob syntax is a superset.
    """
    return "*" if glob == "**/*" else glob


class GrepTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers."

    @property
    def input_schema(self) -> type[BaseModel]:
        return GrepInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, GrepInput) else GrepInput(**tool_input.model_dump())

        rg = _resolve_rg()
        if rg is None:
            return ToolResult(
                output="ripgrep (rg) not on PATH. Install via `winget install BurntSushi.ripgrep.MSVC` and restart the shell.",
                is_error=True,
            )

        search_path = anchor_read_path(inp.path, context.workspace_root)
        if not search_path.exists():
            return ToolResult(output=f"Path not found: {search_path}", is_error=True)

        argv: list[str] = [
            rg,
            "--line-number",
            "--with-filename",
            "--color=never",
            "--no-heading",
            "--max-count", str(inp.max_results),
            "--glob", _translate_glob(inp.glob),
        ]
        if inp.context > 0:
            argv += ["--context", str(inp.context)]
        argv += ["--", inp.pattern, str(search_path)]

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _watch_cancel() -> None:
            while proc.returncode is None:
                if context.cancel_event.is_set():
                    proc.terminate()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(_watch_cancel())
        try:
            stdout, stderr = await proc.communicate()
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

        if context.cancel_event.is_set():
            raise asyncio.CancelledError

        # rg: 0 = matches, 1 = no matches, 2+ = error
        if proc.returncode == 1:
            return ToolResult(output=f"No matches for '{inp.pattern}' in {search_path}")
        if proc.returncode and proc.returncode > 1:
            err = stderr.decode("utf-8", errors="replace").strip() or f"rg exited {proc.returncode}"
            return ToolResult(output=err, is_error=True)

        raw = stdout.decode("utf-8", errors="replace").rstrip("\n")
        lines = raw.split("\n") if raw else []
        if len(lines) > inp.max_results:
            lines = lines[: inp.max_results]
        files_with_matches = len({line.split(":", 1)[0] for line in lines if ":" in line})
        header = f"{len(lines)} matches in {files_with_matches} files"
        return ToolResult(output=header + "\n" + "\n".join(lines))
