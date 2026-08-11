"""GlobTool — finds files matching a glob pattern.

Concurrent-safe, read-only.

`Path.glob` understands `*`, `**`, `?` and `[seq]` and nothing else, and it
reports everything it cannot express as zero matches. A caller reading
"No files matching '**/*.{log,jsonl}'" concludes the files are absent, when
what happened is that the tool cannot say what was asked. That false negative
is expensive precisely when it fires: during diagnosis, where the next
conclusion is built on top of it.

So braces are expanded here rather than passed down, and any other syntax the
engine silently degrades is refused by name. Two shapes also used to escape
the handler entirely — an absolute pattern raises `NotImplementedError` and an
empty one `ValueError`, neither of which is an `OSError` — so the tool crashed
instead of answering.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools._path_anchor import anchor_read_path
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

# The innermost `{…}` group that actually offers a choice — no braces inside
# and at least one comma — so repeated application expands nested alternations
# from the inside out.
#
# The comma requirement is what shells do: `{a,b}` alternates, while `{}` and
# `{a}` are literal text. Without it, `{}.txt` expanded to `.txt` — missing a
# file genuinely named `{}.txt` and matching a different one instead. It also
# makes the loop terminate: a group left literal must stop matching, or the
# `while` below never ends.
_INNERMOST_ALTERNATION = re.compile(r"\{([^{}]*,[^{}]*)\}")

# Syntax `Path.glob` accepts as literal characters and therefore matches
# nothing, rather than rejecting. POSIX classes come from shell/ripgrep habit;
# extglob from bash. Both read as working patterns and both return empty.
_POSIX_CLASS = re.compile(r"\[:[a-zA-Z]+:\]")
_EXTGLOB = re.compile(r"[?*+@!]\(")

# A brace pattern expands multiplicatively: `{a,b}/{c,d}/{e,f}` is 8 globs,
# each a full tree walk. The cap is a guard against a pattern whose cost is
# invisible in its own text, not a judgement about what is reasonable to ask.
_MAX_EXPANSIONS = 64

_MAX_RESULTS = 250


class GlobInput(BaseModel):
    pattern: str = Field(
        description=(
            "Glob pattern to match files (e.g., '**/*.py', 'src/**/*.ts'). "
            "Brace alternation is supported: '**/*.{log,jsonl}'."
        )
    )
    path: str = Field(default=".", description="Directory to search in (default: workspace root)")


class UnsupportedPattern(ValueError):
    """A pattern this tool cannot honour. Carries the operator-facing reason."""


def expand_braces(pattern: str) -> list[str]:
    """Expand `{a,b}` alternations into the concrete patterns they stand for.

    Order is preserved left to right so the result is deterministic. Nested
    braces expand inside out. A brace group with an empty option (`{a,}`) is
    honoured — that is how a caller asks for "with or without this segment" —
    while a group with no comma at all (`{}`, `{a}`) is literal text, as in a
    shell, and is returned untouched.
    """
    patterns = [pattern]
    while any(_INNERMOST_ALTERNATION.search(candidate) for candidate in patterns):
        expanded: list[str] = []
        for candidate in patterns:
            hit = _INNERMOST_ALTERNATION.search(candidate)
            if hit is None:
                expanded.append(candidate)
                continue
            head, tail = candidate[: hit.start()], candidate[hit.end() :]
            for option in hit.group(1).split(","):
                expanded.append(head + option + tail)
                if len(expanded) > _MAX_EXPANSIONS:
                    raise UnsupportedPattern(
                        f"brace expansion exceeds the limit of "
                        f"{_MAX_EXPANSIONS} patterns. Narrow the pattern or "
                        "issue separate calls."
                    )
        patterns = expanded
    # Inside-out expansion turns a nested group into a cross product, so
    # `{a,{b,c}}` yields `a` twice. Deduplicating here makes the result agree
    # with shell semantics and stops one pattern being walked twice.
    return list(dict.fromkeys(patterns))


def _reject_unsupported(pattern: str) -> None:
    """Refuse syntax the glob engine would silently treat as literal text."""
    if not pattern:
        raise UnsupportedPattern("pattern is empty")
    # A shell would treat `{a,b` as literal text and show it back. Here the
    # same leniency prints "No files matching", which reads as an answer about
    # the disk — the precise false negative this tool was rewritten to stop.
    # One missing character is the likeliest way to write a brace pattern
    # wrongly, so it must be the loudest failure, not the quietest.
    if pattern.count("{") != pattern.count("}"):
        raise UnsupportedPattern(
            f"unbalanced braces in {pattern!r} — {pattern.count('{')} '{{' "
            f"against {pattern.count('}')} '}}'. A brace pattern reads "
            "'**/*.{log,jsonl}'."
        )
    if Path(pattern).is_absolute() or pattern.startswith(("/", "\\")):
        raise UnsupportedPattern(
            f"pattern {pattern!r} is absolute. Pass the directory as `path` "
            "and keep the pattern relative to it."
        )
    if _POSIX_CLASS.search(pattern):
        raise UnsupportedPattern(
            f"POSIX character classes (e.g. '[:alpha:]') are not supported by "
            f"this tool's glob engine, which understands '*', '**', '?' and "
            f"'[seq]'. Pattern was {pattern!r}."
        )
    if _EXTGLOB.search(pattern):
        raise UnsupportedPattern(
            f"extended-glob groups (e.g. '@(a|b)') are not supported by this "
            f"tool's glob engine. Use brace alternation — '{{a,b}}' — instead. "
            f"Pattern was {pattern!r}."
        )


def _mtime(path: Path) -> float:
    """Sort key that survives a file vanishing mid-walk.

    A single unreadable entry used to abort the whole call through the
    `OSError` handler, discarding every result already found.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class GlobTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Find files matching a glob pattern, newest first. Supports '*', "
            "'**', '?', '[seq]' and brace alternation '{a,b}'."
        )

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
        search_dir = anchor_read_path(inp.path, context.workspace_root)

        if not search_dir.exists():
            return ToolResult(output=f"Directory not found: {search_dir}", is_error=True)

        try:
            _reject_unsupported(inp.pattern)
            patterns = expand_braces(inp.pattern)
        except UnsupportedPattern as e:
            return ToolResult(output=f"Unsupported glob pattern: {e}", is_error=True)

        # Dedup across expansions: `{py,*}` legitimately overlaps, and the
        # same file surfacing twice would misreport the count.
        seen: dict[Path, None] = {}
        try:
            for candidate in patterns:
                for hit in search_dir.glob(candidate):
                    seen[hit] = None
        except (OSError, ValueError, NotImplementedError) as e:
            return ToolResult(output=f"Glob error: {e}", is_error=True)

        matches = sorted(seen, key=_mtime, reverse=True)

        if not matches:
            expanded_note = ""
            if len(patterns) > 1:
                expanded_note = f" (expanded to {len(patterns)} patterns: {', '.join(patterns)})"
            return ToolResult(output=f"No files matching '{inp.pattern}'{expanded_note} in {search_dir}")

        lines = [str(p) for p in matches[:_MAX_RESULTS]]
        output = "\n".join(lines)
        if len(matches) > _MAX_RESULTS:
            output += f"\n... and {len(matches) - _MAX_RESULTS} more"

        return ToolResult(output=output, metadata={"count": len(matches)})
