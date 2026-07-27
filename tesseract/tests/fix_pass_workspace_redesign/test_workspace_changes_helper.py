"""workspace_changes helper module — propose/commit primitives."""

from __future__ import annotations

import pytest

from tesseract.kernel.workspace_changes import (
    PROPOSABLE_PATHS,
    ConcurrentModificationError,
    ProposeError,
    apply_change,
    compute_diff,
    hash_text,
    preview_change,
    validate_action,
    validate_target,
)


def _make_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.paths import workspace_dir
    ws = workspace_dir()
    ws.mkdir(parents=True)
    return ws


def _seed(ws, name: str, body: str) -> None:
    (ws / name).write_text(body, encoding="utf-8")


def test_validate_target_accepts_allowlisted_path(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path, monkeypatch)
    _seed(ws, "IDENTITY.md", "# id\n")
    full = validate_target(tmp_path, "tesseract/workspace/IDENTITY.md")
    assert full == (ws / "IDENTITY.md").resolve()


def test_validate_target_rejects_off_allowlist(tmp_path):
    with pytest.raises(ProposeError, match="not in PROPOSABLE_PATHS"):
        validate_target(tmp_path, "tesseract/kernel/foo.py")


def test_validate_target_rejects_traversal(tmp_path):
    with pytest.raises(ProposeError):
        validate_target(tmp_path, "../escape.md")


def test_validate_action_enforces_allowed(tmp_path):
    assert validate_action("tesseract/workspace/SOUL.md", "append") == "append"
    with pytest.raises(ProposeError, match="not allowed"):
        validate_action("tesseract/workspace/SOUL.md", "rmrf")


def test_preview_change_append_to_section_strips_placeholder(tmp_path):
    body = (
        "# Soul\n\n"
        "## Growth\n\n"
        "*Currently empty — TARS adds bullets via propose.*\n\n"
        "## Boundaries\n\nstuff\n"
    )
    after = preview_change(
        current_text=body,
        action="append_to_section",
        content="- a stable bullet\n",
        section="Growth",
    )
    assert "*Currently empty" not in after
    assert "- a stable bullet" in after
    assert "## Boundaries" in after  # rest preserved


def test_preview_change_append_to_missing_section_errors():
    with pytest.raises(ProposeError, match="not found"):
        preview_change(
            current_text="# foo\n",
            action="append_to_section",
            content="- x\n",
            section="Missing",
        )


def test_apply_change_writes_atomically(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path, monkeypatch)
    _seed(ws, "IDENTITY.md", "# id\n\nbase\n")
    h = hash_text("# id\n\nbase\n")
    applied = apply_change(
        repo_root=tmp_path,  # accepted for signature compat, unused for resolution
        target_path="tesseract/workspace/IDENTITY.md",
        action="append",
        content="\nadded line\n",
        expected_hash_before=h,
    )
    text_after = (ws / "IDENTITY.md").read_text(encoding="utf-8")
    assert text_after.endswith("added line\n")
    assert applied.hash_before == h
    assert applied.hash_after == hash_text(text_after)
    assert applied.bytes_after > applied.bytes_before


def test_apply_change_concurrent_modification_blocks(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path, monkeypatch)
    _seed(ws, "SOUL.md", "# soul\n")
    stale_hash = hash_text("# OLD CONTENT\n")
    with pytest.raises(ConcurrentModificationError) as exc_info:
        apply_change(
            repo_root=tmp_path,
            target_path="tesseract/workspace/SOUL.md",
            action="append",
            content="x\n",
            expected_hash_before=stale_hash,
        )
    assert exc_info.value.target == "tesseract/workspace/SOUL.md"
    assert exc_info.value.expected == stale_hash


def test_compute_diff_is_unified():
    diff = compute_diff("a\nb\n", "a\nB\n", target_label="t")
    assert "@@" in diff
    assert "-b" in diff
    assert "+B" in diff


def test_proposable_paths_covers_all_workspace_md_files():
    expected = {
        "SOUL.md", "IDENTITY.md", "FOUNDATION.md", "USER.md", "VOICE.md",
        "AGENTS.md", "HEARTBEAT.md", "MCP.md", "TOOLS.md", "WORKSHOP.md",
        "DIARY.md", "BOOT.md",
    }
    actual = {p.split("/")[-1] for p in PROPOSABLE_PATHS}
    assert actual == expected
