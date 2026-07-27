"""Kernel ``tick()`` — determinism, dedupe, admission policy, selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyEvent,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
)
from tesseract.orchestrator.autonomy.kernel import (
    REASON_AWAITING_OPERATOR,
    REASON_LANE_FULL,
    REASON_LANE_UNCONFIGURED,
    REASON_RISK_MISMATCH,
    REASON_SELECTED,
    REASON_TOTAL_CONCURRENCY_BLOCK,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane


def _emit_operator(kernel: AutonomyKernel, goal: str, **kwargs) -> AutonomyEvent:
    event = AutonomyEvent.make(
        AgendaSource.OPERATOR, {"goal": goal, **kwargs}
    )
    kernel.bus.publish_nowait(event)
    return event


@pytest.mark.asyncio
async def test_tick_creates_items_and_selects_top_k(
    kernel: AutonomyKernel,
) -> None:
    _emit_operator(kernel, "doe-research-1", operator_priority=5)
    _emit_operator(kernel, "doe-research-2", operator_priority=3)
    _emit_operator(kernel, "doe-research-3", operator_priority=1)
    _emit_operator(kernel, "doe-research-4", operator_priority=0)
    result = await kernel.tick()
    assert result.events_drained == 4
    assert result.items_created == 4
    assert len(result.selected) == 3  # top_k


@pytest.mark.asyncio
async def test_tick_idempotent_replay_dedupes(kernel: AutonomyKernel) -> None:
    event = AutonomyEvent.make(
        AgendaSource.OPERATOR, {"goal": "doe-thing"}
    )
    kernel.bus.publish_nowait(event)
    first = await kernel.tick()
    assert first.items_created == 1

    # Re-publish the same event (same event_id + same goal) — dedupe holds.
    kernel.bus.publish_nowait(event)
    second = await kernel.tick()
    assert second.items_created == 0
    assert second.items_deduped == 1


@pytest.mark.asyncio
async def test_tick_dedupes_by_source_event_id(kernel: AutonomyKernel) -> None:
    event = AutonomyEvent.make(
        AgendaSource.SELF_REFLECTION,
        {"observation": "doc drifted", "confidence": 0.9, "suggestions": ["x"]},
    )
    kernel.bus.publish_nowait(event)
    await kernel.tick()
    # Same self_reflection event id replayed in a later tick is deduped
    # even though the goal hashing might differ on minute boundaries.
    kernel.bus.publish_nowait(event)
    result = await kernel.tick()
    assert result.items_created == 0
    assert result.items_deduped == 1


@pytest.mark.asyncio
async def test_lane_cap_zero_rejects_propose_items(
    isolated_home: Path,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    # Cap the markdown_agent lane (default for PROPOSE) to zero.
    lane = WorkerLane(
        {
            WorkerKind.TARS_SELF: 10,
            WorkerKind.MARKDOWN_AGENT: 0,
            WorkerKind.CLAUDE_CLI: 10,
            WorkerKind.CODEX_CLI: 10,
            WorkerKind.TERMINAL: 10,
        }
    )
    kernel = AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
    )
    _emit_operator(kernel, "doe-propose-1")
    result = await kernel.tick()
    assert result.items_created == 1
    assert result.selected == []
    assert any(r["reason"] == REASON_LANE_FULL for r in result.rejections)


@pytest.mark.asyncio
async def test_risk_class_mismatch_admission_rejection(
    isolated_home: Path,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    # The autonomous-only kind ceiling is TARS_SELF=propose ceiling, so
    # an autonomous item routed to a kind with ceiling=autonomous would
    # admit; we exercise the opposite — kind with no ceiling (ABSENT)
    # surfaces unconfigured.
    lane = WorkerLane({})  # zero kinds configured at all
    kernel = AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
    )
    _emit_operator(kernel, "doe-no-lane")
    result = await kernel.tick()
    assert any(
        r["reason"] == REASON_LANE_UNCONFIGURED for r in result.rejections
    )


@pytest.mark.asyncio
async def test_governor_pause_skips_source(kernel: AutonomyKernel) -> None:
    _emit_operator(kernel, "doe-operator-paused")
    kernel.pause_source(AgendaSource.OPERATOR, reason="trust_degradation")
    result = await kernel.tick()
    # AU-6 — mappers short-circuit on paused sources so the event never
    # mints an item. Nothing reaches selection; the item is parked at
    # the mapper layer, not the rejection bucket.
    assert result.selected == []
    assert result.items_created == 0
    # An item created BEFORE the pause would still flow into rejections
    # under REASON_GOVERNOR_PAUSED (covered by the AU-6 integration test).


@pytest.mark.asyncio
async def test_governor_resume_allows_subsequent_admission(
    kernel: AutonomyKernel,
) -> None:
    kernel.pause_source(AgendaSource.OPERATOR)
    _emit_operator(kernel, "doe-paused-then-resumed")
    blocked = await kernel.tick()
    assert blocked.selected == []
    # Resume + emit another candidate; the resume-er can pick it up.
    kernel.resume_source(AgendaSource.OPERATOR)
    _emit_operator(kernel, "doe-after-resume")
    after = await kernel.tick()
    assert len(after.selected) == 1


@pytest.mark.asyncio
async def test_operator_priority_dominates_age(kernel: AutonomyKernel) -> None:
    _emit_operator(kernel, "doe-low", operator_priority=0)
    _emit_operator(kernel, "doe-high", operator_priority=5)
    result = await kernel.tick()
    assert len(result.selected) >= 1
    # The +5 priority item ranks first regardless of insertion order.
    first = result.selected[0]
    item = kernel._agenda.get(first)
    assert item is not None
    assert "doe-high" in item.goal


@pytest.mark.asyncio
async def test_disabled_mapper_drops_event(
    isolated_home: Path,
    permissive_lane,
) -> None:
    # No mapper configs at all — operator-source defaults to enabled,
    # everything else disabled. A self_reflection event therefore drops
    # silently.
    kernel = AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs={},
    )
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.SELF_REFLECTION,
            {"observation": "x", "confidence": 0.9, "suggestions": ["y"]},
        )
    )
    result = await kernel.tick()
    assert result.drafts_emitted == 0
    assert result.items_created == 0


@pytest.mark.asyncio
async def test_awaiting_operator_when_approvals_required(
    kernel: AutonomyKernel,
) -> None:
    # vault_signal mapper attaches operator_review approval — selection
    # transitions to awaiting_operator, not selected.
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.VAULT_SIGNAL,
            {"summary": "test_bar failed: boom", "kind": "test_failure_relic"},
        )
    )
    result = await kernel.tick()
    assert result.selected == []
    assert any(r["reason"] == REASON_AWAITING_OPERATOR for r in result.rejections)
    items = kernel._agenda.list_active()
    assert any(i.status == AgendaStatus.AWAITING_OPERATOR for i in items)


@pytest.mark.asyncio
async def test_selected_items_get_status_and_decision_stamped(
    kernel: AutonomyKernel,
) -> None:
    _emit_operator(kernel, "doe-stamped", operator_priority=5)
    result = await kernel.tick()
    assert len(result.selected) == 1
    item = kernel._agenda.get(result.selected[0])
    assert item is not None
    # S2 transitions all the way to RUNNING after the dispatch fires.
    # last_decision is populated either with rationale (if adapter
    # available) or the canonical SELECTED marker as fallback.
    assert item.status == AgendaStatus.RUNNING
    assert item.last_decision is not None
    assert item.linked_workers, "WorkerRecord id must be linked on dispatch"


@pytest.mark.asyncio
async def test_quiesce_pauses_selection_but_keeps_ingest(
    kernel: AutonomyKernel,
) -> None:
    kernel.quiesce()
    _emit_operator(kernel, "doe-quiesced")
    result = await kernel.tick()
    assert result.events_drained == 1
    assert result.items_created == 1
    assert result.paused is True
    assert result.selected == []
    # Resume — the next tick selects the existing item.
    kernel.resume()
    result2 = await kernel.tick()
    assert len(result2.selected) == 1


@pytest.mark.asyncio
async def test_daily_token_cap_pauses_selection(
    isolated_home: Path,
    permissive_lane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    store = AgendaStore()
    # Seed an active item that has already burned the token budget.
    _emit = AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-cap"})
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(
            top_k=3, daily_tokens_cap=100, daily_seconds_cap=0
        ),
        mapper_configs=all_mappers_enabled,
    )
    kernel.bus.publish_nowait(_emit)
    await kernel.tick()  # creates + selects
    # Mutate spent tokens past the cap. Real S2 dispatch would update
    # this; for S1 we simulate the post-dispatch state.
    items = store.list_active()
    assert items
    items[0].budget_tokens_spent = 999
    store.save(items[0])
    # Next tick should pause.
    kernel.bus.publish_nowait(
        AutonomyEvent.make(AgendaSource.OPERATOR, {"goal": "doe-cap-2"})
    )
    result = await kernel.tick()
    assert result.paused is True
    assert kernel.dispatch_paused is True
    assert kernel.dispatch_pause_reason is not None


@pytest.mark.asyncio
async def test_tick_promotes_stale_unvetted_item(kernel: AutonomyKernel) -> None:
    """Fix 1 — the UNVETTED staleness escape valve fires every tick, not
    just at kernel boot, so a vetter job disabled mid-run doesn't strand
    items in UNVETTED forever."""
    store = kernel._agenda  # type: ignore[attr-defined]
    stale_created = datetime.now(timezone.utc) - timedelta(hours=25)
    item = AgendaItem(
        id=mint_agenda_id(AgendaSource.SELF_REFLECTION),
        created_at=stale_created,
        updated_at=stale_created,
        source=AgendaSource.SELF_REFLECTION,
        goal="doe stale unvetted",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.UNVETTED,
    )
    store.save(item)

    await kernel.tick()

    refreshed = store.get(item.id)
    assert refreshed is not None
    # The item is no longer UNVETTED — it may have advanced further
    # within the same tick (selection picks up freshly-PROPOSED items),
    # so assert on the recorded transition rather than the final status.
    assert refreshed.status != AgendaStatus.UNVETTED
    reasons = [t.reason for t in refreshed.status_history]
    assert "vet_timeout" in reasons
