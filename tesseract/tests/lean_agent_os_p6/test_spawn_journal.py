"""P6 Task 3 §G5 — spawn journal: best-effort start/terminal writer + the
resume-time orphan sweep it feeds.

Design: Docs/Plan/lean-agent-os/idle-wake-design.md §G5. Probe (chat.py
inspection — see .superpowers/sdd/p6-task-3-report.md) confirmed the
restored-history-scan alternative is not viable: `chat_store.py` persists
role/content only, and spawn completions are one-shot injections spliced
into the adapter message list, never appended to `ChatSession.history`. This
module is the journal that replaces the non-viable scan.
"""

from __future__ import annotations

import json

import pytest

from tesseract.brain import spawn_journal


def test_journal_path_resolves_under_tesseract_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    path = spawn_journal.spawn_journal_path("sess-1")
    assert path == tmp_path / "logs" / "sessions" / "sess-1" / "spawns.jsonl"


def test_record_start_then_terminal_written_as_two_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawn_journal.record_start("sess-1", "h1", "delegate_claude", "2026-07-06T00:00:00+00:00")
    spawn_journal.record_terminal("sess-1", "h1", "done")

    lines = [
        json.loads(line)
        for line in spawn_journal.spawn_journal_path("sess-1").read_text(encoding="utf-8").splitlines()
    ]
    assert lines[0]["event"] == "start"
    assert lines[0]["handle_id"] == "h1"
    assert lines[1]["event"] == "terminal"
    assert lines[1]["outcome"] == "done"


def test_sweep_orphans_returns_start_without_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawn_journal.record_start("sess-1", "h1", "delegate_claude", "t0")
    spawn_journal.record_start("sess-1", "h2", "delegate_codex", "t0")
    spawn_journal.record_terminal("sess-1", "h2", "done")  # h2 completed normally

    orphans = spawn_journal.sweep_orphans("sess-1")
    assert [o["handle_id"] for o in orphans] == ["h1"]


def test_sweep_orphans_flags_parked_spawns(tmp_path, monkeypatch):
    """Deferred 2026-07-12 — an orphan that was holding a parked ASK (trio
    W4) carries was_parked=True so `_format_spawn_lost` can say so; a plain
    orphan carries was_parked=False."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawn_journal.record_start("sess-1", "h1", "delegate_claude", "t0")
    spawn_journal.record_start("sess-1", "h2", "delegate_codex", "t0")
    spawn_journal.record_parked("sess-1", "h1")

    orphans = {o["handle_id"]: o for o in spawn_journal.sweep_orphans("sess-1")}
    assert orphans["h1"]["was_parked"] is True
    assert orphans["h2"]["was_parked"] is False


def test_sweep_orphans_marks_terminal_so_second_sweep_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    spawn_journal.record_start("sess-1", "h1", "delegate_claude", "t0")

    first = spawn_journal.sweep_orphans("sess-1")
    second = spawn_journal.sweep_orphans("sess-1")
    assert [o["handle_id"] for o in first] == ["h1"]
    assert second == []


def test_sweep_orphans_no_journal_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    assert spawn_journal.sweep_orphans("never-existed") == []


def test_sweep_orphans_blank_session_id_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    assert spawn_journal.sweep_orphans("") == []


def test_sweep_orphans_skips_corrupt_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    path = spawn_journal.spawn_journal_path("sess-1")
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"event": "start", "handle_id": "h1", "kind": "delegate_claude", "started_at": "t0"}\n'
        "not-json\n",
        encoding="utf-8",
    )
    orphans = spawn_journal.sweep_orphans("sess-1")
    assert [o["handle_id"] for o in orphans] == ["h1"]


def test_append_write_failure_never_raises(tmp_path, monkeypatch):
    """A journal write failure must never propagate — memory-write discipline
    (CLAUDE.md, mirrored by the p6-task-3 brief's journal constraint)."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(spawn_journal.json, "dumps", _boom)
    spawn_journal.record_start("sess-1", "h1", "delegate_claude", "t0")  # must not raise
    spawn_journal.record_terminal("sess-1", "h1", "done")  # must not raise

    # `open("a")` may still create an empty file; no *event line* was ever
    # written since every dumps() call raised before f.write() completed.
    path = spawn_journal.spawn_journal_path("sess-1")
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    assert "handle_id" not in content
