"""Render a skill draft to SKILL.md, and the door it must pass to be written.

The pure half of the door (render, validate, refuse), with no I/O beyond the
throwaway temp folders both checks use to round-trip through the real loader.
It lives in `brain/` beside `skills.py` because `replace_skill_body` runs the
same door on a rewrite, and a tool module is not something `brain` imports.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

from tesseract.brain.skills import _FRONTMATTER_RE, SKILL_FILENAME, load_skill_folder


def render_skill_markdown(draft: Any) -> str:
    """Render the full SKILL.md content from a `skill_door.SkillDraft`.
    Frontmatter aligned to the Agent Skills standard (name/description
    required) plus the procedure contract, written whole: every
    contract key is written, empty where the author gave nothing, so the
    contract reports "declared and empty" rather than "missing" and the file
    reads as a whole declaration. Pure function.
    """
    fm: dict[str, Any] = {"name": draft.name, "description": draft.description}
    # A skill's version is an ordering key, so it is the runtime's and not
    # the author's: `keep_predecessor` archives under it and refuses anything
    # that does not sort above the live one. This function only ever renders
    # a CREATED skill, and a created skill is the first revision.
    fm["version"] = "1"
    fm["status"] = "active"
    if draft.license:
        fm["license"] = draft.license
    fm["allowed-tools"] = list(draft.allowed_tools)
    fm["trigger"] = draft.trigger
    fm["use_when"] = draft.use_when
    fm["not_when"] = draft.not_when
    fm["preconditions"] = list(draft.preconditions)
    # `steps` is the one contract key whose empty declaration BLOCKS the
    # write outright (`playbook_contract.gaps_for_skill`: "names nothing to
    # do"), so, unlike every other field above and below, it is written only
    # when the draft actually carries steps. A draft with none is a skill
    # with nothing to run yet, which is honestly "not declared", not
    # "declared and refused".
    if draft.steps:
        fm["steps"] = [
            {"do": step.do, **({"tool": step.tool} if step.tool else {})}
            for step in draft.steps
        ]
    fm["forbidden-tools"] = list(draft.forbidden_tools)
    fm["expected_result"] = draft.expected_result
    fm["failure_modes"] = list(draft.failure_modes)
    fm["evidence"] = list(draft.evidence)
    fm["confidence"] = draft.confidence
    front = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{front}\n---\n\n{draft.instructions.strip()}\n"


def refuse_playbook(rendered: str, name: str, tool_names: frozenset[str] | None) -> str | None:
    """Why a rendered SKILL.md may not be written, or None. Runs on every
    skill: the blocking-gap check and the credential/path scan
    below apply whether or not the file declared the contract."""
    from tesseract.brain.playbook_contract import gaps_for_skill
    from tesseract.kernel.tools._path_door import refuse_paths
    from tesseract.paths import home_dir

    tmp_root = Path(tempfile.mkdtemp())
    folder = tmp_root / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / SKILL_FILENAME).write_text(rendered, encoding="utf-8")
        entry = load_skill_folder(folder)
    finally:
        try:
            (folder / SKILL_FILENAME).unlink(missing_ok=True)
            folder.rmdir()
            tmp_root.rmdir()
        except OSError:
            pass
    if entry is None:
        return None

    blocking = [g for g in gaps_for_skill(entry, tool_names=tool_names) if g.blocking]
    if blocking:
        return "Refused, this playbook cannot run: " + "; ".join(
            f"{g.field} {g.detail}" for g in blocking
        )

    # Every field a person or a model wrote, the body included: the body is
    # what the assistant reads and follows, so a path there is the one that
    # matters most, and the description is what every prompt shows. The
    # judgment is `_path_door`'s, one module that knows every path shape and
    # judges by the string rather than by the host.
    texts = [entry.description, entry.trigger, entry.use_when, entry.not_when, entry.expected_result]
    texts += list(entry.preconditions) + list(entry.failure_modes)
    texts += [step.do for step in entry.steps]
    texts.append(_FRONTMATTER_RE.sub("", rendered, count=1))
    return refuse_paths(texts, home_dir())


def validate_roundtrip(rendered: str, name: str) -> str | None:
    """Write rendered SKILL.md to a temp folder, load via the skills loader,
    confirm it parses with the expected name. Returns an error message or None."""
    tmp_root = Path(tempfile.mkdtemp())
    folder = tmp_root / name
    folder.mkdir(parents=True, exist_ok=True)
    try:
        (folder / SKILL_FILENAME).write_text(rendered, encoding="utf-8")
        entry = load_skill_folder(folder)
        if entry is None:
            return "loader rejected the rendered SKILL.md (frontmatter/size)."
        if entry.name != name:
            return f"frontmatter name {entry.name!r} does not match folder {name!r}."
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    finally:
        try:
            (folder / SKILL_FILENAME).unlink(missing_ok=True)
            folder.rmdir()
            tmp_root.rmdir()
        except OSError:
            pass
