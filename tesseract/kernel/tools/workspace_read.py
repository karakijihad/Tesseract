"""workspace_read tool — the read owner for the assistant's own workspace.

**The defect this closes.** The system prompt points at documents in the
workspace tree: `WORKSHOP.md`, `DIARY.md`, and one `SKILL.md` per playbook.
Those pointers are emitted after checking the STATE tree (`workspace_dir()`,
which travels with `TESSERACT_HOME`), and the read tools anchor a relative path
against the CODE tree. In a dev checkout the two coincide, because the code
tree happens to contain a workspace, so every pointer resolves and the defect is
invisible. In a packaged install `app/` holds no workspace at all, so the
runtime spends prompt on telling the assistant to read files it cannot open.

**Why an owner and not another entry in `paths.READABLE_STATE_PREFIXES`.**
That list is a prefix match, and the shortest prefix covering the pointers is
`workspace`, which also reaches `_shipping/` (the build source deciding what
every user receives) and `canvas-state/`. Nothing points at either and neither
is an operator document. A read allowlist wide enough for the pointers is wider
than the pointers, so the bound is written here instead, the way `memory_get`
owns `memory-store/` rather than `file_read` growing a prefix for it.

**What it may read, and why that is the whole list.** The six operator
documents and anything under `skills/`. Three of the six are already inlined
into the prompt verbatim and a fourth on channel turns, so reading them back is
not a disclosure the runtime has not already made. It is a useful one: the
inline copies are capped by `prompt_content.PER_FILE_CAP`, and this is how the
assistant reaches the rest of a document the prompt truncated.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

#: The operator documents this may read, by exact name. A closed list rather
#: than "every `.md` at the top level", because the answer to "may the
#: assistant read this" is a decision per document and a new file appearing in
#: the tree is not that decision being made.
_DOCUMENTS: frozenset[str] = frozenset({
    "SOUL.md", "USER.md", "OPERATING.md", "WORKSHOP.md", "DIARY.md", "CHANNEL.md",
})

#: The one subtree that is readable whole. Every playbook the prompt lists has
#: a `SKILL.md` under it, and a playbook the assistant wrote is the case this
#: exists for.
_SUBTREE = "skills"

#: Refused by name so the reason is on the record rather than implied by
#: absence. `_shipping/` decides what every user receives and is a build input,
#: not something the assistant reads about itself.
_REFUSED = ("_shipping",)


class WorkspaceReadInput(BaseModel):
    path: str = Field(
        description=(
            "Workspace-relative path, as the prompt spells it. "
            "'WORKSHOP.md', 'DIARY.md', or 'skills/<name>/SKILL.md'. A leading "
            "'tesseract/workspace/' or 'workspace/' is accepted and stripped."
        ),
    )
    line_start: int = Field(
        default=1, ge=1, description="1-based starting line (default 1)."
    )
    line_end: int = Field(
        default=0,
        ge=0,
        description="1-based inclusive ending line. 0 = end-of-file (default).",
    )


def _resolve(raw: str, *, root: Path) -> Path:
    """The path this names inside the workspace, or a reason it is refused.

    Every spelling the prompt has ever used is accepted, because the pointer
    and the reader disagreeing about the prefix is the defect this tool exists
    to close and would be a poor thing to reintroduce here.
    """
    cleaned = raw.strip().replace("\\", "/").lstrip("/")
    if not cleaned:
        raise ValueError("path is empty")
    for prefix in ("tesseract/workspace/", "workspace/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if not cleaned:
        raise ValueError("path names the workspace root rather than a file in it")

    # **Canonicalise, THEN authorise, and never the other way round.** Written
    # the other way first, and `skills/../_shipping/OPERATING.md` read the one
    # subtree named as refused: the head of the string was `skills`, so the
    # refusal never looked at where the path actually pointed. Every check
    # below reads `rel`, which is what the filesystem says this resolves to,
    # so there is no spelling of a target that is checked as a different one.
    resolved_root = root.resolve()
    candidate = (resolved_root / cleaned).resolve()
    try:
        rel = candidate.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes the workspace: {raw!r}") from exc
    if not rel or rel == ".":
        raise ValueError("path names the workspace root rather than a file in it")

    if not rel.endswith(".md"):
        raise ValueError(f"workspace_read reads markdown: {raw!r}")

    head = rel.split("/", 1)[0]
    if head in _REFUSED:
        raise ValueError(
            f"{head}/ is not readable: it is what the build ships to every "
            "user, not a document about this install"
        )
    if "/" in rel:
        if head != _SUBTREE:
            raise ValueError(
                f"only {_SUBTREE}/ is readable below the top level, not {head}/"
            )
    elif rel not in _DOCUMENTS:
        raise ValueError(
            f"{rel} is not one of the operator documents "
            f"({', '.join(sorted(_DOCUMENTS))})"
        )
    return candidate


class WorkspaceReadTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "remembering"
    summary: ClassVar[str] = (
        "Read one of your own workspace documents, or a playbook's SKILL.md."
    )
    use_when: ClassVar[str] = (
        "Use when the prompt points at a workspace document and you want it: "
        "WORKSHOP.md before writing a task artifact, DIARY.md when reviewing "
        "your own pattern of behaviour, or a playbook's SKILL.md before "
        "running it. Also use it to read the rest of a document the prompt "
        "shows you only the beginning of."
    )
    not_when: ClassVar[str] = (
        "use `file_read` for anything in the code tree, and `memory_get` for "
        "the memory store. This reads the workspace only, and only markdown."
    )
    depends_on: ClassVar[str] = ""

    def __init__(self, *, workspace_root: Path | None = None) -> None:
        # Held as None by default and resolved per call, never at construction.
        # `TESSERACT_HOME` is honoured at call time everywhere in this runtime,
        # and a root captured here would answer for the wrong install after an
        # update or a test fixture moved it.
        self._workspace_root = workspace_root

    def _root(self) -> Path:
        if self._workspace_root is not None:
            return self._workspace_root
        from tesseract.paths import workspace_dir

        return workspace_dir()

    @property
    def name(self) -> str:
        return "workspace_read"

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkspaceReadInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: WorkspaceReadInput = tool_input  # type: ignore[assignment]
        root = self._root()
        try:
            path = _resolve(inp.path, root=root)
        except ValueError as exc:
            return ToolResult(output=f"workspace_read rejected: {exc}", is_error=True)
        if not path.is_file():
            return ToolResult(
                output=f"workspace_read: not found: {inp.path}", is_error=True
            )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(output=f"workspace_read: read error: {exc}", is_error=True)

        lines = text.splitlines()
        total = len(lines)
        start_idx = max(0, inp.line_start - 1)
        end_idx = total if inp.line_end == 0 else min(total, inp.line_end)
        selected = [] if start_idx >= total else lines[start_idx:end_idx]
        rel_display = path.relative_to(root.resolve()).as_posix()
        header = f"workspace/{rel_display} ({total} lines)"
        if inp.line_start > 1 or (inp.line_end and inp.line_end < total):
            header += f", showing lines {start_idx + 1}-{end_idx}"
        body = [f"{start_idx + i + 1}\t{ln}" for i, ln in enumerate(selected)]
        return ToolResult(
            output=header + "\n" + "\n".join(body),
            metadata={
                "path": rel_display,
                "total_lines": total,
                "line_start": start_idx + 1,
                "line_end": end_idx,
            },
        )


__all__ = ["WorkspaceReadTool", "WorkspaceReadInput"]
