"""Smoke tests for soul_growth_propose.

After the workspace-redesign refactor (2026-05-06), this tool no longer
writes SOUL.md directly — it queues a `change_proposal` event in the
workspace inbox. The append-to-Growth semantics are preserved in the
event payload (action=append_to_section, section=Growth, content=bullet
line). Operator approves in workspace; the REST handler performs the
commit. Tests assert the **proposal payload** instead of the file body.
Workspace REST commit is covered separately in
`fix_pass_workspace_redesign/test_workspace_decision_commits_change.py`.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.soul_growth_propose import (
    SoulGrowthProposeInput,
    SoulGrowthProposeTool,
    _MAX_BULLET_CHARS,
)
from tesseract.workspace_events import EventStore


SOUL_FIXTURE = """\
---
name: TARS
version: 1
---

# SOUL

Living identity document.

## Vibe

Calm, dry.

## Growth

This section is mutable.

*Currently empty — emerges through use.*

## Continuity

What survives.
"""


def _ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="sess-test")


def _setup(tmp_path: Path) -> Path:
    soul = tmp_path / "tesseract" / "workspace" / "SOUL.md"
    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text(SOUL_FIXTURE, encoding="utf-8")
    return soul


async def test_first_bullet_proposes_with_placeholder_strip(tmp_path: Path, monkeypatch):
    """First bullet: tool queues a change_proposal whose preview-applied
    diff strips `*Currently empty*` and adds `- <bullet>` under Growth."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "tesseract"))
    soul = _setup(tmp_path)
    body_before = soul.read_text(encoding="utf-8")
    tool = SoulGrowthProposeTool(repo_root=tmp_path)
    res = await tool.run(
        SoulGrowthProposeInput(bullet="Operator wants opinions stated, not menus."),
        _ctx(),
    )
    assert not res.is_error
    assert soul.read_text(encoding="utf-8") == body_before, (
        "tool must not mutate SOUL.md — workspace approve does the commit"
    )
    store = EventStore(tmp_path / "tesseract" / "logs")
    events = store.list_events()
    assert len(events) == 1
    payload = events[0].payload
    assert payload["target_path"] == "tesseract/workspace/SOUL.md"
    assert payload["section"] == "Growth"
    assert payload["action"] == "append_to_section"
    assert payload["content"] == "- Operator wants opinions stated, not menus.\n"
    diff = payload["diff"]
    assert "Currently empty" in diff and "-" in diff  # placeholder removed
    assert "+- Operator wants opinions stated" in diff


async def test_second_bullet_proposes_separate_event(tmp_path: Path, monkeypatch):
    """Two propose calls produce two distinct change_proposal events;
    SOUL.md is untouched. Commit-time idempotency is the workspace REST
    handler's responsibility (covered separately)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "tesseract"))
    soul = _setup(tmp_path)
    body_before = soul.read_text(encoding="utf-8")
    tool = SoulGrowthProposeTool(repo_root=tmp_path)
    await tool.run(SoulGrowthProposeInput(bullet="first observation"), _ctx())
    await tool.run(SoulGrowthProposeInput(bullet="second observation"), _ctx())
    assert soul.read_text(encoding="utf-8") == body_before
    store = EventStore(tmp_path / "tesseract" / "logs")
    events = store.list_events()
    assert len(events) == 2
    bullets = sorted(ev.payload["content"].strip() for ev in events)
    assert bullets == ["- first observation", "- second observation"]


async def test_oversized_bullet_rejected(tmp_path: Path):
    _setup(tmp_path)
    tool = SoulGrowthProposeTool(repo_root=tmp_path)
    res = await tool.run(
        SoulGrowthProposeInput(bullet="x" * (_MAX_BULLET_CHARS + 1)),
        _ctx(),
    )
    assert res.is_error
    assert "too long" in res.output.lower()


async def test_missing_growth_section_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "tesseract"))
    soul = tmp_path / "tesseract" / "workspace" / "SOUL.md"
    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text("# SOUL\n\nNo growth section here.\n", encoding="utf-8")
    tool = SoulGrowthProposeTool(repo_root=tmp_path)
    res = await tool.run(SoulGrowthProposeInput(bullet="something"), _ctx())
    assert res.is_error
    assert "growth" in res.output.lower()


async def test_empty_bullet_rejected(tmp_path: Path):
    _setup(tmp_path)
    tool = SoulGrowthProposeTool(repo_root=tmp_path)
    res = await tool.run(SoulGrowthProposeInput(bullet="   "), _ctx())
    assert res.is_error
