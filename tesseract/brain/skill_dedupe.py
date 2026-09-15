"""Whether a proposed skill's steps are already covered by an existing one.

`skill_door.py` re-exports these. Every origin is checked against the same
rule, and for a task accepted done a match is a second success supporting a
draft's steps rather than a duplicate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tesseract.brain.skills import SKILL_PENDING_DIRNAME, load_skill_folder, load_skills


def collapsed_tools(steps: Any) -> list[str]:
    """Ordered tool names off a step list, with an immediate repeat
    collapsed. Works over either `skill_door.DraftStep` or the loader's
    `Step`: both carry a `.tool`."""
    out: list[str] = []
    for step in steps:
        tool = getattr(step, "tool", "") or ""
        if tool and not (out and out[-1] == tool):
            out.append(tool)
    return out


def scan_sequences(skills_dir: Path) -> list[tuple[Path, str, list[str]]]:
    """Every active and pending skill, with its collapsed tool sequence."""
    found: list[tuple[Path, str, list[str]]] = []
    for entry in load_skills(skills_dir):
        found.append((skills_dir / entry.dirname, entry.name, collapsed_tools(entry.steps)))
    pending_dir = skills_dir / SKILL_PENDING_DIRNAME
    if pending_dir.is_dir():
        for folder in sorted(p for p in pending_dir.iterdir() if p.is_dir()):
            entry = load_skill_folder(folder)
            if entry is not None:
                found.append((folder, entry.name, collapsed_tools(entry.steps)))
    return found


def matching_sequence(
    existing: list[tuple[Path, str, list[str]]], sequence: list[str]
) -> tuple[Path, str] | None:
    """The first existing skill whose own tool sequence equals `sequence`
    exactly. An empty sequence matches nothing — a step with no tool at all
    says nothing about repetition."""
    for folder, name, tools in existing:
        if tools and tools == sequence:
            return folder, name
    return None
