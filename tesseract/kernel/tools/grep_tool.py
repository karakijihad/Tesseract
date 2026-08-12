"""GrepTool — searches file contents for a regex pattern via ripgrep.

Concurrent-safe, read-only. Spawns `rg` as a subprocess; cancel_event
triggers proc.terminate() for instant interrupt.

Two numbers in the summary line used to be untrue, which matters because the
summary is what a caller reads before deciding whether to look further:

- `max_results` was passed to `--max-count`, which bounds matches PER FILE,
  and the header then printed the truncated line count as though it were the
  total. A search that hit the ceiling reported exactly the ceiling, with
  nothing to say it had been cut.
- The file tally split each line on its first colon. Every path on this
  platform is absolute and begins `C:\\`, so the tally was the number of
  distinct drive letters — 112 files reported as 1, on every install.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools._path_anchor import ReadPathRefused, anchor_read_path
from tesseract.paths import secret_exclusion_globs
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

# `<path>:<line>:<text>` for a match, `<path>-<line>-<text>` for a context
# line, and a bare `--` between context groups.
#
# Splitting on the first colon is wrong twice over: it takes the drive letter
# on Windows, and it gives up entirely on a POSIX path containing a colon —
# which produced a summary of ZERO matches while the matching lines were right
# there in the output. So the known search root is stripped first, since
# ripgrep echoes back exactly the path argument it was given, and only the
# remainder is parsed.
_AFTER_ROOT_MATCH = re.compile(r"^(?P<rest>[^:]*):(?P<line>\d+):")
_AFTER_ROOT_CONTEXT = re.compile(r"^(?P<rest>[^:]*)-(?P<line>\d+)-")
# Consulted only when the strict form above fails, which happens when the path
# itself contains a colon. Greedy, so it takes the LAST `:<digits>:` — right
# for a colon in the path, wrong if the matched CONTENT also contains one.
# Ordering settles that trade: content holding `:12:` is common and is parsed
# correctly by the strict pattern, so this is reached only in the rare case
# where the strict one has already ruled itself out.
_AFTER_ROOT_MATCH_GREEDY = re.compile(r"^(?P<rest>.*):(?P<line>\d+):")
# Fallback for output that does not carry the root prefix.
_MATCH_LINE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:]*):(?P<line>\d+):")
_CONTEXT_LINE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:]*)-(?P<line>\d+)-")


def classify_line(line: str, search_root: str) -> tuple[str, str | None]:
    """Sort one ripgrep output line into (kind, path).

    `kind` is 'match', 'context', 'separator' or 'unknown'. Only 'match' is
    counted, and 'unknown' is reported rather than dropped — a line the parser
    cannot attribute must never quietly reduce the total, which is how the
    zero-match summary happened.
    """
    if not line:
        return "separator", None
    if line == "--":
        return "separator", None

    root = search_root.rstrip("\\/")
    if root and line.startswith(root):
        remainder = line[len(root) :]
        hit = _AFTER_ROOT_MATCH.match(remainder)
        if hit is not None:
            return "match", root + hit.group("rest")
        ctx = _AFTER_ROOT_CONTEXT.match(remainder)
        if ctx is not None:
            return "context", root + ctx.group("rest")
        loose = _AFTER_ROOT_MATCH_GREEDY.match(remainder)
        if loose is not None:
            return "match", root + loose.group("rest")

    hit = _MATCH_LINE.match(line)
    if hit is not None:
        return "match", hit.group("path")
    if _CONTEXT_LINE.match(line) is not None:
        return "context", None
    return "unknown", None


def summarise(raw: str, search_root: str, max_results: int) -> tuple[str, list[str], dict]:
    """Turn ripgrep's stdout into (header, lines_to_show, metadata).

    Pure — no subprocess, no filesystem — so the reporting logic that used to
    be reachable only through a machine-local ripgrep binary can be tested
    directly. It previously could not be, and both of its numbers were wrong.
    """
    lines = raw.split("\n") if raw else []

    total_matches = 0
    unknown = 0
    files: set[str] = set()
    for line in lines:
        kind, path = classify_line(line, search_root)
        if kind == "match":
            total_matches += 1
            if path is not None:
                files.add(path)
        elif kind == "unknown":
            unknown += 1

    shown = lines[:max_results]
    match_word = "match" if total_matches == 1 else "matches"
    file_word = "file" if len(files) == 1 else "files"
    header = f"{total_matches} {match_word} in {len(files)} {file_word}"
    if len(lines) > len(shown):
        header += f" — showing the first {len(shown)} lines (display cap: max_results={max_results})"
    if unknown:
        header += f" — WARNING: {unknown} output line(s) could not be attributed to a file, so the total may undercount"

    return (
        header,
        shown,
        {
            "total_matches": total_matches,
            "files_with_matches": len(files),
            "truncated": len(lines) > len(shown),
            "unattributed_lines": unknown,
        },
    )


class GrepInput(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="File or directory to search in")
    glob: str = Field(default="**/*", description="Glob filter for files (e.g., '*.py')")
    context: int = Field(default=0, ge=0, le=10, description="Number of context lines before and after each match")
    max_results: int = Field(
        default=250,
        gt=0,
        le=1000,
        description=(
            "Maximum number of matches to DISPLAY. The reported total is the "
            "true match count; output beyond this is summarised, not dropped "
            "silently."
        ),
    )


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

        try:
            search_path = anchor_read_path(inp.path, context.workspace_root)
        except ReadPathRefused as exc:
            return ToolResult(output=str(exc), is_error=True)
        if not search_path.exists():
            return ToolResult(output=f"Path not found: {search_path}", is_error=True)

        argv: list[str] = [
            rg,
            "--line-number",
            "--with-filename",
            "--color=never",
            "--no-heading",
            "--glob", _translate_glob(inp.glob),
        ]
        # ripgrep skips hidden and gitignored files by default, which already
        # covers `.env` — but that is someone else's default, not this tool's
        # decision, and it would stop holding the moment anyone adds `--hidden`
        # or `--no-ignore` here. Stated explicitly so the guarantee survives
        # that edit. Later globs win in rg, so these follow the caller's.
        #
        # `--iglob`, not `--glob`: rg matches globs case-sensitively, while
        # `is_secret_filename` casefolds and NTFS preserves case. A file the
        # operator created as `SECRETS.YAML` would otherwise be refused by
        # every other read tool and searched by this one.
        for secret in secret_exclusion_globs():
            argv += ["--iglob", f"!{secret}"]
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
        header, shown, metadata = summarise(raw, str(search_path), inp.max_results)
        return ToolResult(output=header + "\n" + "\n".join(shown), metadata=metadata)
