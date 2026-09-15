"""Skill usage telemetry — one JSONL line per skill load + outcome.

When the assistant `file_read`s a `workspace/skills/<name>/SKILL.md` body, that
consultation is logged to `<TESSERACT_HOME>/logs/skills/usage.jsonl`.
`brain/playbook_reuse.py` reads it to measure a revision, and
`skill_refine`'s `report` action writes the one row a rewrite is ever built
from.

Outcome vocabulary (Agent-Skills-agnostic):
- ``ok``          — the skill body read cleanly (the common case).
- ``error``       — the assistant was pointed at the skill but the read failed
                    (missing / oversize / unreadable): a genuinely broken skill.
- ``correction``  — the agent that followed a skill reports it went wrong,
                    through `skill_refine` action `report`. It writes the
                    revision that was read, the turn that read it and the
                    step that failed, when the agent knows them, so a
                    correction lands on a step and a version rather than on
                    every skill loaded that session.

TESSERACT_HOME is resolved AT CALL TIME (never an import-time constant) so a
test that sets ``TESSERACT_HOME`` before calling never writes to the
production logs tree, which is a zero-tolerance rule. All writes are
best-effort: telemetry must never break a `file_read`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.paths import log_dir

logger = logging.getLogger(__name__)

SkillOutcome = Literal["ok", "error", "correction"]

_USAGE_FILENAME = "usage.jsonl"
# The workspace-relative marker that identifies a skill body read. Matched
# against POSIX-normalized paths so Windows backslashes don't miss.
_SKILLS_MARKER = "workspace/skills/"
_SKILL_FILE = "SKILL.md"


def usage_log_path() -> Path:
    """`<TESSERACT_HOME>/logs/skills/usage.jsonl`, resolved at call time.

    `log_dir` reaches `home_dir()` on every call and reads the environment
    itself, so resolving it a second time here was dead and read as though
    the override were applied in this function.
    """
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


def log_skill_load(
    skill: str,
    session_id: str,
    outcome: SkillOutcome = "ok",
    *,
    version: str = "",
) -> None:
    """Append one LOAD row: the skill was read, and whether the read worked.

    `version` is the revision that was read, for a playbook. It is what lets
    reuse be measured against the version rather than the name, so a revision
    that performs worse than the one it replaced can be told apart from it.
    Empty for a plain skill, which has nothing to compare, and empty for an
    `error` by construction, since `_version_at` reads the version off the
    file whose read just failed.
    """
    _append(skill, session_id, outcome, {"version": version} if version else {})


def log_correction(
    skill: str,
    session_id: str,
    *,
    version: str = "",
    turn_id: str = "",
    step: int | None = None,
) -> None:
    """Append one CORRECTION row: the agent that followed this skill reports
    it went wrong.

    **Its own function because it is its own row shape.** These three fields
    only ever mean something together with `outcome="correction"`, and while
    they hung off `log_skill_load` the signature described neither row: a
    caller could write an `ok` with a step, or a correction with no turn, and
    nothing refused either. Two shapes, two functions, and each one's
    arguments are now the ones it actually has.

    Written by `skill_refine`'s `report` action alone: the agent's report
    leads and this is the record of it, not a mined inference.
    """
    extra: dict[str, Any] = {}
    if version:
        extra["version"] = version
    if turn_id:
        extra["turn_id"] = turn_id
    # `is not None`, never a truth test: 0 is a real answer here. It is the
    # turn that read the playbook and then followed none of it, which is the
    # most interesting row on the log, and `if step:` drops exactly that one.
    if step is not None:
        extra["step"] = step
    _append(skill, session_id, "correction", extra)


def _append(
    skill: str, session_id: str, outcome: SkillOutcome, extra: dict[str, Any]
) -> None:
    """One line on the log. Best-effort — never raises past this call.

    A field the caller left out is absent from the row rather than written as
    a blank: every reader of this file tells "no version" from "version is
    empty" by whether the key is there at all.
    """
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "skill": skill,
        "session_id": session_id or "",
        "outcome": outcome,
    }
    row.update(extra)
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
    The assistant was pointed at is an ``error`` outcome (broken skill); a clean read
    is ``ok``.
    """
    name = skill_name_for_path(path)
    if name is None:
        return
    log_skill_load(name, session_id, "error" if is_error else "ok", version=_version_at(path))


def _version_at(path: str | Path) -> str:
    """The revision the body read carries, or "" when the skill never
    declared one or the file cannot be parsed. Read off the file rather than
    a registry, because the file is what the assistant just read."""
    from tesseract.brain.skills import load_skill_folder

    try:
        entry = load_skill_folder(Path(path).parent)
    except Exception:  # noqa: BLE001 — telemetry must never break the caller
        return ""
    return entry.version if entry is not None else ""


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
