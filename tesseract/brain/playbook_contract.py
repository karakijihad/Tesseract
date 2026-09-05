"""What a playbook must declare before the assistant will reach for it.

The same move `check_tool_contract` makes for tools and `agents/contract.py`
makes for cards: a thing the runtime runs on its own declares what it is, and
the declaration is checked at boot rather than discovered when it fires.

A playbook is a skill that declares the procedure contract in its frontmatter
(`brain/skills.py::PLAYBOOK_KEYS`). Declaring any one of those keys is
declaring all of them, so a half-written playbook is a list of gaps and never
a skill that quietly loads as prose.

**Reported, never refused, and the reason is where skills live.** Agent cards
have two roots, the app's and the operator's, and a shipped card that cannot
run stops the boot. Skills have one root, `workspace_dir()/skills`, and it is
the operator's in every install: nothing ships a playbook. So every gap here is
reported the way an operator's card is, a blocking one at ERROR so it reaches
the pulse feed (the idiom the skill loader already uses for a malformed skill)
and every one in the prompt's skills block, so the model is told which playbook
it may not reach for and why.

**Blocking is "can it run", as for cards.** A step naming a tool the registry
does not hold, a step using a tool the playbook forbids or leaves outside its
own envelope, a status outside the vocabulary, a version that cannot be
ordered. A missing prose field means nothing can say what the playbook is for,
which is reported and not blocking. Postures are NOT checked: `permissions.yaml`
decides at the call, exactly as it does for a fresh one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from tesseract.brain.skills import (
    CONTRACT_KEYS,
    PLAYBOOK_STATUSES,
    SkillEntry,
    load_skills,
)

logger = logging.getLogger(__name__)

#: Contract keys that may be declared empty and still be honest: no
#: preconditions, no forbidden tools, a hand-written playbook with no turn to
#: cite yet, nothing measured. Every other key must carry something.
_MAY_BE_EMPTY = frozenset({
    "not_when", "preconditions", "forbidden-tools", "failure_modes",
    "evidence", "confidence",
})


@dataclass(frozen=True)
class PlaybookGap:
    """One playbook and what it fails to declare.

    `blocking` separates the two questions: True means the playbook cannot
    run, False means nothing can say what it is for. The skills block prints
    both; the log level differs.
    """

    name: str
    field: str
    detail: str
    blocking: bool


def gaps_for_skill(
    entry: SkillEntry,
    *,
    tool_names: frozenset[str] | None = None,
) -> list[PlaybookGap]:
    """Every gap in one ALREADY-LOADED skill. Empty for a plain skill.

    `tool_names` is the live registry, or None for "no answer": the prompt
    builder has no registry in hand, and a step tool it cannot check is not a
    step tool that is missing.
    """
    if not entry.is_playbook:
        return []
    gaps: list[PlaybookGap] = []

    for key in sorted(CONTRACT_KEYS):
        if key not in entry.declared:
            gaps.append(_gap(entry, key, "is not declared", blocking=False))
        elif key not in _MAY_BE_EMPTY and _is_empty(entry, key):
            gaps.append(_gap(entry, key, "is declared and empty", blocking=False))

    if "status" in entry.declared and entry.status not in PLAYBOOK_STATUSES:
        gaps.append(_gap(
            entry, "status",
            f"is {entry.status!r}; one of {', '.join(PLAYBOOK_STATUSES)}",
            blocking=True,
        ))
    if "version" in entry.declared and version_number(entry.version) is None:
        gaps.append(_gap(
            entry, "version",
            f"is {entry.version!r}, which cannot be ordered against the "
            "revision before it. A whole number, counting up",
            blocking=True,
        ))
    if "steps" in entry.declared and not entry.steps:
        gaps.append(_gap(entry, "steps", "names nothing to do", blocking=True))

    allowed = set(entry.allowed_tools)
    forbidden = set(entry.forbidden_tools)
    both = sorted(allowed & forbidden)
    if both:
        gaps.append(_gap(
            entry, "forbidden-tools",
            f"names {', '.join(both)}, which allowed-tools also names",
            blocking=True,
        ))
    for index, step in enumerate(entry.steps, start=1):
        if not step.tool:
            continue
        where = f"steps[{index}]"
        if step.tool in forbidden:
            gaps.append(_gap(
                entry, where,
                f"uses {step.tool!r}, which this playbook forbids",
                blocking=True,
            ))
        elif step.tool not in allowed:
            gaps.append(_gap(
                entry, where,
                f"uses {step.tool!r}, which allowed-tools does not name",
                blocking=True,
            ))
        elif tool_names is not None and step.tool not in tool_names:
            gaps.append(_gap(
                entry, where,
                f"uses {step.tool!r}, which is not a tool the runtime has",
                blocking=True,
            ))
    return gaps


def version_number(raw: str) -> int | None:
    """A playbook version as something that can be ordered, or None."""
    text = (raw or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def inspect_playbooks(
    skills_dir: Path,
    *,
    tool_names: frozenset[str] | None = None,
) -> list[PlaybookGap]:
    """Every gap in every playbook under `skills_dir`. Never raises: a
    skill that will not parse is the loader's to report, and it does."""
    gaps: list[PlaybookGap] = []
    for entry in load_skills(skills_dir):
        gaps.extend(gaps_for_skill(entry, tool_names=tool_names))
    return gaps


def blocking_gaps(
    entries: list[SkillEntry],
    *,
    tool_names: frozenset[str] | None = None,
) -> dict[str, PlaybookGap]:
    """Per playbook name, the gap that stops it running, for a surface to show.

    One gap per name, the first blocking one: a playbook that cannot run for
    two reasons is still a playbook that cannot run, and a line shows a
    sentence rather than a list. Takes loaded entries because the one caller
    that renders this has them in hand already and parses each file once.
    """
    found: dict[str, PlaybookGap] = {}
    for entry in entries:
        for gap in gaps_for_skill(entry, tool_names=tool_names):
            if gap.blocking and gap.name not in found:
                found[gap.name] = gap
    return found


def check_playbooks(
    skills_dir: Path,
    *,
    tool_names: frozenset[str] | None = None,
) -> list[PlaybookGap]:
    """Log every gap, blocking ones where the operator will see them.

    Called once from `build_tool_registry`, beside the tool and card contracts,
    because that is the assembly point every entry into the runtime goes
    through. Returns what it logged so a caller can record it.
    """
    gaps = inspect_playbooks(skills_dir, tool_names=tool_names)
    for gap in gaps:
        if gap.blocking:
            logger.error(
                "playbook %s cannot run: %s %s", gap.name, gap.field, gap.detail
            )
        else:
            logger.info("playbook %s: %s %s", gap.name, gap.field, gap.detail)
    return gaps


def _gap(entry: SkillEntry, field: str, detail: str, *, blocking: bool) -> PlaybookGap:
    return PlaybookGap(name=entry.name, field=field, detail=detail, blocking=blocking)


def _is_empty(entry: SkillEntry, key: str) -> bool:
    value = getattr(entry, key.replace("-", "_"))
    return value is None or value == "" or value == ()


__all__ = [
    "PlaybookGap",
    "blocking_gaps",
    "check_playbooks",
    "gaps_for_skill",
    "inspect_playbooks",
    "version_number",
]
