"""Skill usage telemetry — one JSONL line per skill load + outcome.

Phase 4 (capability-growth) 4a. When TARS `file_read`s a
`workspace/skills/<name>/SKILL.md` body, that consultation is logged to
`<TESSERACT_HOME>/logs/skills/usage.jsonl` so the refinement job
(`scheduler/tasks/skill_refinement.py`) can flag skills that keep failing.

Outcome vocabulary (Agent-Skills-agnostic):
- ``ok``          — the skill body read cleanly (the common case).
- ``error``       — TARS was pointed at the skill but the read failed
                    (missing / oversize / unreadable): a genuinely broken skill.
- ``correction``  — reserved: an operator correction attributed to a skill.
                    No cheap production producer is wired yet; the refinement
                    job counts it when present but nothing emits it today.

TESSERACT_HOME is resolved AT CALL TIME (never an import-time constant) so a
test that sets ``TESSERACT_HOME`` before calling never writes to the
production logs tree — the zero-tolerance rule in CLAUDE.md. All writes are
best-effort: telemetry must never break a `file_read`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.paths import TESSERACT_HOME, log_dir

logger = logging.getLogger(__name__)

SkillOutcome = Literal["ok", "error", "correction"]

_USAGE_FILENAME = "usage.jsonl"
# The workspace-relative marker that identifies a skill body read. Matched
# against POSIX-normalized paths so Windows backslashes don't miss.
_SKILLS_MARKER = "workspace/skills/"
_SKILL_FILE = "SKILL.md"


def usage_log_path() -> Path:
    """`<TESSERACT_HOME>/logs/skills/usage.jsonl`, resolved at call time."""
    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else TESSERACT_HOME
    return log_dir("skills") / _USAGE_FILENAME


def skill_name_for_path(path: str | Path) -> str | None:
    """Return the skill name if `path` is an ACTIVE skill body read, else None.

    Active means `workspace/skills/<name>/SKILL.md` — quarantined drafts
    (`workspace/skills/pending/...` / `rejected/...`) are deliberately NOT
    treated as loads: a pending skill is inert until promoted, so consulting
    one shouldn't score its usage.
    """
    posix = Path(path).as_posix()
    marker_idx = posix.rfind(_SKILLS_MARKER)
    if marker_idx == -1:
        return None
    tail = posix[marker_idx + len(_SKILLS_MARKER):]
    parts = tail.split("/")
    # Expect exactly <name>/SKILL.md — reject pending/<name>/SKILL.md (name
    # would be "pending") and deeper nesting.
    if len(parts) != 2 or parts[1] != _SKILL_FILE:
        return None
    name = parts[0]
    if not name or name in ("pending", "rejected"):
        return None
    return name


def log_skill_load(skill: str, session_id: str, outcome: SkillOutcome = "ok") -> None:
    """Append one usage line. Best-effort — never raises past this call."""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "skill": skill,
        "session_id": session_id or "",
        "outcome": outcome,
    }
    try:
        path = usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the caller
        logger.warning("skill_usage: failed to log %s (%s)", skill, outcome, exc_info=True)


def maybe_log_skill_load(path: str | Path, session_id: str, *, is_error: bool) -> None:
    """Log a skill load iff `path` is an active skill body. `file_read` hook.

    `is_error` maps the read result to the outcome: a failed read of a skill
    TARS was pointed at is an ``error`` outcome (broken skill); a clean read
    is ``ok``.
    """
    name = skill_name_for_path(path)
    if name is None:
        return
    log_skill_load(name, session_id, "error" if is_error else "ok")


def attribute_session_corrections(session_id: str) -> int:
    """Mark every skill loaded in ``session_id`` with a ``correction`` outcome.

    The production producer of the ``correction`` outcome: the session-close
    reflection calls this when it saved an operator-correction (a `feedback`
    memory), so skills consulted during a session that ended in a correction
    are down-weighted by the refinement job. Coarse by design — it attributes
    to ALL skills loaded that session, not the one guilty skill; the window +
    threshold smooth the noise. Idempotent: a skill already carrying a
    correction for this session is not re-flagged. Returns the count added.
    """
    if not session_id:
        return 0
    loaded: set[str] = set()
    corrected: set[str] = set()
    for r in read_usage():
        if r.get("session_id") != session_id:
            continue
        skill = r.get("skill")
        if not skill:
            continue
        if r.get("outcome") == "correction":
            corrected.add(skill)
        else:
            loaded.add(skill)
    targets = loaded - corrected
    for skill in sorted(targets):
        log_skill_load(skill, session_id, "correction")
    return len(targets)


def read_usage() -> list[dict[str, Any]]:
    """Return every well-formed usage row (newest last). Missing file → []."""
    path = usage_log_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("skill"):
            rows.append(data)
    return rows
