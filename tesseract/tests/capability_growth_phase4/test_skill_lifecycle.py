"""Phase 4 — skill lifecycle: draft → quarantine → promote / reject.

Mirrors the Stage-10 agent tests: a drafted skill lands in quarantine and is
NOT surfaced live until the operator promotes it; reject archives it with a
reason sidecar; the loader never surfaces pending/rejected drafts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain.skills import (
    list_pending_skills,
    list_rejected_skills,
    list_skills_names,
    load_skills,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.skill_create import SkillCreateInput, SkillCreateTool
from tesseract.kernel.tools.skill_promote import (
    archive_rejected_skill,
    promote_pending_skill,
)


def _skills_dir(tmp_path: Path) -> Path:
    d = tmp_path / "workspace" / "skills"
    d.mkdir(parents=True)
    return d


def _attended_ctx() -> ToolContext:
    return ToolContext(ask_fn=lambda *a, **k: True, session_id="s")


def _headless_ctx() -> ToolContext:
    return ToolContext(session_id="s")  # ask_fn=None → unattended


async def test_create_lands_in_quarantine_not_live(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    tool = SkillCreateTool(skills_dir=skills)
    r = await tool.run(
        SkillCreateInput(
            name="jane-doe-helper",
            description="Do a thing when X. Use for Y.",
            instructions="## Steps\n1. do it",
            rationale="repeated chore",
        ),
        _attended_ctx(),
    )
    assert not r.is_error
    assert list_pending_skills(skills) == ["jane-doe-helper"]
    # Loader (live scan) must NOT surface the pending draft.
    assert list_skills_names(skills) == []
    assert load_skills(skills) == []


async def test_promote_activates(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    tool = SkillCreateTool(skills_dir=skills)
    await tool.run(
        SkillCreateInput(
            name="jane-doe-helper", description="d for when Y",
            instructions="## Steps\n1. go", rationale="r",
            allowed_tools=["file_read", "grep"],
        ),
        _attended_ctx(),
    )
    entry, err = promote_pending_skill(skills, "jane-doe-helper")
    assert err is None and entry is not None
    assert entry.allowed_tools == ("file_read", "grep")  # standard field survives
    assert list_skills_names(skills) == ["jane-doe-helper"]
    assert list_pending_skills(skills) == []


async def test_reject_archives_with_reason(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    tool = SkillCreateTool(skills_dir=skills)
    await tool.run(
        SkillCreateInput(name="john-doe-bad", description="bad one for Z",
                         instructions="x", rationale="r"),
        _attended_ctx(),
    )
    err = archive_rejected_skill(skills, "john-doe-bad", "not needed")
    assert err is None
    assert list_rejected_skills(skills) == ["john-doe-bad"]
    assert list_pending_skills(skills) == []
    reason = (skills / "rejected" / "john-doe-bad.reason.txt").read_text(encoding="utf-8")
    assert reason.strip() == "not needed"


async def test_rejected_name_cannot_be_reproposed(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    tool = SkillCreateTool(skills_dir=skills)
    inp = SkillCreateInput(name="john-doe-bad", description="desc for W",
                           instructions="x", rationale="r")
    await tool.run(inp, _attended_ctx())
    archive_rejected_skill(skills, "john-doe-bad", "no")
    r = await tool.run(inp, _attended_ctx())
    assert r.is_error and "previously rejected" in r.output


async def test_headless_pending_cap_enforced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    skills = _skills_dir(tmp_path)
    tool = SkillCreateTool(skills_dir=skills)
    # skill_pending_cap is 5 in runtime.yaml; fill it headlessly.
    for i in range(5):
        r = await tool.run(
            SkillCreateInput(name=f"doe-skill-{i}", description=f"desc {i} for use",
                             instructions="x", rationale="r"),
            _headless_ctx(),
        )
        assert not r.is_error, r.output
    over = await tool.run(
        SkillCreateInput(name="doe-skill-6", description="over the cap now",
                         instructions="x", rationale="r"),
        _headless_ctx(),
    )
    assert over.is_error and "cap" in over.output.lower()


async def test_duplicate_active_name_rejected(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    tool = SkillCreateTool(skills_dir=skills)
    inp = SkillCreateInput(name="jane-doe-helper", description="desc for use later",
                           instructions="## Steps\n1. x", rationale="r")
    await tool.run(inp, _attended_ctx())
    promote_pending_skill(skills, "jane-doe-helper")
    r = await tool.run(inp, _attended_ctx())
    assert r.is_error and "already exists" in r.output


def test_promote_missing_pending_errors(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    entry, err = promote_pending_skill(skills, "nope")
    assert entry is None and err is not None and "No pending skill" in err


def test_promote_malformed_pending_fails(tmp_path: Path) -> None:
    skills = _skills_dir(tmp_path)
    bad = skills / "pending" / "broken"
    bad.mkdir(parents=True)
    (bad / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    entry, err = promote_pending_skill(skills, "broken")
    assert entry is None and err is not None and "failed validation" in err
