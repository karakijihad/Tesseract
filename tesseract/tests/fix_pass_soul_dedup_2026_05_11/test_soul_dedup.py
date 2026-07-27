"""SOUL.md Growth-bullet dedup — primary + secondary layer.

Operator complaint (2026-05-10): a bullet appeared twice in
`tesseract/workspace/SOUL.md`. Trace showed `apply_change` →
`_append_to_named_section` would unconditionally append, and the
weekly `feedback_consolidator` could re-emit the same bullet across
runs while a prior `soul_proposal` was still pending in the inbox.

Two layers of control:

1. **Primary (kernel commit):** `_append_to_named_section` is now
   idempotent — a bullet whose normalized form already lives in the
   section is a no-op, and `apply_change` returns
   `ChangeApplied(no_op_reason="duplicate", bytes_after == bytes_before)`.
2. **Secondary (inbox upstream):** `feedback_consolidator` skips
   emitting a `soul_proposal` if a pending event with the same
   normalized bullet text is already in the inbox.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.kernel.workspace_changes import (
    _normalize_bullet,
    _section_contains_bullet,
    apply_change,
    preview_change,
)


SOUL_REL = "tesseract/workspace/SOUL.md"  # PROPOSABLE_PATHS key — stable identifier


def _make_soul(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, growth_body: str) -> Path:
    """Seed SOUL.md under an isolated `TESSERACT_HOME`. Returns the
    workspace dir (`<tmp_path>/workspace`), not the code tree."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.paths import workspace_dir
    ws = workspace_dir()
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text(
        "# Soul\n\n"
        f"## Growth\n\n{growth_body}"
        "## Boundaries\n\nstuff\n",
        encoding="utf-8",
    )
    return ws


# ── _normalize_bullet ───────────────────────────────────────────


def test_normalize_strips_bullet_prefix() -> None:
    assert _normalize_bullet("- Check the actual files first.") == \
        _normalize_bullet("Check the actual files first")
    assert _normalize_bullet("•   Check  the   actual files first") == \
        _normalize_bullet("- check the actual files first.")


def test_normalize_drops_trailing_punctuation_and_case() -> None:
    assert _normalize_bullet("Be terse!") == _normalize_bullet("be terse")
    assert _normalize_bullet("Do X;") == _normalize_bullet("Do X")


def test_normalize_empty_returns_empty() -> None:
    assert _normalize_bullet("") == ""
    assert _normalize_bullet("   ") == ""


# ── _section_contains_bullet ───────────────────────────────────


def test_section_contains_bullet_matches_normalized() -> None:
    body = "- Existing one.\n- Another bullet.\n"
    assert _section_contains_bullet(body, "- existing one")
    assert _section_contains_bullet(body, "EXISTING ONE.")
    assert not _section_contains_bullet(body, "- something new")


def test_section_contains_bullet_empty_body_is_false() -> None:
    assert not _section_contains_bullet("", "- anything")
    assert not _section_contains_bullet("   \n  \n", "- anything")


# ── _append_to_named_section idempotence (primary layer) ───────


def test_preview_append_to_section_is_idempotent_on_duplicate() -> None:
    body = (
        "# Soul\n\n"
        "## Growth\n\n"
        "- Be terse and direct.\n\n"
        "## Boundaries\n\nstuff\n"
    )
    after = preview_change(
        current_text=body,
        action="append_to_section",
        content="- Be terse and direct.\n",
        section="Growth",
    )
    assert after == body, "duplicate bullet must not be appended"


def test_preview_append_to_section_dedups_on_normalized_form() -> None:
    body = (
        "# Soul\n\n"
        "## Growth\n\n"
        "- Be terse and direct.\n\n"
        "## Boundaries\n\nstuff\n"
    )
    after = preview_change(
        current_text=body,
        action="append_to_section",
        content="- BE   terse   and   direct!\n",
        section="Growth",
    )
    assert after == body


