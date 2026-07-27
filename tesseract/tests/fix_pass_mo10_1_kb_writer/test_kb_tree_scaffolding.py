"""MO-10-1 §2a — KB tree scaffolding is idempotent and creates the locked tree."""

from __future__ import annotations

from tesseract.knowledge_keeper import ensure_kb_tree, kb_root, KB_SUBDIRS


def test_ensure_kb_tree_creates_all_subdirs(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    assert base == kb_root()
    for sub in KB_SUBDIRS:
        assert (base / sub).is_dir()
        assert (base / sub / ".last-refresh").is_dir()
    for sub in ("providers", "cli-controls", "ecosystem"):
        log = base / sub / "_refresh-log.jsonl"
        assert log.is_file()
        # Empty seed.
        assert log.read_text(encoding="utf-8") == ""
    # roles/ has no refresh log of its own.
    assert not (base / "roles" / "_refresh-log.jsonl").exists()
    assert (base / "INDEX.md").is_file()


def test_ensure_kb_tree_is_idempotent_and_preserves_log(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    log = base / "providers" / "_refresh-log.jsonl"
    log.write_text('{"ts":"2026-05-15T00:00:00Z","file":"x.md","diff_summary":"seed"}\n', encoding="utf-8")
    ensure_kb_tree()
    assert log.read_text(encoding="utf-8").startswith('{"ts":"2026-05-15T00:00:00Z"')
