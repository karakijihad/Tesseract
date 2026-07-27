"""AU-16 S2 — per-source rolling tree.

One markdown file per source slug at
``<TESSERACT_HOME>/memory-store/trees/source/<source-slug>.md``.

Each ``Seal`` artefact produced by ``SealJob`` is appended (newest-first)
as a single section block. The tree stays operator-readable in any
text editor and is the canonical input for the AU-21 + AU-20 surfaces
that ask "what's been happening on this source?".

The on-disk format is intentionally append-cheap (rewrite once,
keep the rest):

    # source: <slug>

    _Tree derived from leaf seals — newest first._

    ## Seal <seal_id> — <iso8601>

    Leaves: <count>
    Importance peak: <n>

    - leaf_aaa: (snippet)
    - leaf_bbb: (snippet)
    - ...

    ## Seal <seal_id> — <iso8601>

    ...

Atomic rewrites via ``<pid>.<6hex>.tmp`` + ``os.replace``.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.leaf_seals import Seal
from tesseract.memory.leaves import _resolve_home

log = logging.getLogger(__name__)


def SOURCE_TREES_ROOT() -> Path:
    return _resolve_home() / "memory-store" / "trees" / "source"


def source_tree_path(source_slug: str) -> Path:
    return SOURCE_TREES_ROOT() / f"{source_slug}.md"


_SECTION_HEADER_RE = re.compile(r"^## Seal (?P<seal_id>seal_[a-f0-9]+) — ", re.M)


def _format_section(seal: Seal) -> str:
    lines: list[str] = []
    lines.append(f"## Seal {seal.seal_id} — {seal.sealed_at.astimezone(timezone.utc).isoformat()}")
    lines.append("")
    lines.append(f"Leaves: {seal.leaf_count}")
    lines.append(f"Title: {seal.summary_title}")
    lines.append("")
    # Body keeps the bulleted leaf list from build_summary so the tree
    # remains scannable. We strip the build_summary's outer h1 to avoid
    # nested headings inside the section.
    body_lines = []
    for line in seal.summary_body.splitlines():
        if line.startswith("# "):
            continue
        body_lines.append(line)
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    lines.extend(body_lines)
    return "\n".join(lines).rstrip() + "\n"


def _format_header(source_slug: str) -> str:
    # AU-16 frontmatter contract — Obsidian's graph view picks up the
    # `source-summary` color group via the leading tag.
    return (
        "---\n"
        f"kind: source-summary\n"
        "state: sealed\n"
        "parent_tree: source\n"
        f"source: {source_slug}\n"
        "tags:\n  - source-summary\n  - sealed\n"
        "---\n\n"
        f"# source: {source_slug}\n"
        "\n_Tree derived from leaf seals — newest first._\n\n"
    )


def write_seal_section(seal: Seal) -> Path:
    """Insert ``seal`` as the newest section of the source tree.

    Idempotent — if a section for the same ``seal_id`` already exists,
    the file is left untouched (returns the path unchanged).
    """
    target = source_tree_path(seal.source_slug)
    target.parent.mkdir(parents=True, exist_ok=True)

    section = _format_section(seal)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if existing:
        # Idempotent skip — drop the work if this exact seal already lives
        # in the file. Cheap regex over the section headers.
        if any(m.group("seal_id") == seal.seal_id for m in _SECTION_HEADER_RE.finditer(existing)):
            return target
        # Split the existing file at the first `## Seal ` so we can prepend
        # the new section while preserving the header banner.
        split = existing.split("\n## Seal ", 1)
        header = split[0].rstrip() + "\n\n"
        rest = "## Seal " + split[1] if len(split) == 2 else ""
        new_body = header + section + "\n" + rest if rest else header + section
    else:
        new_body = _format_header(seal.source_slug) + section

    tmp = target.with_name(f"{target.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(new_body, encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_source_tree(source_slug: str) -> str | None:
    path = source_tree_path(source_slug)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_source_tree_paths() -> list[Path]:
    root = SOURCE_TREES_ROOT()
    if not root.exists():
        return []
    return sorted(root.glob("*.md"))


__all__ = [
    "SOURCE_TREES_ROOT",
    "list_source_tree_paths",
    "read_source_tree",
    "source_tree_path",
    "write_seal_section",
]
