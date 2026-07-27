"""Loop / cost-spiral / trust detector behaviours."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tesseract.orchestrator.autonomy import AgendaStore, PauseStore
from tesseract.orchestrator.autonomy.governor import (
    DETECTOR_COST_SPIRAL,
    DETECTOR_LOOP,
    DETECTOR_TRUST_DEGRADATION,
    Governor,
    GovernorConfig,
    REASON_COST_SPIRAL,
    REASON_LOOP_DETECTED,
    REASON_TRUST_DEGRADED,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
    RiskClass,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    RiskClass as WorkerRiskClass,
    WorkerRecord,
    WorkerStatus,
    load_record,
    mint_worker_id,
    write_record,
)

from .conftest import make_item


# ----- Loop detector ----------------------------------------------------


@pytest.mark.asyncio
async def test_loop_detector_fires_at_threshold(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    for _ in range(3):
        make_item(store, goal="docs drift on observer suggestion", source=AgendaSource.SELF_REFLECTION)

    result = await governor.run_once()
    pauses = [p.source for p in result.pauses_added]
    assert AgendaSource.SELF_REFLECTION in pauses
    pause = pause_store.get(AgendaSource.SELF_REFLECTION)
    assert pause is not None
    assert pause.detector == DETECTOR_LOOP
    assert pause.reason == REASON_LOOP_DETECTED
    assert pause.evidence["count"] >= 3


@pytest.mark.asyncio
async def test_loop_detector_ignores_under_threshold(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    # Two repeats — below the N=3 floor.
    for _ in range(2):
        make_item(store, goal="under threshold", source=AgendaSource.SELF_REFLECTION)

    result = await governor.run_once()
    assert result.pauses_added == []
    assert not pause_store.is_paused(AgendaSource.SELF_REFLECTION)


@pytest.mark.asyncio
async def test_loop_detector_window_excludes_old_items(
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    fixed_clock = datetime.now(timezone.utc)
    gov = Governor(
        agenda_store=store,
        pause_store=pause_store,
        config=GovernorConfig(loop_n=3, loop_window_hours=24),
        clock=lambda: fixed_clock,
    )
    for _ in range(3):
        item = make_item(
            store,
            goal="ancient repeat",
            source=AgendaSource.SELF_REFLECTION,
            now=old,
        )
        # Force updated_at back to the seed time so the window filter excludes it.
        item.updated_at = old
        store.save(item)

    result = await gov.run_once()
    assert result.pauses_added == []
    assert not pause_store.is_paused(AgendaSource.SELF_REFLECTION)


@pytest.mark.asyncio
async def test_loop_detector_counts_archived_repeats(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    """The store dedupes new admissions against active/ only — when the
    operator keeps cancelling the same goal, each repeat archives out and
    a detector reading only active/ would never see the loop. The
    governor walks active + archive so the cycle surfaces."""
    for _ in range(3):
        item = make_item(store, goal="cycling cancel goal", source=AgendaSource.SELF_REFLECTION)
        # Cancel → archive so each subsequent admission isn't dedupe'd
        # against an active duplicate.
        store.transition(item, AgendaStatus.CANCELLED, reason="seed_cancel")

    result = await governor.run_once()
    assert any(p.source == AgendaSource.SELF_REFLECTION for p in result.pauses_added)
    assert pause_store.is_paused(AgendaSource.SELF_REFLECTION)


@pytest.mark.asyncio
async def test_loop_detector_does_not_re_pause(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    for _ in range(3):
        make_item(store, goal="already paused observer", source=AgendaSource.SELF_REFLECTION)
    first = await governor.run_once()
    assert len(first.pauses_added) == 1
    second = await governor.run_once()
    assert second.pauses_added == []


# ----- Cost spiral detector --------------------------------------------


def _seed_running_worker(
    item_id: str,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_seconds: float = 0.0,
) -> WorkerRecord:
    rec = WorkerRecord(
        id=mint_worker_id(WorkerKind.MARKDOWN_AGENT),
        kind=WorkerKind.MARKDOWN_AGENT,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        agenda_item_id=item_id,
        risk_class=WorkerRiskClass.PROPOSE,
        role="agents_default",
        prompt="seeded",
        status=WorkerStatus.RUNNING,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_seconds=duration_seconds,
    )
    write_record(rec)
    return rec


@pytest.mark.asyncio
async def test_cost_spiral_cancels_worker_and_blocks_item(
    governor: Governor,
    store: AgendaStore,
) -> None:
    item = make_item(
        store,
        goal="costly proposal",
        source=AgendaSource.SELF_REFLECTION,
        budget_tokens_cap=1000,
    )
    record = _seed_running_worker(item.id, tokens_in=1500, tokens_out=1200)

    result = await governor.run_once()
    assert record.id in result.workers_cancelled
    assert item.id in result.items_blocked
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.BLOCKED
    assert refreshed.blocked_reason == REASON_COST_SPIRAL
    refreshed_worker = load_record(record.id)
    assert refreshed_worker is not None
    assert refreshed_worker.status == WorkerStatus.CANCELLED


@pytest.mark.asyncio
async def test_cost_spiral_under_threshold_no_op(
    governor: Governor,
    store: AgendaStore,
) -> None:
    item = make_item(
        store,
        goal="within budget",
        source=AgendaSource.SELF_REFLECTION,
        budget_tokens_cap=1000,
    )
    # 500 + 500 = 1000 < 2× cap (= 2000).
    record = _seed_running_worker(item.id, tokens_in=500, tokens_out=500)

    result = await governor.run_once()
    assert result.workers_cancelled == []
    assert result.items_blocked == []
    refreshed = load_record(record.id)
    assert refreshed is not None
    assert refreshed.status == WorkerStatus.RUNNING


@pytest.mark.asyncio
async def test_cost_spiral_seconds_dimension(
    governor: Governor,
    store: AgendaStore,
) -> None:
    item = make_item(
        store,
        goal="time runaway",
        source=AgendaSource.SELF_REFLECTION,
        budget_seconds_cap=60,
    )
    record = _seed_running_worker(item.id, duration_seconds=180.0)
    result = await governor.run_once()
    assert record.id in result.workers_cancelled


# ----- Trust degradation -----------------------------------------------


def _make_rejected_item(
    store: AgendaStore,
    *,
    goal: str,
    source: AgendaSource = AgendaSource.SELF_REFLECTION,
) -> None:
    """Build an item that walked through awaiting_operator → cancelled."""
    item = make_item(store, goal=goal, source=source)
    store.transition(item, AgendaStatus.AWAITING_OPERATOR, reason="seed_gate")
    store.transition(item, AgendaStatus.CANCELLED, reason="operator_reject", by="operator")


@pytest.mark.asyncio
async def test_trust_degradation_fires_after_n_rejections(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    for i in range(3):
        _make_rejected_item(store, goal=f"observer reject {i}")

    result = await governor.run_once()
    pauses = [p.source for p in result.pauses_added]
    assert AgendaSource.SELF_REFLECTION in pauses
    pause = pause_store.get(AgendaSource.SELF_REFLECTION)
    assert pause is not None
    assert pause.detector == DETECTOR_TRUST_DEGRADATION
    assert pause.reason == REASON_TRUST_DEGRADED


@pytest.mark.asyncio
async def test_trust_degradation_below_threshold_no_op(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    for i in range(2):
        _make_rejected_item(store, goal=f"observer reject {i}")
    result = await governor.run_once()
    assert result.pauses_added == []
    assert not pause_store.is_paused(AgendaSource.SELF_REFLECTION)


@pytest.mark.asyncio
async def test_trust_ignores_non_awaiting_operator_terminals(
    governor: Governor,
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    # Three cancellations but none went through awaiting_operator — kernel
    # cancellations, not operator rejections. Should not trigger.
    for i in range(3):
        item = make_item(store, goal=f"kernel cancel {i}", source=AgendaSource.SELF_REFLECTION)
        store.transition(item, AgendaStatus.CANCELLED, reason="kernel_cancel")

    result = await governor.run_once()
    assert result.pauses_added == []
    assert not pause_store.is_paused(AgendaSource.SELF_REFLECTION)


# ----- Notification hook -----------------------------------------------


@pytest.mark.asyncio
async def test_notify_fn_fires_on_new_pause(
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    calls: list[str] = []

    async def notify(pause) -> None:
        calls.append(pause.source.value)

    gov = Governor(
        agenda_store=store,
        pause_store=pause_store,
        notify_fn=notify,
        config=GovernorConfig(loop_n=3),
    )
    for _ in range(3):
        make_item(store, goal="notify me", source=AgendaSource.SELF_REFLECTION)

    await gov.run_once()
    # Notification fires in a task — yield once so it resolves.
    await asyncio.sleep(0)
    assert calls == ["self_reflection"]


@pytest.mark.asyncio
async def test_notify_fn_not_fired_on_idempotent_pause(
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    """A second tick after pause already exists must not re-notify."""
    calls: list[str] = []

    async def notify(pause) -> None:
        calls.append(pause.source.value)

    gov = Governor(
        agenda_store=store,
        pause_store=pause_store,
        notify_fn=notify,
        config=GovernorConfig(loop_n=3),
    )
    for _ in range(3):
        make_item(store, goal="re-fire?", source=AgendaSource.SELF_REFLECTION)
    await gov.run_once()
    await asyncio.sleep(0)
    assert len(calls) == 1
    # Second pass — pause already in place; no new notification.
    await gov.run_once()
    await asyncio.sleep(0)
    assert len(calls) == 1


# ----- Kernel hook ------------------------------------------------------


@pytest.mark.asyncio
async def test_kernel_pause_hook_called_on_new_pause(
    store: AgendaStore,
    pause_store: PauseStore,
) -> None:
    hook_calls: list[str] = []
    gov = Governor(
        agenda_store=store,
        pause_store=pause_store,
        kernel_pause_hook=lambda src, reason: hook_calls.append(src.value),
        config=GovernorConfig(loop_n=3),
    )
    for _ in range(3):
        make_item(store, goal="hook me", source=AgendaSource.SELF_REFLECTION)
    await gov.run_once()
    assert hook_calls == ["self_reflection"]
