"""Markdown skills loader — TARS's prose self-extension mechanism ("workshop").

Scans `tesseract/workspace/skills/<name>/SKILL.md` for YAML frontmatter
(`name`, `description`) + a markdown instruction body. `brain/prompt.py::
_build_skills_block` exposes only name/description/path into the prompt
manifest section — TARS `file_read`s the SKILL.md body on demand.

Skill folders MAY also carry a `scripts/` subdirectory (P6 Task 4b
"workshop" bundling). This loader never reads it: bundled scripts run
through the existing bash/subprocess ASK path, not through this loader —
no new execution surface, no registry writes.

Malformed or unreadable skill folders are SKIPPED, never a boot/prompt-build
failure. Each skip logs at ERROR level so it reaches the Mirror pulse feed
(`mirror/server/log_forwarder.py::MirrorLogHandler` forwards ERROR+ only —
same idiom as the permissions-drift logging in `brain/boot.py`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

SKILL_FILENAME = "SKILL.md"

# A hostile/corrupt SKILL.md must not be read into memory whole — cap at
# 256 KiB (PTY_LINE_CAP idiom: a code-level bound, not a config key).
SKILL_MD_MAX_BYTES = 262_144

# Phase 4 quarantine — mirrors agents/loader.py's pending/rejected split.
# A skill drafted unattended lands in `skills/pending/<name>/SKILL.md` and is
# NEVER surfaced live until the operator promotes it; a rejected draft is
# archived in `skills/rejected/<name>/`. Both dirnames (and __pycache__) are
# skipped by the live scan so a quarantined draft can't rejoin the active set.
SKILL_PENDING_DIRNAME = "pending"
SKILL_REJECTED_DIRNAME = "rejected"
_SKIP_DIRNAMES = frozenset({SKILL_PENDING_DIRNAME, SKILL_REJECTED_DIRNAME, "__pycache__"})


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    dirname: str
    # Agent Skills standard — optional frontmatter carried through for
    # interop with the peer ecosystems (docs.anthropic.com/en/docs/claude-code/
    # skills). name/description stay required; these are best-effort.
    version: str = ""
    license: str = ""
    allowed_tools: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def _load_skill(folder: Path) -> SkillEntry | None:
    """Parse one skill folder's SKILL.md. Returns None (logged) on any
    malformation; never raises — one bad skill must not break the rest."""
    skill_md = folder / SKILL_FILENAME
    if not skill_md.exists():
        return None  # not a skill folder — nothing to warn about

    try:
        size = skill_md.stat().st_size
    except OSError as exc:
        logger.error("skills: %s unreadable (%s) — skipping", skill_md, exc)
        return None
    if size > SKILL_MD_MAX_BYTES:
        logger.error(
            "skills: %s exceeds %d bytes (%d) — skipping",
            skill_md, SKILL_MD_MAX_BYTES, size,
        )
        return None

    try:
        raw = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8
        # SKILL.md (plausible on Windows) must skip+pulse-warn like any
        # other malformed skill, never raise past load_skills.
        logger.error("skills: %s unreadable (%s) — skipping", skill_md, exc)
        return None

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        logger.error("skills: %s missing YAML frontmatter — skipping", skill_md)
        return None

    try:
        fm: Any = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.error("skills: %s frontmatter YAML invalid (%s) — skipping", skill_md, exc)
        return None

    if not isinstance(fm, dict):
        logger.error("skills: %s frontmatter must be a mapping — skipping", skill_md)
        return None

    name = str(fm.get("name") or "").strip()
    description = str(fm.get("description") or "").strip()
    if not name or not description:
        logger.error(
            "skills: %s frontmatter missing required name/description — skipping",
            skill_md,
        )
        return None

    return SkillEntry(
        name=name,
        description=description,
        dirname=folder.name,
        version=str(fm.get("version") or "").strip(),
        license=str(fm.get("license") or "").strip(),
        allowed_tools=_parse_allowed_tools(fm.get("allowed-tools")),
        metadata=fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {},
    )


def _parse_allowed_tools(raw: Any) -> tuple[str, ...]:
    """Normalize the Agent Skills `allowed-tools` frontmatter to a tuple.

    The standard accepts either a YAML list or a space/comma-separated string
    (docs show `Read Grep`); both collapse to an ordered tuple of tool names.
    """
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(t for t in re.split(r"[,\s]+", raw.strip()) if t)
    if isinstance(raw, (list, tuple)):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    return ()


def load_skill_folder(folder: Path) -> SkillEntry | None:
    """Parse a single `<folder>/SKILL.md`. Public wrapper over the loader used
    by the promote/create tools to validate a draft parses before moving it.
    Returns None (logged) on any malformation; never raises."""
    return _load_skill(folder)


def load_skills(skills_dir: Path) -> list[SkillEntry]:
    """Return every well-formed skill under `skills_dir`, folder-name sorted.

    Missing `skills_dir` returns []. An unreadable `skills_dir` itself
    (permissions, race) also returns [] — never raises past this function.
    """
    if not skills_dir.exists():
        return []
    try:
        folders = sorted(
            (
                p for p in skills_dir.iterdir()
                if p.is_dir() and p.name not in _SKIP_DIRNAMES
            ),
            key=lambda p: p.name,
        )
    except OSError as exc:
        logger.error("skills: %s unreadable (%s) — skipping skills section", skills_dir, exc)
        return []

    entries: list[SkillEntry] = []
    for folder in folders:
        entry = _load_skill(folder)
        if entry is not None:
            entries.append(entry)
    return entries


def list_skills_names(skills_dir: Path) -> list[str]:
    """Names of active (well-formed, non-quarantined) skills."""
    return [s.name for s in load_skills(skills_dir)]


def _list_quarantine_skills(skills_dir: Path, dirname: str) -> list[str]:
    """Return names of skills under `skills_dir/<dirname>/` — each a
    `<name>/SKILL.md` folder. Missing/unreadable → []."""
    quarantine = skills_dir / dirname
    if not quarantine.exists():
        return []
    try:
        return sorted(
            p.name for p in quarantine.iterdir()
            if p.is_dir() and (p / SKILL_FILENAME).exists()
        )
    except OSError:
        return []


def list_pending_skills(skills_dir: Path) -> list[str]:
    """Names of quarantined skills awaiting operator promotion."""
    return _list_quarantine_skills(skills_dir, SKILL_PENDING_DIRNAME)


def list_rejected_skills(skills_dir: Path) -> list[str]:
    """Names of operator-rejected skills archived in `rejected/`."""
    return _list_quarantine_skills(skills_dir, SKILL_REJECTED_DIRNAME)


def read_rejection_reason(skills_dir: Path, name: str) -> str:
    """Operator's reason from the reject sidecar, empty string if absent.
    Written next to `rejected/<name>/` as `rejected/<name>.reason.txt`."""
    reason_path = skills_dir / SKILL_REJECTED_DIRNAME / f"{name}.reason.txt"
    try:
        return reason_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
