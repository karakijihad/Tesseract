"""Smoke tests for the diary_append tool.

Validates the write path: per-day file, header on first entry, append on
subsequent entries, length cap, empty-text rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.diary_append import (
    DiaryAppendInput,
    DiaryAppendTool,
    _DIARY_REL,
    _MAX_ENTRY_CHARS,
)


def _ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="sess-test")


async def test_first_entry_creates_dated_file_with_header(tmp_path: Path):
    tool = DiaryAppendTool(repo_root=tmp_path)
    res = await tool.run(DiaryAppendInput(text="felt stiff in turn 14"), _ctx())
    assert not res.is_error
    diary_dir = tmp_path / _DIARY_REL
    files = list(diary_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("# Diary —")
    assert "felt stiff in turn 14" in body


async def test_second_entry_appends_to_same_file(tmp_path: Path):
    tool = DiaryAppendTool(repo_root=tmp_path)
    await tool.run(DiaryAppendInput(text="entry one"), _ctx())
    await tool.run(DiaryAppendInput(text="entry two"), _ctx())
    diary_dir = tmp_path / _DIARY_REL
    files = list(diary_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert body.count("# Diary —") == 1  # header only once
    assert "entry one" in body
    assert "entry two" in body


async def test_empty_text_rejected(tmp_path: Path):
    tool = DiaryAppendTool(repo_root=tmp_path)
    res = await tool.run(DiaryAppendInput(text="   \n  "), _ctx())
    assert res.is_error
    assert "empty" in res.output.lower()


async def test_too_long_entry_rejected(tmp_path: Path):
    tool = DiaryAppendTool(repo_root=tmp_path)
    text = "x" * (_MAX_ENTRY_CHARS + 1)
    res = await tool.run(DiaryAppendInput(text=text), _ctx())
    assert res.is_error
    assert "too long" in res.output.lower()


async def test_entry_includes_timestamp_marker(tmp_path: Path):
    tool = DiaryAppendTool(repo_root=tmp_path)
    res = await tool.run(DiaryAppendInput(text="abc"), _ctx())
    assert not res.is_error
    body = (tmp_path / _DIARY_REL).glob("*.md").__next__().read_text(encoding="utf-8")
    # Format: **HH:MM**  abc
    assert "**" in body
    assert "abc" in body
