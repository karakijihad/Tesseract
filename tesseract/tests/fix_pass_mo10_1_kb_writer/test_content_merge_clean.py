"""MO-10-1 §2e — refresher overwrite when operator hasn't edited."""

from __future__ import annotations

from tesseract.knowledge_keeper import MergeResult, ensure_kb_tree, merge_kb_file


def test_first_write_creates_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    target = base / "providers" / "anthropic.md"
    res = merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic", "updated_at": "2026-05-15T06:30:00Z"},
        new_body="# Anthropic\n\nFirst body.\n",
    )
    assert isinstance(res, MergeResult)
    assert res.changed
    text = target.read_text(encoding="utf-8")
    assert "provider: anthropic" in text
    assert "First body." in text
    snap = (base / "providers" / ".last-refresh" / "anthropic.md").read_text(encoding="utf-8")
    assert "First body." in snap


def test_no_operator_edit_refresher_overwrites_body(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    target = base / "providers" / "anthropic.md"
    merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body="# Anthropic\n\nOld body.\n",
    )
    res = merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic", "updated_at": "2026-05-16T00:00:00Z"},
        new_body="# Anthropic\n\nNew body.\n",
    )
    assert isinstance(res, MergeResult)
    assert res.changed
    text = target.read_text(encoding="utf-8")
    assert "New body." in text
    assert "Old body." not in text
    assert "updated_at: '2026-05-16T00:00:00Z'" in text or "updated_at: 2026-05-16T00:00:00Z" in text
