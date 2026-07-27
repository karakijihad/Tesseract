from __future__ import annotations

from pathlib import Path

import yaml

from tesseract.brain.session_store import save_session


def test_save_session_increments_soul_interaction_count(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text(
        "---\n"
        "entity_color: 246 83% 68%\n"
        "interaction_count: 5\n"
        "last_reflection: null\n"
        "name: TARS\n"
        "version: 2\n"
        "---\n\n# SOUL\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    save_session(
        session_dir=tmp_path / "sessions",
        name="test-session",
        model="test-model",
        started_at="2026-05-20T10:00:00+01:00",
        history=[{"role": "user", "content": "hi"}],
    )

    raw = soul.read_text(encoding="utf-8")
    front_text = raw.split("---\n", 2)[1]
    front = yaml.safe_load(front_text)
    assert front["interaction_count"] == 6


def test_save_session_index_work_flag(tmp_path, monkeypatch):
    from tesseract.memory.work_index import WorkIndex

    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    history = [{"role": "user", "content": "zentaur uniquetoken recall"}]

    # index_work=False — Mirror's multi-chat autosave path; the active-chat
    # snapshot must NOT add recall chunks (each chat is indexed separately).
    save_session(
        session_dir=tmp_path / "sessions",
        name="2026-06-29-1200",
        model="test-model",
        started_at="2026-06-29T12:00:00+00:00",
        history=history,
        index_work=False,
    )
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    try:
        assert idx.search("zentaur", source="session") == []
    finally:
        idx.close()

    # Default (REPL / operator /save) still indexes for recall.
    save_session(
        session_dir=tmp_path / "sessions",
        name="2026-06-29-1201",
        model="test-model",
        started_at="2026-06-29T12:01:00+00:00",
        history=history,
    )
    idx = WorkIndex(tmp_path / "work_index.sqlite")
    try:
        assert len(idx.search("zentaur", source="session")) >= 1
    finally:
        idx.close()


def test_save_session_no_crash_when_soul_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    save_session(
        session_dir=tmp_path / "sessions",
        name="test-session",
        model="test-model",
        started_at="2026-05-20T10:00:00+01:00",
        history=[],
    )
    assert (tmp_path / "sessions" / "test-session.json").exists()


def test_save_session_preserves_other_soul_frontmatter(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text(
        "---\n"
        "entity_color: 246 83% 68%\n"
        "interaction_count: 5\n"
        "last_reflection: null\n"
        "name: TARS\n"
        "version: 2\n"
        "---\n\n# SOUL\n\noriginal body content\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    save_session(
        session_dir=tmp_path / "sessions",
        name="test-session",
        model="test-model",
        started_at="2026-05-20T10:00:00+01:00",
        history=[],
    )
    raw = soul.read_text(encoding="utf-8")
    front_text = raw.split("---\n", 2)[1]
    front = yaml.safe_load(front_text)
    assert front["name"] == "TARS"
    assert front["entity_color"] == "246 83% 68%"
    assert front["version"] == 2
    assert "original body content" in raw


def test_save_session_preserves_null_last_reflection_as_null_not_tilde(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    soul.write_text(
        "---\n"
        "entity_color: 246 83% 68%\n"
        "interaction_count: 5\n"
        "last_reflection: null\n"
        "name: TARS\n"
        "version: 2\n"
        "---\n\n# SOUL\n\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    save_session(
        session_dir=tmp_path / "sessions",
        name="test-session",
        model="test-model",
        started_at="2026-05-20T10:00:00+01:00",
        history=[],
    )
    raw = soul.read_text(encoding="utf-8")
    # `null` must survive the round-trip — not become `~`
    assert "last_reflection: null" in raw, (
        f"last_reflection should round-trip as `null`, got SOUL.md:\n{raw}"
    )


def test_save_session_preserves_key_order(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    soul = workspace / "SOUL.md"
    # Deliberately non-alphabetical order
    soul.write_text(
        "---\n"
        "name: TARS\n"
        "version: 2\n"
        "interaction_count: 5\n"
        "entity_color: 246 83% 68%\n"
        "last_reflection: null\n"
        "---\n\n# SOUL\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    save_session(
        session_dir=tmp_path / "sessions",
        name="test-session", model="test-model",
        started_at="2026-05-20T10:00:00+01:00",
        history=[],
    )
    raw = soul.read_text(encoding="utf-8")
    front_text = raw.split("---\n", 2)[1]
    lines = [l for l in front_text.splitlines() if l.strip() and not l.startswith("#")]
    keys_in_order = [l.split(":")[0].strip() for l in lines]
    assert keys_in_order == ["name", "version", "interaction_count", "entity_color", "last_reflection"], (
        f"key order changed: {keys_in_order}"
    )
