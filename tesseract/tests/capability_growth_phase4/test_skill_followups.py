"""Phase 4 follow-ups — correction outcome producer + skill_refine tool."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tesseract.brain.skill_usage import (
    attribute_session_corrections,
    log_skill_load,
    read_usage,
)
from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.skill_create import SkillCreateInput, SkillCreateTool
from tesseract.kernel.tools.skill_promote import promote_pending_skill
from tesseract.kernel.tools.skill_refine import SkillRefineInput, SkillRefineTool
from tesseract.workspace_events import EventStore


# ── correction producer ──────────────────────────────────


def test_attribute_corrections_flags_session_skills(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    log_skill_load("skill-a", "sess-1", "ok")
    log_skill_load("skill-b", "sess-1", "error")
    log_skill_load("skill-c", "sess-2", "ok")  # different session — untouched

    added = attribute_session_corrections("sess-1")
    assert added == 2

    outcomes = {(r["skill"], r["outcome"]) for r in read_usage()}
    assert ("skill-a", "correction") in outcomes
    assert ("skill-b", "correction") in outcomes
    assert ("skill-c", "correction") not in outcomes


def test_attribute_corrections_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    log_skill_load("skill-a", "sess-1", "ok")
    assert attribute_session_corrections("sess-1") == 1
    assert attribute_session_corrections("sess-1") == 0  # already corrected
    corrections = [r for r in read_usage() if r["outcome"] == "correction"]
    assert len(corrections) == 1


def test_attribute_corrections_empty_session_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    assert attribute_session_corrections("") == 0
    assert attribute_session_corrections("ghost") == 0


def test_correction_gate_requires_durable_save(tmp_path: Path, monkeypatch) -> None:
    """A feedback memory_save that was deduped/blocked (not `status==saved`)
    must NOT attribute corrections — only a durable save counts."""
    from types import SimpleNamespace

    from tesseract.brain.session_ops import _attribute_skill_corrections

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    log_skill_load("skill-x", "sess-9", "ok")
    sess = SimpleNamespace(tool_context=SimpleNamespace(session_id="sess-9"))

    # deduped feedback save → no attribution
    _attribute_skill_corrections(
        sess, [{"tool": "memory_save", "save_type": "feedback", "status": "deduped"}],
    )
    assert not any(r["outcome"] == "correction" for r in read_usage())

    # durable feedback save → attributes
    _attribute_skill_corrections(
        sess, [{"tool": "memory_save", "save_type": "feedback", "status": "saved"}],
    )
    assert any(r["outcome"] == "correction" and r["skill"] == "skill-x" for r in read_usage())


# ── skill_refine tool ────────────────────────────────────


_REVISED = (
    "---\nname: jane-doe-helper\ndescription: revised helper for when X\n---\n\n"
    "## Steps\n1. do it carefully\n2. verify\n"
)


async def _make_active_skill(tmp_path: Path, store: EventStore) -> Path:
    skills = tmp_path / "workspace" / "skills"
    skills.mkdir(parents=True)
    tool = SkillCreateTool(skills_dir=skills)
    await tool.run(
        SkillCreateInput(
            name="jane-doe-helper", description="helper for when X",
            instructions="## Steps\n1. do it", rationale="r",
        ),
        ToolContext(ask_fn=lambda *a, **k: True, session_id="s"),
    )
    promote_pending_skill(skills, "jane-doe-helper")
    return skills


async def test_refine_files_card_without_mutating_live(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    skills = await _make_active_skill(tmp_path, store)
    before = (skills / "jane-doe-helper" / "SKILL.md").read_text(encoding="utf-8")

    tool = SkillRefineTool(skills_dir=skills, event_store=store)
    r = await tool.run(
        SkillRefineInput(name="jane-doe-helper", proposed_markdown=_REVISED, rationale="stale"),
        ToolContext(ask_fn=lambda *a, **k: True, session_id="s"),
    )
    assert not r.is_error
    # Live skill unchanged — approval is the operator's.
    assert (skills / "jane-doe-helper" / "SKILL.md").read_text(encoding="utf-8") == before
    cards = store.list_events(kinds=("skill_refinement",))
    assert len(cards) == 1
    assert cards[0].payload["name"] == "jane-doe-helper"
    assert cards[0].payload["proposed_markdown"] == _REVISED


async def test_refine_unknown_skill_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    skills = tmp_path / "workspace" / "skills"
    skills.mkdir(parents=True)
    tool = SkillRefineTool(skills_dir=skills, event_store=store)
    r = await tool.run(
        SkillRefineInput(name="ghost", proposed_markdown=_REVISED, rationale="x"),
        ToolContext(ask_fn=lambda *a, **k: True, session_id="s"),
    )
    assert r.is_error and "No active skill" in r.output


async def test_refine_name_mismatch_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    skills = await _make_active_skill(tmp_path, store)
    bad = "---\nname: someone-else\ndescription: mismatched name field\n---\n\nbody\n"
    tool = SkillRefineTool(skills_dir=skills, event_store=store)
    r = await tool.run(
        SkillRefineInput(name="jane-doe-helper", proposed_markdown=bad, rationale="x"),
        ToolContext(ask_fn=lambda *a, **k: True, session_id="s"),
    )
    assert r.is_error and "must match" in r.output


async def test_refine_dedups_open_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    skills = await _make_active_skill(tmp_path, store)
    tool = SkillRefineTool(skills_dir=skills, event_store=store)
    ctx = ToolContext(ask_fn=lambda *a, **k: True, session_id="s")
    inp = SkillRefineInput(name="jane-doe-helper", proposed_markdown=_REVISED, rationale="x")
    assert not (await tool.run(inp, ctx)).is_error
    second = await tool.run(inp, ctx)
    assert second.is_error and "already open" in second.output
    assert len(store.list_events(kinds=("skill_refinement",))) == 1
