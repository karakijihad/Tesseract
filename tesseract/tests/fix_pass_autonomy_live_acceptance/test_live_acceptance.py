"""Autonomy live-acceptance smoke — five scenarios covering the self-reflection
→ mapper → agenda → worker → reconcile chain (mcp-control-plane P5 Workstream B;
closes ``Docs/Deferred.md §Autonomy live-acceptance``).

Each scenario drives the REAL kernel/mappers/reconciler (no stubs beyond the
worker runner) and asserts an observable agenda outcome. All state lands in the
per-test ``TESSERACT_HOME`` (see ``conftest.isolated_home``).

Scenarios:
  1. Self-reflection event → agenda item created → worker DONE → item DONE.
  2. Docs/vault delta → operator-review agenda item (AWAITING_OPERATOR).
  3. Worker FAILED → linked agenda item BLOCKED with a reason.
  4. Operator-view bare repeat-switch → suppressed (no agenda item).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import WorkerRecord, WorkerStatus


def _worker(item_id: str, *, status: WorkerStatus) -> WorkerRecord:
    moment = datetime.now(timezone.utc)
    return WorkerRecord(
        id=f"wk-live-{status.value}",
        kind=WorkerKind.TARS_SELF,
        created_at=moment,
        updated_at=moment,
        agenda_item_id=item_id,
        risk_class=RiskClass.AUTONOMOUS,
        role="",
        prompt="test",
        status=status,
    )


# ── Scenario 1 — self-reflection → agenda item → DONE ────────────────────

@pytest.mark.asyncio
async def test_self_reflection_creates_item_that_can_reach_done(kernel) -> None:
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.SELF_REFLECTION,
            {"observation": "no goal completions in the last hour; consider a retry"},
        )
    )
    result = await kernel.tick()
    assert result.events_drained == 1
    assert result.items_created == 1

    item = kernel._agenda.list_active()[0]
    assert item.source == AgendaSource.SELF_REFLECTION

    # Drive the worker to DONE and reconcile — the item closes DONE.
    kernel._agenda.transition(item, AgendaStatus.RUNNING, reason="smoke", by="test")
    item.linked_workers = ["wk-live-done"]
    kernel._agenda.save(item)
    kernel._reconcile_agenda_for_worker(_worker(item.id, status=WorkerStatus.DONE))

    assert kernel._agenda.get(item.id).status == AgendaStatus.DONE


# ── Scenario 2 — vault delta → operator-review item ──────────────────────

@pytest.mark.asyncio
async def test_vault_signal_creates_operator_review_item(kernel) -> None:
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.VAULT_SIGNAL,
            {
                "kind": "contradiction",
                "summary": "vault doc contradicts a stored memory",
                "vault_path": "sources/example.md",
                "risk_class": "propose",
            },
        )
    )
    result = await kernel.tick()
    assert result.items_created == 1

    item = kernel._agenda.list_active()[0]
    assert item.source == AgendaSource.VAULT_SIGNAL
    assert item.approvals_required
    gate = item.approvals_required[0]
    assert gate.kind == "operator_review"
    assert gate.target.startswith("vault:")
    # An approval-gated item is held for the operator, not auto-run.
    assert item.status == AgendaStatus.AWAITING_OPERATOR
    assert any(r.get("reason") == "awaiting_operator_approval" for r in result.rejections)


# ── Scenario 3 — worker FAILED → agenda BLOCKED ──────────────────────────

@pytest.mark.asyncio
async def test_worker_failed_blocks_agenda_item(kernel) -> None:
    item = AgendaItem(
        id=mint_agenda_id(AgendaSource.PROVIDER_WATCH),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        source=AgendaSource.PROVIDER_WATCH,
        goal="do a thing",
        risk_class=RiskClass.AUTONOMOUS,
        status=AgendaStatus.RUNNING,
        linked_workers=["wk-live-failed"],
    )
    kernel._agenda.save(item)

    kernel._reconcile_agenda_for_worker(_worker(item.id, status=WorkerStatus.FAILED))

    refreshed = kernel._agenda.get(item.id)
    assert refreshed.status == AgendaStatus.BLOCKED
    assert refreshed.blocked_reason == "worker_failed:wk-live-failed"


# ── Scenario 4 — operator-view repeat-switch suppressed ──────────────────

@pytest.mark.asyncio
async def test_operator_view_bare_repeat_switch_suppressed(kernel) -> None:
    for count in (5, 6):  # two repeats — neither should create an item
        kernel.bus.publish_nowait(
            AutonomyEvent.make(
                AgendaSource.OPERATOR_VIEW,
                {"view": "chat", "repeat_switch": True, "switch_count_today": count},
            )
        )
        result = await kernel.tick()
        assert result.events_drained == 1
        assert result.items_created == 0
        assert result.items_deduped == 0  # suppressed at the mapper, never reaches dedupe
    assert kernel._agenda.list_active() == []
