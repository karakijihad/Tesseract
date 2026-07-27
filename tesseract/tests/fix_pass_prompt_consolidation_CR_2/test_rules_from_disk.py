"""CR-2: prompt-builder rules load from `tesseract/brain/rules/*.md`.

The 13 hardcoded English constants in `tesseract/brain/prompt.py`
(``_ALIVE_NUDGE_TEXT`` etc.) moved into one numbered markdown file per
rule. Numbered prefix preserves assembly order; missing files are
skipped silently (graceful degradation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.brain import prompt as prompt_module


def test_rules_dir_exists_and_is_non_empty() -> None:
    rules_dir = prompt_module.RULES_DIR
    assert rules_dir.exists(), f"rules dir missing: {rules_dir}"
    files = sorted(rules_dir.glob("*.md"))
    assert len(files) >= 15, (
        f"expected at least 15 numbered rule files, got {len(files)}: "
        f"{[f.name for f in files]}"
    )


def test_rules_load_in_numeric_order() -> None:
    blobs = prompt_module._load_rules(prompt_module.RULES_DIR)
    assert isinstance(blobs, list)
    # First rule is the alive-nudge; last is the output-contract.
    assert "Interaction style" in blobs[0]
    assert "Output contract" in blobs[-1]


def test_missing_rules_dir_does_not_raise(tmp_path: Path) -> None:
    """Graceful degradation — if the dir is absent, return empty list."""
    blobs = prompt_module._load_rules(tmp_path / "nonexistent")
    assert blobs == []


def test_individual_rule_files_track_canonical_content() -> None:
    """Every required H1 / H2 must appear in at least one rule file —
    catches a rename that drops content silently."""
    required_headers = [
        "# Interaction style",
        "# Tool use",
        "# Capability gap",
        "# Operator-visible task checklist",
        "# Parallel delegation",
        "# Workspace thread isolation",
        "# Reflect on directives",
        "# Time awareness",
        "## Audit-loop routing",
        "# Error recovery",
        "# Vault reflex",
        "# Orb state",
        "# Multimodal body",
        "# Output contract",
    ]
    blobs = prompt_module._load_rules(prompt_module.RULES_DIR)
    joined = "\n".join(blobs)
    for header in required_headers:
        assert header in joined, f"missing rule content: {header!r}"


def test_assemble_system_prompt_includes_rules_block(tmp_path: Path) -> None:
    """The assembled prompt must still contain every rule's H1/H2
    after migration. Regression guard: nothing was deleted in the move."""
    rendered = prompt_module.assemble_system_prompt(mode="manifest")
    for header in (
        "# Interaction style",
        "# Tool use",
        "# Output contract",
    ):
        assert header in rendered, f"assembled prompt missing rule: {header!r}"
