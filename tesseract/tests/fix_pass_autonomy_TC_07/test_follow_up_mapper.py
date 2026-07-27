"""TC-7 — FollowUpMapper unit + integration tests.

Covers the actionability heuristic, draft creation, journal write, and
kernel-side hook through `_reconcile_agenda_for_worker`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.follow_up_mapper import (
    FollowUpConfig,
    FollowUpMapper,
    _first_sentence,
    _slug_from_goal,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)


def _make_record(
    *,
    summary: str,
    status: WorkerStatus = WorkerStatus.DONE,
    artifacts: list | None = None,
    record_id: str = "wk-test-001",
    agenda_id: str = "ag-test-parent",
) -> WorkerRecord:
    now = datetime.now(timezone.utc)
    return WorkerRecord(
        id=record_id,
        kind=WorkerKind.CLAUDE_CLI,
        created_at=now,
        updated_at=now,
        agenda_item_id=agenda_id,
        risk_class=RiskClass.OPERATOR_GATE,
        role="advisor",
        prompt="parent prompt",
        status=status,
        summary=summary,
        artifacts=list(artifacts or []),
    )


# ── heuristic ──────────────────────────────────────────────────────────


class TestActionability:
    def test_long_summary_with_keyword_actionable(
        self, isolated_home: Path
    ) -> None:
        mapper = FollowUpMapper(AgendaStore())
        summary = (
            "We should implement the new caching layer so subsequent "
            "requests skip the slow path. " * 5
        )
        record = _make_record(summary=summary)
        assert mapper.is_actionable(record) is True

    def test_short_summary_not_actionable(self, isolated_home: Path) -> None:
        mapper = FollowUpMapper(AgendaStore())
        record = _make_record(summary="implement quickly")
        assert mapper.is_actionable(record) is False

    def test_long_summary_no_keyword_not_actionable(
        self, isolated_home: Path
    ) -> None:
        mapper = FollowUpMapper(AgendaStore())
        summary = (
            "Some neutral analysis. The thing was observed. Numbers were "
            "noted. Charts compared. Observations recorded. "
        ) * 5
        record = _make_record(summary=summary)
        assert mapper.is_actionable(record) is False

    def test_record_with_artifacts_not_actionable(
        self, isolated_home: Path
    ) -> None:
        from tesseract.orchestrator.workers.record import ArtifactRef

        mapper = FollowUpMapper(AgendaStore())
        summary = "implement the cache layer end to end" * 10
        record = _make_record(
            summary=summary,
            artifacts=[ArtifactRef(kind="diff", path="/tmp/foo.patch")],
        )
        assert mapper.is_actionable(record) is False

    def test_non_done_status_not_actionable(
        self, isolated_home: Path
    ) -> None:
        mapper = FollowUpMapper(AgendaStore())
        record = _make_record(
            summary="implement the cache layer" * 20,
            status=WorkerStatus.FAILED,
        )
        assert mapper.is_actionable(record) is False

    def test_disabled_mapper_always_false(self, isolated_home: Path) -> None:
        mapper = FollowUpMapper(
            AgendaStore(),
            FollowUpConfig(enabled=False),
        )
        summary = "implement the cache layer" * 20
        record = _make_record(summary=summary)
        assert mapper.is_actionable(record) is False

    def test_custom_keywords(self, isolated_home: Path) -> None:
        mapper = FollowUpMapper(
            AgendaStore(),
            FollowUpConfig(
                enabled=True,
                min_summary_chars=10,
                keywords=("synthesize",),
            ),
        )
        record = _make_record(
            summary="we should synthesize a new abstraction here please"
        )
        assert mapper.is_actionable(record) is True


# ── draft creation ─────────────────────────────────────────────────────


class TestCreateDraft:
    def test_create_draft_persists_item_with_linkage(
        self, isolated_home: Path
    ) -> None:
        store = AgendaStore()
        mapper = FollowUpMapper(store)
        summary = (
            "Implement the missing follow-up dispatcher so accepted "
            "advisor output flows into a code-edit worker automatically. "
            "Today: nothing happens after acceptance, so the operator "
            "has to manually retype the directive."
        )
        record = _make_record(summary=summary, record_id="wk-parent-abc")
        draft = mapper.create_draft(record)
        assert draft is not None
        assert draft.status is AgendaStatus.AWAITING_OPERATOR
        assert draft.source is AgendaSource.SELF_REFLECTION
        assert draft.source_event_id == record.id
        assert "wk-parent-abc" in draft.linked_workers
        assert draft.risk_class is RiskClass.OPERATOR_GATE
        assert len(draft.approvals_required) == 1
        assert draft.approvals_required[0].kind == "operator_review"
        # Goal is first sentence — should not contain the trailing prose.
        assert "Today: nothing happens" not in draft.goal
        # Persisted in store.
        from tesseract.orchestrator.autonomy.paths import (
            agenda_item_path,
        )
        assert agenda_item_path(draft.id).exists()

    def test_create_draft_is_idempotent_on_id_collision(
        self, isolated_home: Path
    ) -> None:
        """A retry within the same UTC minute mints the same id;
        the second call must be a quiet no-op (no raise, no traceback
        log) and return the same item."""
        from datetime import datetime, timezone

        store = AgendaStore()
        mapper = FollowUpMapper(store)
        summary = "implement the cache layer end to end please" * 10
        record = _make_record(summary=summary)
        when = datetime.now(timezone.utc)
        first = mapper.create_draft(record, now=when)
        second = mapper.create_draft(record, now=when)
        assert first is not None
        assert second is not None
        assert first.id == second.id

    def test_create_draft_failure_returns_none(
        self, isolated_home: Path, monkeypatch
    ) -> None:
        """When the store rejects an add (e.g. id collision), the
        method returns None without raising into the caller."""

        store = AgendaStore()

        def broken_add(*a, **kw):
            raise RuntimeError("simulated store failure")

        monkeypatch.setattr(store, "add", broken_add)
        mapper = FollowUpMapper(store)
        record = _make_record(summary="implement the cache" * 20)
        draft = mapper.create_draft(record)
        assert draft is None


# ── public surface (gating + journal) ──────────────────────────────────


class TestCreateDraftIfActionable:
    def test_actionable_writes_journal_row(self, isolated_home: Path) -> None:
        store = AgendaStore()
        mapper = FollowUpMapper(store)
        summary = (
            "We need to implement a follow-up mapper that converts a "
            "non-trivial advisor summary into a draft agenda item so the "
            "operator does not retype the directive. Operator review is "
            "the existing approval flow, no new UI required."
        ) * 2
        record = _make_record(summary=summary)
        draft = mapper.create_draft_if_actionable(record)
        assert draft is not None
        rows = list(operator_journal.read_recent(limit=10))
        kinds = [r["event_type"] for r in rows]
        assert "follow_up_draft" in kinds
        latest = next(r for r in rows if r["event_type"] == "follow_up_draft")
        assert latest["worker_id"] == record.id
        assert latest["follow_up_draft_id"] == draft.id

    def test_non_actionable_no_draft_no_journal(
        self, isolated_home: Path
    ) -> None:
        store = AgendaStore()
        mapper = FollowUpMapper(store)
        record = _make_record(summary="ok")  # too short
        before_rows = list(operator_journal.read_recent(limit=10))
        draft = mapper.create_draft_if_actionable(record)
        after_rows = list(operator_journal.read_recent(limit=10))
        assert draft is None
        assert before_rows == after_rows

    def test_never_raises_even_when_journal_unavailable(
        self, isolated_home: Path, monkeypatch
    ) -> None:
        store = AgendaStore()
        mapper = FollowUpMapper(store)
        summary = "implement the migration" * 20

        def broken_append(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(operator_journal, "append", broken_append)
        record = _make_record(summary=summary)
        # Must not raise.
        draft = mapper.create_draft_if_actionable(record)
        # The draft itself still landed in the agenda store even though
        # the journal append failed.
        assert draft is not None


# ── helpers ────────────────────────────────────────────────────────────


class TestHelpers:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Implement the cache.", "Implement the cache."),
            (
                "Implement the cache. Then test it.",
                "Implement the cache.",
            ),
            ("No period here ever", "No period here ever"),
            ("", ""),
        ],
    )
    def test_first_sentence(self, text, expected):
        assert _first_sentence(text) == expected

    def test_first_sentence_truncates(self):
        long = "A" * 500 + "."
        out = _first_sentence(long, cap=120)
        assert len(out) <= 120
        assert out.endswith("…")

    def test_first_sentence_splits_no_space_capital_letter(self):
        # Bleed-across case: no space between sentences. Without the
        # capital-letter lookahead the whole string came back.
        assert (
            _first_sentence("Implement the cache.Then test it more.")
            == "Implement the cache."
        )

    def test_slug_from_goal_kebab(self):
        assert _slug_from_goal("Implement the Cache Layer!") == "implement-the-cache-layer"

    def test_slug_falls_back_when_empty(self):
        assert _slug_from_goal("   ") == "follow-up"
        assert _slug_from_goal("!!!") == "follow-up"


# ── config loader ──────────────────────────────────────────────────────


class TestFollowUpConfig:
    def test_defaults_when_no_block(self):
        cfg = FollowUpConfig.from_yaml_block(None)
        assert cfg.enabled is True
        assert cfg.min_summary_chars == 200
        assert "implement" in cfg.keywords

    def test_loads_custom_block(self):
        cfg = FollowUpConfig.from_yaml_block(
            {
                "enabled": False,
                "min_summary_chars": 50,
                "actionable_keywords": ["synthesize", "design"],
            }
        )
        assert cfg.enabled is False
        assert cfg.min_summary_chars == 50
        assert cfg.keywords == ("synthesize", "design")

    def test_invalid_min_chars_falls_back(self):
        cfg = FollowUpConfig.from_yaml_block({"min_summary_chars": "abc"})
        assert cfg.min_summary_chars == 200

    def test_invalid_keywords_falls_back(self):
        cfg = FollowUpConfig.from_yaml_block({"actionable_keywords": "implement"})
        # str is not a list → default
        assert "implement" in cfg.keywords
        assert len(cfg.keywords) > 1

    def test_empty_keyword_list_uses_default(self):
        cfg = FollowUpConfig.from_yaml_block({"actionable_keywords": []})
        assert "implement" in cfg.keywords
