"""Phase 4 4a — skill-usage telemetry.

Every write must land under an isolated ``TESSERACT_HOME`` and never touch the
production ``tesseract/logs/**`` tree (CLAUDE.md zero-tolerance rule).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain.skill_usage import (
    log_skill_load,
    maybe_log_skill_load,
    read_usage,
    skill_name_for_path,
    usage_log_path,
)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tesseract/workspace/skills/folder-inventory/SKILL.md", "folder-inventory"),
        (r"C:\x\tesseract\workspace\skills\my-skill\SKILL.md", "my-skill"),
        ("tesseract/workspace/skills/pending/draft/SKILL.md", None),
        ("tesseract/workspace/skills/rejected/foo/SKILL.md", None),
        ("tesseract/workspace/skills/folder-inventory/scripts/x.py", None),
        ("tesseract/workspace/SOUL.md", None),
        ("some/other/file.md", None),
    ],
)
def test_skill_name_for_path(path, expected) -> None:
    assert skill_name_for_path(path) == expected


def test_log_and_read_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    log_skill_load("jane-doe-helper", "s1", "ok")
    log_skill_load("jane-doe-helper", "s1", "error")

    rows = read_usage()
    assert [r["outcome"] for r in rows] == ["ok", "error"]
    assert all(r["skill"] == "jane-doe-helper" for r in rows)
    # The file lives under the isolated home, not the production tree.
    assert usage_log_path() == tmp_path / "logs" / "skills" / "usage.jsonl"
    assert str(tmp_path) in str(usage_log_path())


def test_maybe_log_only_fires_for_skill_bodies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    maybe_log_skill_load("tesseract/workspace/skills/a-skill/SKILL.md", "s", is_error=False)
    maybe_log_skill_load("tesseract/workspace/skills/pending/x/SKILL.md", "s", is_error=False)
    maybe_log_skill_load("tesseract/workspace/SOUL.md", "s", is_error=False)
    rows = read_usage()
    assert len(rows) == 1
    assert rows[0]["skill"] == "a-skill" and rows[0]["outcome"] == "ok"


def test_maybe_log_error_outcome(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    maybe_log_skill_load("tesseract/workspace/skills/broken/SKILL.md", "s", is_error=True)
    rows = read_usage()
    assert rows and rows[0]["outcome"] == "error"


async def test_file_read_tool_logs_skill_load(tmp_path: Path, monkeypatch) -> None:
    """The real hook: reading a SKILL.md through file_read logs one load."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.file_read import FileReadInput, FileReadTool

    workspace = tmp_path / "ws"
    skill_dir = workspace / "tesseract" / "workspace" / "skills" / "doe-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: doe-skill\ndescription: d\n---\nbody", encoding="utf-8")
    (workspace / "plain.txt").write_text("hello", encoding="utf-8")

    tool = FileReadTool()
    ctx = ToolContext(workspace_root=str(workspace), session_id="sess-x")

    # A plain file read logs nothing.
    await tool.run(FileReadInput(file_path="plain.txt"), ctx)
    assert read_usage() == []

    # A skill body read logs an "ok" load.
    await tool.run(
        FileReadInput(file_path="tesseract/workspace/skills/doe-skill/SKILL.md"), ctx,
    )
    rows = read_usage()
    assert len(rows) == 1
    assert rows[0]["skill"] == "doe-skill"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["session_id"] == "sess-x"
