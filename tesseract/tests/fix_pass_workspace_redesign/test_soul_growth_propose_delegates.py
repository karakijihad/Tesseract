"""soul_growth_propose — thin wrapper, queues change_proposal event."""

from __future__ import annotations

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.soul_growth_propose import (
    SoulGrowthProposeInput,
    SoulGrowthProposeTool,
)
from tesseract.workspace_events import EventStore


def _seed(tmp_path):
    (tmp_path / "tesseract" / "workspace").mkdir(parents=True)
    (tmp_path / "tesseract" / "workspace" / "SOUL.md").write_text(
        "# Soul\n\n## Growth\n\n*Currently empty — TARS adds bullets here.*\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_soul_growth_does_not_write_soul_md(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "tesseract"))
    from importlib import reload

    from tesseract import paths
    reload(paths)
    from tesseract.kernel.tools import soul_growth_propose as sgp
    reload(sgp)

    before = (tmp_path / "tesseract/workspace/SOUL.md").read_text(encoding="utf-8")
    tool = sgp.SoulGrowthProposeTool(repo_root=tmp_path)
    out = await tool.run(
        sgp.SoulGrowthProposeInput(bullet="Operator wants opinions stated, not menus."),
        ToolContext(),
    )
    assert not out.is_error, out.output
    after = (tmp_path / "tesseract/workspace/SOUL.md").read_text(encoding="utf-8")
    assert after == before, "soul_growth_propose must not write SOUL.md directly"

    store = EventStore(tmp_path / "tesseract" / "logs")
    events = store.list_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "change_proposal"
    assert ev.payload["target_path"] == "tesseract/workspace/SOUL.md"
    assert ev.payload["action"] == "append_to_section"
    assert ev.payload["section"] == "Growth"
    assert ev.payload["kind_origin"] == "soul_growth"
    assert "Operator wants opinions" in ev.payload["content"]


@pytest.mark.asyncio
async def test_soul_growth_rejects_oversize_bullet(tmp_path):
    tool = SoulGrowthProposeTool(repo_root=tmp_path)
    out = await tool.run(
        SoulGrowthProposeInput(bullet="x" * 300),
        ToolContext(),
    )
    assert out.is_error
    assert "too long" in out.output


@pytest.mark.asyncio
async def test_soul_growth_default_posture_is_auto():
    assert SoulGrowthProposeTool.default_posture == "auto"
