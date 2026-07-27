"""propose_change tool — writes pending event only, no file mutation."""

from __future__ import annotations

import json

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.propose_change import (
    ProposeChangeInput,
    ProposeChangeTool,
)
from tesseract.workspace_events import EventStore


def _seed_workspace(tmp_path):
    (tmp_path / "tesseract" / "workspace").mkdir(parents=True)
    (tmp_path / "tesseract" / "workspace" / "IDENTITY.md").write_text(
        "# Identity\n\n## Stance\n\nbase line\n", encoding="utf-8",
    )
    (tmp_path / "tesseract" / "workspace" / "SOUL.md").write_text(
        "# Soul\n\n## Growth\n\n*Currently empty.*\n", encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_propose_change_does_not_mutate_target(tmp_path, monkeypatch):
    _seed_workspace(tmp_path)
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path / "tesseract"))
    # paths.TESSERACT_HOME is read at import; reload not strictly needed
    # because the tool uses TESSERACT_HOME at call-time via EventStore ctor
    # but we rebuild the constant for clarity.
    from importlib import reload

    from tesseract import paths
    reload(paths)
    from tesseract.kernel.tools import propose_change as pc_mod
    reload(pc_mod)

    tool = pc_mod.ProposeChangeTool(repo_root=tmp_path)
    out = await tool.run(
        pc_mod.ProposeChangeInput(
            target_path="tesseract/workspace/IDENTITY.md",
            action="append",
            content="\nnew stance line\n",
            summary="add a new stance line",
        ),
        ToolContext(),
    )
    assert not out.is_error, out.output
    body_after = (tmp_path / "tesseract/workspace/IDENTITY.md").read_text(encoding="utf-8")
    assert "new stance line" not in body_after, "tool must not mutate target"

    store = EventStore(tmp_path / "tesseract" / "logs")
    events = store.list_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "change_proposal"
    assert ev.payload["target_path"] == "tesseract/workspace/IDENTITY.md"
    assert ev.payload["action"] == "append"
    assert ev.payload["expected_hash_before"]
    assert "@@" in ev.payload["diff"]


@pytest.mark.asyncio
async def test_propose_change_rejects_off_allowlist(tmp_path):
    tool = ProposeChangeTool(repo_root=tmp_path)
    out = await tool.run(
        ProposeChangeInput(
            target_path="tesseract/kernel/secret.py",
            action="append",
            content="x",
            summary="malicious",
        ),
        ToolContext(),
    )
    assert out.is_error
    assert "not in PROPOSABLE_PATHS" in out.output


@pytest.mark.asyncio
async def test_propose_change_enforces_content_size(tmp_path):
    _seed_workspace(tmp_path)
    tool = ProposeChangeTool(repo_root=tmp_path)
    out = await tool.run(
        ProposeChangeInput(
            target_path="tesseract/workspace/IDENTITY.md",
            action="append",
            content="x" * (8 * 1024 + 1),
            summary="too big",
        ),
        ToolContext(),
    )
    assert out.is_error
    assert "too large" in out.output


@pytest.mark.asyncio
async def test_propose_change_default_posture_is_auto():
    assert ProposeChangeTool.default_posture == "auto"


@pytest.mark.asyncio
async def test_propose_change_payload_round_trips_json():
    """Payload must be JSON-serializable for events.jsonl persistence."""
    # Use small synthetic payload through model_dump path — guards against
    # accidentally embedding non-serializable values (Path, datetime, etc.)
    inp = ProposeChangeInput(
        target_path="tesseract/workspace/SOUL.md",
        action="append_to_section",
        content="- bullet\n",
        summary="rationale",
        section="Growth",
    )
    json.dumps(inp.model_dump())
