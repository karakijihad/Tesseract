"""Shared text-quality predicates — fragment detection + actionable-goal
extraction. Directly covers `tesseract.orchestrator.autonomy.text_quality`,
the single source of truth consumed by `follow_up_mapper.py` and the
kernel admission gate."""

from __future__ import annotations

import pytest

from tesseract.orchestrator.autonomy.follow_up_mapper import _DEFAULT_KEYWORDS
from tesseract.orchestrator.autonomy.text_quality import (
    actionable_goal,
    first_sentence,
    is_degenerate_goal,
    looks_like_fragment,
)


class TestLooksLikeFragment:
    def test_symbol_only_is_fragment(self):
        assert looks_like_fragment("}") is True

    def test_actionable_sentence_is_not_fragment(self):
        assert looks_like_fragment("Implement the cache layer.") is False


class TestActionableGoal:
    def test_extracts_directive_from_numbered_summary(self):
        summary = (
            "2. **Update memory-type alignment** - Ensure the MemoryType "
            "enumeration matches validate_memory_path. "
            "3. **Enable and test auto-proposal** - Run a completed mission."
        )
        assert actionable_goal(summary, _DEFAULT_KEYWORDS).startswith(
            "Update memory-type alignment"
        )

    @pytest.mark.parametrize(
        "summary",
        [
            "`). Move the tagged code, define __all__, and update exports.",
            "s). Create responsibility modules and run the test suite.",
            "and identify natural responsibility clusters. Create modules.",
        ],
    )
    def test_rejects_truncated_fragments(self, summary):
        assert actionable_goal(summary, _DEFAULT_KEYWORDS) == ""


class TestFirstSentence:
    def test_splits_first_sentence(self):
        assert first_sentence("Implement the cache. Then test it.") == "Implement the cache."


class TestIsDegenerateGoal:
    def test_symbol_only_is_degenerate(self):
        assert is_degenerate_goal("}") is True

    def test_whitespace_only_is_degenerate(self):
        assert is_degenerate_goal("  ") is True

    def test_heartbeat_mapper_goal_is_not_degenerate(self):
        assert is_degenerate_goal(
            "act on heartbeat observation: consolidate memory"
        ) is False

    def test_resolve_flake_goal_is_not_degenerate(self):
        assert is_degenerate_goal(
            "resolve flake in tests/foo: timeout"
        ) is False

    def test_implement_cache_is_not_degenerate(self):
        assert is_degenerate_goal("Implement cache.") is False
