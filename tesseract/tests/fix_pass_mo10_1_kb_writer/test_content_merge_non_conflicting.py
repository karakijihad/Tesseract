"""MO-10-1 §2e — operator adds a section, refresher updates a different
section: both land cleanly, no conflict event."""

from __future__ import annotations

from tesseract.knowledge_keeper import MergeResult, ensure_kb_tree, merge_kb_file


def test_non_conflicting_paragraph_changes_merge_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    target = base / "providers" / "anthropic.md"

    # 1) initial refresh
    merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Para A original.\n"
            "\n"
            "Para B original.\n"
        ),
    )

    # 2) operator hand-edits — adds a NEW paragraph at the end (untouched
    #    by the refresher next cycle).
    current = target.read_text(encoding="utf-8")
    edited = current + "\nOperator added paragraph.\n"
    target.write_text(edited, encoding="utf-8")

    # 3) refresher updates Para B (its own content). Para A unchanged.
    res = merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Para A original.\n"
            "\n"
            "Para B updated.\n"
        ),
    )
    assert isinstance(res, MergeResult)
    final = target.read_text(encoding="utf-8")
    assert "Para A original." in final
    assert "Para B updated." in final
    assert "Operator added paragraph." in final
