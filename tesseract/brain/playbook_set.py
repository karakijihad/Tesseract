"""Which playbooks ride every turn, and which are one search away.

The same dial `working_set.yaml` is for tools, pointed at the other thing a
turn carries before anyone has typed anything. A playbook in the carried set
arrives with its description, its version and status, and its `use_when`, so
the assistant reaches for it directly; one outside it appears on the map as a name and a line, and
`playbook_search` fetches the rest. Off the list is slower to reach, never
unavailable.

**This file never ships and never leaves the machine.** Playbooks are written
here, out of this operator's own work, and `tesseract/workspace/` is private
per machine — so the carried list lives beside them rather than in
`working_set.yaml`, which is tracked and ships verbatim. It is the same split
the tools already have: the shipped roster in config, the machine's own
promotions in a home-side file (`home_tools.promoted_path`).

Two halves, and only one of them is a person's:

- **The choice** — which names are in the file. That is the operator's.
- **Everything else** — the annotation beside each name and the list of what
  is not carried. Those are facts about the playbooks, read off them every
  time this file is written, so the file can never become a second opinion
  about what a playbook does.

An unknown name is kept and marked rather than dropped, and never raises: the
names here are folders the operator may delete at any moment, and a carried
playbook they removed must not stop the app from starting.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tesseract.brain.skills import SkillEntry, load_skills
from tesseract.paths import workspace_dir

logger = logging.getLogger(__name__)

CARRIED_FILENAME = "carried.txt"

_BANNER = """\
# Which playbooks the assistant sees without having to look them up.
#
# Every playbook you have is always readable. This file decides only which
# ones arrive on every turn with when to use them, so the assistant reaches
# for them directly. The rest appear as a name and a line. Either way the
# trigger, what must hold first and the steps come from `playbook_search`,
# which is one call. Off this list is one step slower to reach, never
# unavailable.
#
# So this is a spending dial, the same one `working_set.yaml` is for tools.
# Every carried playbook costs its description and when to use it on every
# turn, whether or not the turn has anything to do with it.
#
# THE NAMES ARE YOURS. The descriptions beside them are not: they are read off
# the playbooks themselves every time this file is written, so they cannot
# drift from what a playbook says. Regenerate after a hand-edit with
#   python -m tesseract.scripts.generate_playbook_set --write
#
# A name no playbook answers to is kept and marked, never deleted quietly.
#
# This file stays on this machine. Your playbooks are yours and none of them
# ships.
"""

_UNKNOWN_HEADING = "# NO PLAYBOOK ANSWERS TO THESE NAMES"
_REST_HEADING = (
    "# Not carried, and one `playbook_search` away. Delete the leading `# `\n"
    "# from a line to carry it every turn."
)


def skills_dir() -> Path:
    """`<workspace>/skills`, resolved at call time.

    Named, because callers were reaching it as `carried_path().parent`, which
    is correct only while the carried list sits directly in that directory.
    A coupling to one filename's position is not a way to find a directory.
    """
    return workspace_dir() / "skills"


def carried_path() -> Path:
    """`<workspace>/skills/carried.txt`, resolved at call time so a
    `TESSERACT_HOME` change after import is honored."""
    return skills_dir() / CARRIED_FILENAME


def load_carried_names(path: Path | None = None) -> frozenset[str]:
    """The names in the file. Never raises.

    Unlike `working_set.yaml::core`, an unreadable file here is a warning and
    an empty set rather than a refusal to start. That file is ours; this one
    holds folder names the operator owns, and a deleted playbook must not be
    able to take the app down with it.

    An empty set is a legitimate answer, not a fallback: it means every
    playbook is a search away, which is the dial turned all the way down.
    """
    target = path or carried_path()
    try:
        if not target.is_file():
            return frozenset()
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("playbook set: %s unreadable: %s", target, exc)
        return frozenset()
    names = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = stripped.split("#", 1)[0].strip()
        if name:
            names.add(name)
    return frozenset(names)


def render(chosen: list[str], entries: list[SkillEntry]) -> str:
    """The whole file, deterministic for a given (choice, playbook set).

    A chosen name with nothing on disk answering to it is kept and marked.
    Dropping it would turn a typo into "that playbook stopped being carried
    and nobody said so".
    """
    facts = {e.name: e.description for e in entries if e.is_playbook}
    picked = set(chosen)
    lines: list[str] = [_BANNER]

    for name in sorted(picked & set(facts)):
        lines.append(f"{name}{_annotation(name, facts)}")

    unknown = sorted(picked - set(facts))
    if unknown:
        lines += ["", _UNKNOWN_HEADING]
        lines += list(unknown)

    rest = sorted(set(facts) - picked)
    if rest:
        lines += ["", _REST_HEADING]
        for name in rest:
            lines.append(f"# {name}{_annotation(name, facts)}")

    return "\n".join(lines) + "\n"


def _annotation(name: str, facts: dict[str, str]) -> str:
    description = facts.get(name, "")
    return f"  # {description}" if description else ""


def write_carried(chosen: list[str], path: Path | None = None) -> None:
    """Replace the file with the rendered choice. Reads the live playbooks for
    the annotations, so a save from the panel rebuilds them too."""
    target = path or carried_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = load_skills(target.parent)
    target.write_text(render(chosen, entries), encoding="utf-8")


__all__ = [
    "CARRIED_FILENAME",
    "carried_path",
    "skills_dir",
    "load_carried_names",
    "render",
    "write_carried",
]
