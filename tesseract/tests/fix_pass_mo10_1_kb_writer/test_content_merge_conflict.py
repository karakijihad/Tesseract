"""MO-10-1 §2e — operator and refresher both edited the same paragraph.

Refresher leaves the file unchanged and returns a MergeConflict so the
caller emits a ``kb_merge_conflict`` workspace event.
"""

from __future__ import annotations

from tesseract.knowledge_keeper import MergeConflict, ensure_kb_tree, merge_kb_file


def test_same_paragraph_edited_on_both_sides_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    target = base / "providers" / "anthropic.md"

    merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Models: claude_4_7_opus.\n"
            "\n"
            "Footer line.\n"
        ),
    )

    # Operator rewrites the models paragraph.
    op_edited = (
        "---\n"
        "provider: anthropic\n"
        "---\n"
        "\n"
        "# Anthropic\n"
        "\n"
        "Models: claude_4_8_sonnet (operator).\n"
        "\n"
        "Footer line.\n"
    )
    target.write_text(op_edited, encoding="utf-8")

    # Refresher also wants to rewrite the models paragraph (different text).
    res = merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Models: claude_4_8_haiku (refresher).\n"
            "\n"
            "Footer line.\n"
        ),
    )
    assert isinstance(res, MergeConflict)
    on_disk = target.read_text(encoding="utf-8")
    # Operator's body stays on disk unchanged.
    assert "claude_4_8_sonnet (operator)" in on_disk
    assert "claude_4_8_haiku (refresher)" not in on_disk
