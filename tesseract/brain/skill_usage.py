"""Skill usage telemetry — one JSONL line per skill load + outcome.

When the assistant `file_read`s a `workspace/skills/<name>/SKILL.md` body, that
consultation is logged to `<TESSERACT_HOME>/logs/skills/usage.jsonl` so the
refinement job (`scheduler/tasks/skill_refinement.py`) can flag skills that keep
failing. The volume of this file is also what fires that job — its row waits on
`skill_usage_volume` rather than on an hour.

Outcome vocabulary (Agent-Skills-agnostic):
- ``ok``          — the skill body read cleanly (the common case).
- ``error``       — the assistant was pointed at the skill but the read failed
                    (missing / oversize / unreadable): a genuinely broken skill.
- ``correction``  — an operator correction attributed to a skill. Produced by
                    `attribute_session_corrections`, called when a `feedback`
                    memory is saved: by `memory_save` on the turn it happens,
                    and by the session-close reflection (`brain/session_ops.py`)
                    when the reflection itself saved one. A correction row for
                    a PLAYBOOK also carries the revision that was read, the
                    turn that read it, and the furthest step that turn reached
                    after reading it, so a correction lands on a step and a
                    version rather than on every skill loaded that session.

TESSERACT_HOME is resolved AT CALL TIME (never an import-time constant) so a
test that sets ``TESSERACT_HOME`` before calling never writes to the
production logs tree, which is a zero-tolerance rule. All writes are
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


def log_skill_load(
    skill: str,
    session_id: str,
    outcome: SkillOutcome = "ok",
    *,
    version: str = "",
    turn_id: str = "",
    step: int | None = None,
) -> None:
    """Append one usage line. Best-effort — never raises past this call.

    `version` is the revision that was read, for a playbook. It is what lets
    reuse be measured against the version rather than the name, so a revision
    that performs worse than the one it replaced can be told apart from it.
    Empty for a plain skill, which has nothing to compare.
    """
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "skill": skill,
        "session_id": session_id or "",
        "outcome": outcome,
    }
    if version:
        row["version"] = version
    if turn_id:
        row["turn_id"] = turn_id
    if step is not None:
        row["step"] = step
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
    """The revision the body read carries, or "" when it is not a playbook
    or cannot be parsed. Read off the file rather than a registry, because the
    file is what the assistant just read."""
    from tesseract.brain.skills import load_skill_folder

    try:
        entry = load_skill_folder(Path(path).parent)
    except Exception:  # noqa: BLE001 — telemetry must never break the caller
        return ""
    return entry.version if entry is not None and entry.is_playbook else ""


def attribute_session_corrections(session_id: str) -> int:
    """Mark every skill loaded in ``session_id`` with a ``correction`` outcome.

    Called when a `feedback` memory is saved in the session. A plain skill is
    marked by name, which is coarse by design and smoothed by the refinement
    job's window and threshold. A playbook is marked on the REVISION that was
    read, the TURN that read it, and the furthest STEP that turn reached after
    the read, which is what lets a correction be laid against one step of one
    version rather than against everything consulted that session. Idempotent:
    a skill already carrying a correction for this session is not re-flagged.
    Returns the count added.
    """
    if not session_id:
        return 0
    loaded: dict[str, dict[str, Any]] = {}
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
            # The most recent load wins: the correction lands on the revision
            # that was read last, and on the turn that read it.
            loaded[skill] = r
    targets = {skill: loaded[skill] for skill in loaded if skill not in corrected}
    added = 0
    for skill in sorted(targets):
        row = targets[skill]
        version = str(row.get("version") or "")
        turn_id, step = ("", None)
        if version:
            turn_id, step, total, matched = step_reached(skill, session_id, row.get("ts"), version)
            # No closed turn holds the read yet: the read was in the turn
            # that is still running. Writing a row now would carry no turn
            # and no step, and would mark the skill as corrected so the
            # session-close pass could not write the joined one. Leave it.
            if step is None:
                continue
            # Read is not followed. A task whose subject is the playbooks
            # themselves reads every one of them, and a correction on that
            # turn belongs to the procedure the turn was carrying out, not to
            # the files it opened. Measured 2026-09-03 on exactly that task:
            # the playbook being followed reached 8 of its 8 steps and the
            # four it read as content reached 4, 4, 2 and 0 of 8. More than
            # half is the line, and a playbook with no tool steps has nothing
            # to be judged by and is marked as read.
            if step is not None and total and matched * 2 <= total:
                continue
        log_skill_load(
            skill, session_id, "correction", version=version, turn_id=turn_id, step=step
        )
        added += 1
    return added


def step_reached(
    skill: str, session_id: str, loaded_at: Any, version: str = ""
) -> tuple[str, int | None, int, int]:
    """The turn that read the playbook, the furthest step it then reached,
    how many tool steps the playbook has, and how many of those were run.

    The turn is the closed record of `session_id` whose span holds the load;
    the step is the highest playbook step whose tool the turn ran AFTER the
    read, matched in order, because a turn that read the steps and then ran
    `web_search` and `channel_send` was on step three when whatever went
    wrong went wrong. `(turn_id, 0, n, 0)` is a turn that read the playbook
    and followed none of it; `("", None, n, 0)` is a load no closed turn
    accounts for.
    """
    from tesseract.brain.playbook_reuse import turn_record_around
    from tesseract.brain.skills import load_skill_folder
    from tesseract.orchestrator.turns.manifest import read_step_name
    from tesseract.paths import workspace_dir

    folder = workspace_dir() / "skills" / skill
    entry = load_skill_folder(folder)
    # The steps of the revision that was READ, which is not the live file
    # once a refinement has landed in between: the kept copy under
    # history/<version>/ is the one the correction is about.
    if entry is not None and version and entry.version != version:
        from tesseract.brain.skills import SKILL_HISTORY_DIRNAME

        kept = load_skill_folder(folder / SKILL_HISTORY_DIRNAME / version)
        if kept is not None:
            entry = kept
    tools = [s.tool for s in entry.steps] if entry is not None else []
    total = sum(1 for tool in tools if tool)
    when = _moment(loaded_at)
    if when is None:
        return "", None, total, 0
    record = turn_record_around(session_id, when)
    if record is None:
        return "", None, total, 0
    ran: list[str] = []
    for row in record.get("stages") or []:
        kind, name = read_step_name(str(row.get("stage") or ""))
        started = _moment(row.get("started_at"))
        if kind != "tool" or started is None or started <= when:
            continue
        ran.append(name)
    # Two numbers: the frontmatter index of the furthest step reached, which
    # is what the row reports, and how many TOOL steps were matched, which is
    # what the more-than-half rule compares against `total`. A prose step
    # between two tool steps would otherwise make one matched call read as
    # step three of two.
    reached, matched, cursor = 0, 0, 0
    for index, tool in enumerate(tools, start=1):
        if not tool:
            continue
        try:
            cursor = ran.index(tool, cursor) + 1
        except ValueError:
            continue
        reached = index
        matched += 1
    return str(record.get("run_id") or ""), reached, total, matched


def _moment(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


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
