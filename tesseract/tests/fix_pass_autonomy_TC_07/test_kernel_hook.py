"""TC-7 — kernel-side hook verification.

`AutonomyKernel._reconcile_agenda_for_worker` must invoke
`FollowUpMapper.create_draft_if_actionable` only AFTER the `advice_only`
journal row lands, and must not propagate any mapper failure into the
reconcile path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.follow_up_mapper import (
    FollowUpConfig,
    FollowUpMapper,
)
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_kernel(
    *,
    config: FollowUpConfig | None = None,
    mapper: FollowUpMapper | None = None,
) -> AutonomyKernel:
    store = AgendaStore()
    return AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        follow_up_mapper=mapper
        or FollowUpMapper(store, config or FollowUpConfig()),
    )


def _make_done_advisor_record(
    *,
    summary: str,
    record_id: str = "wk-test-advisor",
    agenda_id: str = "ag-test-parent",
) -> WorkerRecord:
    now = _now()
    return WorkerRecord(
        id=record_id,
        kind=WorkerKind.CLAUDE_CLI,
        created_at=now,
        updated_at=now,
        agenda_item_id=agenda_id,
        risk_class=RiskClass.OPERATOR_GATE,
        role="advisor",
        prompt="parent prompt",
        status=WorkerStatus.DONE,
        summary=summary,
    )


def _add_parent_item(kernel: AutonomyKernel, item_id: str) -> AgendaItem:
    now = _now()
    item = AgendaItem(
        id=item_id,
        created_at=now,
        updated_at=now,
        source=AgendaSource.OPERATOR,
        goal="parent goal",
        risk_class=RiskClass.OPERATOR_GATE,
        status=AgendaStatus.RUNNING,
    )
    kernel._agenda.add(item, reason="test_seed")
    return item


# ── happy path ─────────────────────────────────────────────────────────


def test_reconcile_emits_advice_only_then_draft(isolated_home: Path) -> None:
    kernel = _build_kernel()
    _add_parent_item(kernel, "ag-test-parent")
    summary = "implement the auto follow-up mapper end to end" * 10
    record = _make_done_advisor_record(summary=summary)

    kernel._reconcile_agenda_for_worker(record)

    rows = operator_journal.read_recent(limit=20)
    kinds = [r["event_type"] for r in rows]
    assert "outcome" in kinds
    assert "advice_only" in kinds
    assert "follow_up_draft" in kinds
    # Order on disk is append: outcome → advice_only → follow_up_draft.
    # ``read_recent`` returns newest-first, so the draft should appear
    # at or before advice_only in the returned list.
    draft_idx = kinds.index("follow_up_draft")
    advice_idx = kinds.index("advice_only")
    outcome_idx = kinds.index("outcome")
    assert draft_idx < advice_idx < outcome_idx


def test_reconcile_skips_draft_when_summary_short(isolated_home: Path) -> None:
    kernel = _build_kernel()
    _add_parent_item(kernel, "ag-test-parent")
    record = _make_done_advisor_record(summary="too short")

    kernel._reconcile_agenda_for_worker(record)

    rows = operator_journal.read_recent(limit=20)
    kinds = [r["event_type"] for r in rows]
    assert "outcome" in kinds
    # ``advice_only`` still fires for any non-empty summary with zero
    # artifacts (kernel rule); the mapper's length floor only gates
    # ``follow_up_draft``.
    assert "advice_only" in kinds
    assert "follow_up_draft" not in kinds


def test_reconcile_skips_draft_when_mapper_disabled(
    isolated_home: Path,
) -> None:
    kernel = _build_kernel(config=FollowUpConfig(enabled=False))
    _add_parent_item(kernel, "ag-test-parent")
    summary = "implement the cache end to end please" * 10
    record = _make_done_advisor_record(summary=summary)

    kernel._reconcile_agenda_for_worker(record)

    rows = operator_journal.read_recent(limit=20)
    kinds = [r["event_type"] for r in rows]
    # advice_only still fires (DONE + summary + no artifacts).
    assert "advice_only" in kinds
    # …but the disabled mapper produced no draft.
    assert "follow_up_draft" not in kinds


def test_mapper_raise_does_not_poison_reconcile(isolated_home: Path) -> None:
    """If the mapper's gating helper raises, the reconcile path must
    still complete: parent item transitions to DONE."""

    class BrokenMapper(FollowUpMapper):
        def create_draft_if_actionable(self, record):  # type: ignore[override]
            raise RuntimeError("boom")

    store = AgendaStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        follow_up_mapper=BrokenMapper(store),
    )
    parent = _add_parent_item(kernel, "ag-test-parent")
    summary = "implement the cache end to end please" * 10
    record = _make_done_advisor_record(summary=summary)

    kernel._reconcile_agenda_for_worker(record)
    # Parent item transitioned to DONE despite the mapper raising.
    refreshed = store.get(parent.id)
    assert refreshed is not None
    assert refreshed.status is AgendaStatus.DONE


def test_routing_intent_operator_gate_risk_class(
    isolated_home: Path,
) -> None:
    """The draft must carry ``OPERATOR_GATE`` risk_class so when it is
    dispatched the kernel's `_kind_for_item` helper picks CLAUDE_CLI
    (controller routing is held back per audit-2 C-1 until the runner
    can dispatch through controller IPC), and so the worktree allocator
    treats it as a code-editing candidate.

    This is the linkage between the mapper and the exit criterion
    "accepted draft always has non-null worktree_path before dispatch":
    once OPERATOR_GATE + CLAUDE_CLI lines up at dispatch time,
    ``worktree.allocate_for_record`` mints the worktree.
    """

    from tesseract.orchestrator.workers.worktree import requires_worktree

    kernel = _build_kernel()
    _add_parent_item(kernel, "ag-test-parent")
    summary = "implement the cache end to end please" * 10
    record = _make_done_advisor_record(summary=summary)

    kernel._reconcile_agenda_for_worker(record)

    items = list(kernel._agenda.iter_active())
    drafts = [
        i for i in items
        if i.source is AgendaSource.SELF_REFLECTION and record.id in i.linked_workers
    ]
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.risk_class is RiskClass.OPERATOR_GATE
    # Worktree allocation would fire for this risk_class + CLAUDE_CLI.
    assert requires_worktree(draft.risk_class, WorkerKind.CLAUDE_CLI) is True
