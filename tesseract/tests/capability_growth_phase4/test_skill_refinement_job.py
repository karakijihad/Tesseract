"""Phase 4 4b — the refinement job flags underperforming skills.

Detection is pure arithmetic over usage.jsonl (no model needed), so the card
always fires for a genuinely underperforming skill. All writes stay under an
isolated ``TESSERACT_HOME``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tesseract.scheduler.tasks.skill_refinement import SkillRefinementJob
from tesseract.scheduler.types import JobContext
from tesseract.workspace_events import EventStore


def _setup(tmp_path: Path, outcomes: list[str], *, skill: str = "jane-doe-flaky") -> None:
    skills = tmp_path / "workspace" / "skills" / skill
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: fixture skill for X\n---\n\n## Steps\n1. go",
        encoding="utf-8",
    )
    usage = tmp_path / "logs" / "skills"
    usage.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    with (usage / "usage.jsonl").open("w", encoding="utf-8") as fh:
        for o in outcomes:
            fh.write(json.dumps({"ts": now, "skill": skill, "session_id": "s", "outcome": o}) + "\n")


def _cfg() -> dict:
    return {"window_days": 7, "min_loads": 3, "ratio_threshold": 0.34, "max_cards": 3}


async def test_underperforming_skill_files_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _setup(tmp_path, ["error", "error", "error", "ok"])
    res = await SkillRefinementJob().run(JobContext(job_name="skill_refinement", config=_cfg()))
    assert res.ok
    cards = EventStore(tmp_path / "logs").list_events(kinds=("skill_refinement",))
    assert len(cards) == 1
    assert cards[0].payload["name"] == "jane-doe-flaky"
    assert cards[0].payload["stats"] == {"total": 4, "negative": 3}


async def test_healthy_skill_no_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _setup(tmp_path, ["ok", "ok", "ok", "error"])
    res = await SkillRefinementJob().run(JobContext(job_name="skill_refinement", config=_cfg()))
    assert res.ok
    cards = EventStore(tmp_path / "logs").list_events(kinds=("skill_refinement",))
    assert cards == []


async def test_below_min_loads_no_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _setup(tmp_path, ["error", "error"])  # 2 < min_loads 3
    res = await SkillRefinementJob().run(JobContext(job_name="skill_refinement", config=_cfg()))
    assert res.ok
    assert EventStore(tmp_path / "logs").list_events(kinds=("skill_refinement",)) == []


async def test_dedup_against_open_card(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    _setup(tmp_path, ["error", "error", "error"])
    job = SkillRefinementJob()
    await job.run(JobContext(job_name="skill_refinement", config=_cfg()))
    await job.run(JobContext(job_name="skill_refinement", config=_cfg()))
    cards = EventStore(tmp_path / "logs").list_events(kinds=("skill_refinement",))
    assert len(cards) == 1  # second run sees the open card and skips


async def test_deleted_skill_not_flagged(tmp_path: Path, monkeypatch) -> None:
    """Usage for a skill that no longer exists on disk is ignored."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    usage = tmp_path / "logs" / "skills"
    usage.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    with (usage / "usage.jsonl").open("w", encoding="utf-8") as fh:
        for o in ["error", "error", "error"]:
            fh.write(json.dumps({"ts": now, "skill": "ghost", "session_id": "s", "outcome": o}) + "\n")
    (tmp_path / "workspace" / "skills").mkdir(parents=True)
    res = await SkillRefinementJob().run(JobContext(job_name="skill_refinement", config=_cfg()))
    assert res.ok
    assert EventStore(tmp_path / "logs").list_events(kinds=("skill_refinement",)) == []
