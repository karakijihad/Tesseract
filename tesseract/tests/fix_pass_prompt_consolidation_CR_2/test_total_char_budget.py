"""CR-2: total assembled prompt has a soft ceiling.

When the prompt exceeds ``MAX_TOTAL_CHARS``, lower-priority blocks are
dropped in this order: diary digest, manifest pointers, older daily
capsule entries, non-active directives. The base identity sections
(IDENTITY / SOUL / USER / AGENTS / rules / now) are never dropped.
"""

from __future__ import annotations

from pathlib import Path

from tesseract.brain import prompt as prompt_module


def test_max_total_chars_constant_exists() -> None:
    assert isinstance(prompt_module.MAX_TOTAL_CHARS, int)
    assert prompt_module.MAX_TOTAL_CHARS > 0


def test_budget_dropper_returns_input_when_under_budget() -> None:
    short = "hello\n\nworld"
    out = prompt_module._apply_total_budget(short, diary="d", manifest="m")
    assert out == short


def test_budget_dropper_removes_diary_first_when_over() -> None:
    long_body = "X" * (prompt_module.MAX_TOTAL_CHARS - 100)
    diary = "DIARY-BLOCK " * 200  # ~2400 chars; clearly droppable
    manifest = "MANIFEST-BLOCK"
    over = long_body + "\n\n" + diary + "\n\n" + manifest
    out = prompt_module._apply_total_budget(
        over, diary=diary, manifest=manifest
    )
    assert "DIARY-BLOCK" not in out, "diary should be the first dropped block"
    # Body content must be preserved.
    assert long_body[:200] in out


def test_budget_dropper_keeps_body_even_when_oversized() -> None:
    """If the base sections alone exceed the budget, return them as-is —
    truncating identity/rules would be worse than overrunning the budget."""
    huge_body = "Y" * (prompt_module.MAX_TOTAL_CHARS + 1_000)
    out = prompt_module._apply_total_budget(huge_body, diary="", manifest="")
    # No silent truncation of body content.
    assert out.startswith("Y" * 100)


def test_budget_dropper_full_priority_order() -> None:
    """When far over budget, blocks drop in order: diary → manifest →
    directives → capsule. Earlier blocks dropped first; later blocks
    survive until needed."""
    base = "BASE" * (prompt_module.MAX_TOTAL_CHARS // 4 - 100)
    diary = "DIARY-BLOCK"
    manifest = "MANIFEST-BLOCK"
    directives = "DIRECTIVES-BLOCK"
    capsule = "CAPSULE-BLOCK"
    extra = "Z" * 6_000  # forces all four drops to fire
    over = "\n\n".join([base, diary, manifest, directives, capsule, extra])

    out = prompt_module._apply_total_budget(
        over,
        diary=diary,
        manifest=manifest,
        directives=directives,
        capsule=capsule,
    )
    # Every soft-drop block is gone.
    assert "DIARY-BLOCK" not in out
    assert "MANIFEST-BLOCK" not in out
    assert "DIRECTIVES-BLOCK" not in out
    assert "CAPSULE-BLOCK" not in out
    # Base sections survive.
    assert "BASE" in out


def test_budget_dropper_stops_at_first_block_that_fits() -> None:
    """Dropping diary alone is sufficient — manifest survives."""
    base = "B" * (prompt_module.MAX_TOTAL_CHARS - 3_000)
    diary = "X" * 5_000  # dropping diary alone gets us under budget
    manifest = "MANIFEST-SURVIVES"
    over = base + "\n\n" + diary + "\n\n" + manifest

    out = prompt_module._apply_total_budget(
        over, diary=diary, manifest=manifest,
        directives="UNUSED", capsule="UNUSED-CAPSULE",
    )
    assert diary not in out, "diary should be dropped"
    assert "MANIFEST-SURVIVES" in out, (
        "manifest must NOT be dropped when diary alone freed enough"
    )
