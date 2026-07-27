"""memory_promote tool — operator-confirmed lifecycle actions.

Covers archive / bump_importance / merge_into. Soul-growth delegation is
asserted at the boundary (the call reaches SoulGrowthProposeTool with
the right bullet); the soul-growth tool has its own coverage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.memory_promote import (
    MemoryPromoteInput,
    MemoryPromoteTool,
)
from tesseract.kernel.tools.soul_growth_propose import (
    SoulGrowthProposeInput,
    SoulGrowthProposeTool,
)
from tesseract.memory.index import MemoryIndex
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability


def _seed(
    store: MemoryStore,
    *,
    mem_id: str,
    title: str = "title",
    body: str = (
        "Operator wants tight responses with no trailing summaries — when "
        "the diff already says it, leave the talk for somewhere else."
    ),
    importance: int = 7,
    auto_links: list[str] | None = None,
) -> None:
    fm = MemoryFrontmatter(
        id=mem_id,
        type=MemoryType.FEEDBACK,
        title=title,
        summary=body[:80],
        importance=importance,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        stability=Stability.ACTIVE,
        auto_links=auto_links or [],
    )
    target = store.store_dir / "feedback" / f"{mem_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        + yaml.dump(fm.to_yaml_dict(), default_flow_style=False, sort_keys=False)
        + "---\n\n"
        + body
    )
    target.write_text(text, encoding="utf-8")


def _make_tool(tmp_path: Path) -> tuple[MemoryPromoteTool, MemoryStore, MemoryIndex]:
    store_dir = tmp_path / "memory-store"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(store_dir)
    index = MemoryIndex(store_dir=store_dir)
    soul = SoulGrowthProposeTool(repo_root=tmp_path)
    tool = MemoryPromoteTool(store=store, index=index, soul_growth_tool=soul)
    return tool, store, index


@pytest.mark.asyncio
async def test_archive_flips_stability(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_x")

    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_x", action="archive"),
        ToolContext(),
    )
    assert result.is_error is False
    fm, _ = store.read("mem_x")
    assert fm.stability == Stability.ARCHIVED


@pytest.mark.asyncio
async def test_archive_idempotent(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_x")
    await tool.run(MemoryPromoteInput(memory_id="mem_x", action="archive"), ToolContext())
    result = await tool.run(MemoryPromoteInput(memory_id="mem_x", action="archive"), ToolContext())
    assert result.is_error is False
    assert "already archived" in result.output.lower()


@pytest.mark.asyncio
async def test_bump_importance_clamps(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_x", importance=4)

    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_x", action="bump_importance", importance=9),
        ToolContext(),
    )
    assert result.is_error is False
    fm, _ = store.read("mem_x")
    assert fm.importance == 9


@pytest.mark.asyncio
async def test_bump_importance_requires_value(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_x")
    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_x", action="bump_importance"),
        ToolContext(),
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_merge_into_appends_body_and_archives_source(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_target",
          body="target body x" * 10, importance=7)
    _seed(store, mem_id="mem_source",
          body="source body y" * 10, importance=8,
          auto_links=["mem_other"])

    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_source", action="merge_into",
                           target="mem_target"),
        ToolContext(),
    )
    assert result.is_error is False

    target_fm, target_body = store.read("mem_target")
    assert "merged from mem_source" in target_body
    assert "source body y" in target_body
    assert target_fm.importance == 8  # max(7, 8)
    assert "mem_other" in target_fm.auto_links
    assert "mem_source" in target_fm.auto_links

    source_fm, _ = store.read("mem_source")
    assert source_fm.stability == Stability.ARCHIVED


@pytest.mark.asyncio
async def test_merge_into_self_rejected(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_x")
    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_x", action="merge_into", target="mem_x"),
        ToolContext(),
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_merge_into_missing_target_rejected(tmp_path: Path) -> None:
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_src")
    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_src", action="merge_into",
                           target="mem_does_not_exist"),
        ToolContext(),
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_unknown_id_returns_error(tmp_path: Path) -> None:
    tool, _, _ = _make_tool(tmp_path)
    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_ghost", action="archive"),
        ToolContext(),
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_propose_soul_growth_delegates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool, _, _ = _make_tool(tmp_path)

    seen: list[str] = []

    async def fake_soul_run(self, inp: SoulGrowthProposeInput, ctx: ToolContext):
        seen.append(inp.bullet)
        from tesseract.kernel.tools.base import ToolResult
        return ToolResult(output="appended")

    monkeypatch.setattr(SoulGrowthProposeTool, "run", fake_soul_run)

    result = await tool.run(
        MemoryPromoteInput(
            memory_id="mem_x", action="propose_soul_growth",
            bullet="Operator wants tight, direct answers.",
        ),
        ToolContext(),
    )
    assert result.is_error is False
    assert seen == ["Operator wants tight, direct answers."]


@pytest.mark.asyncio
async def test_merge_into_archive_failure_is_not_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the source-archive step fails after target was merged, the result
    must NOT be is_error — the Inbox would treat that as a failed action and
    retry, which would double-append the body to target. Instead surface the
    merge as done with a 'manual archive needed' suffix."""
    tool, store, _ = _make_tool(tmp_path)
    _seed(store, mem_id="mem_target", importance=7)
    _seed(store, mem_id="mem_source", importance=8)

    real_write = store.write
    call_count = {"n": 0}

    def fake_write(fm, body, *, subdir_override=None):
        call_count["n"] += 1
        # First write = target update (succeeds). Second = source archive (block).
        if call_count["n"] == 2:
            return False
        return real_write(fm, body, subdir_override=subdir_override)

    monkeypatch.setattr(store, "write", fake_write)

    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_source", action="merge_into",
                           target="mem_target"),
        ToolContext(),
    )
    assert result.is_error is False
    assert "manual archive needed" in result.output


@pytest.mark.asyncio
async def test_propose_soul_growth_requires_bullet(tmp_path: Path) -> None:
    tool, _, _ = _make_tool(tmp_path)
    result = await tool.run(
        MemoryPromoteInput(memory_id="mem_x", action="propose_soul_growth"),
        ToolContext(),
    )
    assert result.is_error is True
