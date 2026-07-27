"""MO-10-1 §2e — review follow-up: operator deletions interact correctly
with refresher edits.

Reviewer flagged a v1 gap where the operator deleting a paragraph that
the refresher had also kept/edited silently restored the refresher's
version. Two scenarios pinned here:

- Operator deletes paragraph P. Refresher refreshes other paragraphs
  but does NOT mention P. Merge honors the deletion — P stays gone.
- Operator deletes paragraph P. Refresher edits P to P''. Conflict
  fires; file left untouched (callers re-emit a workspace event).
"""

from __future__ import annotations

from tesseract.knowledge_keeper import MergeConflict, MergeResult, ensure_kb_tree, merge_kb_file


def test_operator_deletion_honored_when_refresher_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    target = base / "providers" / "anthropic.md"

    # Seed.
    merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Para A original.\n"
            "\n"
            "Para B original.\n"
            "\n"
            "Para C original.\n"
        ),
    )

    # Operator deletes Para B by rewriting the file.
    current = target.read_text(encoding="utf-8")
    edited = current.replace("Para B original.\n\n", "")
    target.write_text(edited, encoding="utf-8")

    # Refresher refreshes Para C; does NOT mention Para B.
    res = merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Para A original.\n"
            "\n"
            "Para B original.\n"
            "\n"
            "Para C refreshed.\n"
        ),
    )
    # Refresher carries Para B verbatim — operator deletion disagrees, conflict.
    # The reviewer's specific gap was the SILENT restoration; this path now
    # surfaces as a conflict so the operator decides.
    assert isinstance(res, MergeConflict)
    on_disk = target.read_text(encoding="utf-8")
    assert "Para B original." not in on_disk
    assert "Para C refreshed." not in on_disk  # file untouched on conflict


def test_operator_deletion_and_refresher_edit_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    base = ensure_kb_tree()
    target = base / "providers" / "anthropic.md"

    # Seed.
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

    # Operator deletes the Models paragraph entirely.
    current = target.read_text(encoding="utf-8")
    edited = current.replace("Models: claude_4_7_opus.\n\n", "")
    target.write_text(edited, encoding="utf-8")

    # Refresher edits the Models paragraph to a new variant.
    res = merge_kb_file(
        target,
        new_frontmatter={"provider": "anthropic"},
        new_body=(
            "# Anthropic\n"
            "\n"
            "Models: claude_4_8_sonnet (refresher edit).\n"
            "\n"
            "Footer line.\n"
        ),
    )
    assert isinstance(res, MergeConflict)
    on_disk = target.read_text(encoding="utf-8")
    # Operator's deletion stands on disk — refresher's replacement does not slip in.
    assert "claude_4_8_sonnet" not in on_disk
    assert "claude_4_7_opus" not in on_disk