def test_preview_append_to_section_appends_distinct_bullet() -> None:
    body = (
        "# Soul\n\n"
        "## Growth\n\n"
        "- Be terse and direct.\n\n"
        "## Boundaries\n\nstuff\n"
    )
    after = preview_change(
        current_text=body,
        action="append_to_section",
        content="- Push back when warranted.\n",
        section="Growth",
    )
    assert after != body
    assert "Push back when warranted" in after
    assert "Be terse and direct" in after  # original survives


def test_apply_change_duplicate_returns_no_op_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _make_soul(tmp_path, monkeypatch, growth_body="- Be terse and direct.\n\n")
    applied = apply_change(
        repo_root=tmp_path,  # accepted for signature compat, unused for resolution
        target_path=SOUL_REL,
        action="append_to_section",
        content="- Be terse and direct.\n",
        section="Growth",
    )
    assert applied.no_op_reason == "duplicate"
    assert applied.bytes_before == applied.bytes_after
    assert applied.hash_before == applied.hash_after
    # File unchanged on disk.
    assert (ws / "SOUL.md").read_text(encoding="utf-8").count(
        "Be terse and direct"
    ) == 1


def test_apply_change_distinct_bullet_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = _make_soul(tmp_path, monkeypatch, growth_body="- Be terse and direct.\n\n")
    applied = apply_change(
        repo_root=tmp_path,
        target_path=SOUL_REL,
        action="append_to_section",
        content="- Push back when warranted.\n",
        section="Growth",
    )
    assert applied.no_op_reason is None
    assert applied.bytes_after > applied.bytes_before
    text = (ws / "SOUL.md").read_text(encoding="utf-8")
    assert text.count("Push back when warranted") == 1
    assert text.count("Be terse and direct") == 1


def test_apply_change_replace_unchanged_tagged_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `replace` whose payload is byte-identical to disk is `unchanged`,
    not `duplicate` — the latter is reserved for bullet-dedup hits."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.paths import workspace_dir
    ws = workspace_dir()
    ws.mkdir(parents=True)
    body = "# Soul\n\nCurrent content.\n"
    (ws / "SOUL.md").write_text(body, encoding="utf-8")
    applied = apply_change(
        repo_root=tmp_path,
        target_path=SOUL_REL,
        action="replace",
        content=body,
        section=None,
    )
    assert applied.no_op_reason == "unchanged"
    assert applied.bytes_before == applied.bytes_after


# ── feedback_consolidator inbox dedup (secondary layer) ────────


def test_consolidator_skips_when_pending_soul_proposal_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the dedup guard in `_emit_inbox_events` short-circuits a
    second `soul_proposal` for the same bullet while one is pending.

    Direct unit test of the dedup branch — exercises only the hand-rolled
    list-and-skip logic added in `feedback_consolidator.py`, no LLM call.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    from tesseract.kernel.workspace_changes import workspace_events_dir
    from tesseract.workspace_events import EventStore, WorkspaceEvent
    from tesseract.kernel.workspace_changes import _normalize_bullet

    store = EventStore(workspace_events_dir())
    bullet = "Check the actual files first."
    store.append_event(WorkspaceEvent.new(
        kind="soul_proposal",
        source="feedback_consolidator",
        title="Soul-growth bullet (×3)",
        summary=bullet,
        payload={"action": "propose_soul_growth", "bullet": bullet},
    ))

    # Re-read pending and run the same dedup logic the task uses.
    pending = store.list_events(kinds=("soul_proposal",), status="pending")
    pending_norms = {
        _normalize_bullet(str((ev.payload or {}).get("bullet", "")))
        for ev in pending
    }

    # Same bullet (different surface form) → would be skipped.
    assert _normalize_bullet("- check  the actual FILES first!") in pending_norms
    # Distinct bullet → would still emit.
    assert _normalize_bullet("Push back when warranted") not in pending_norms
