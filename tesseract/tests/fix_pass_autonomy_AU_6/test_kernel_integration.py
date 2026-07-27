"""Kernel + PauseStore integration — boot reload, mapper short-circuit, persistence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy import (
    AgendaStore,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
    PauseStore,
)
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
from tesseract.orchestrator.autonomy.governor import (
    DETECTOR_LOOP,
    REASON_LOOP_DETECTED,
)
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.lane import WorkerLane


@pytest.fixture
def permissive_lane() -> WorkerLane:
    return WorkerLane(
        {
            WorkerKind.TARS_SELF: 10,
            WorkerKind.MARKDOWN_AGENT: 10,
            WorkerKind.CLAUDE_CLI: 10,
            WorkerKind.CODEX_CLI: 10,
            WorkerKind.TERMINAL: 10,
        }
    )


@pytest.fixture
def all_mappers_enabled() -> dict[AgendaSource, MapperConfig]:
    return {
        source: MapperConfig(
            enabled=True,
            source=source,
            default_risk_class=RiskClass.PROPOSE,
            dedupe_window_hours=24,
        )
        for source in (
            AgendaSource.OPERATOR,
            AgendaSource.PROVIDER_WATCH,
            AgendaSource.SELF_REFLECTION,
        )
    }


def _emit_self_reflection(kernel: AutonomyKernel, slug: str) -> None:
    kernel._bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.SELF_REFLECTION,
            payload={
                "observation": f"self_reflection drift in {slug}",
                "source_event_id": f"evt-{slug}",
                "suggested_risk_class": RiskClass.PROPOSE.value,
            },
        )
    )


@pytest.mark.asyncio
async def test_kernel_reloads_pauses_on_init(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    """A pause persisted before kernel boot is honoured by the next kernel.
    This is the recovery-across-restart guarantee."""
    store = AgendaStore()
    pause_store = PauseStore()
    pause_store.add(
        AgendaSource.SELF_REFLECTION,
        detector=DETECTOR_LOOP,
        reason=REASON_LOOP_DETECTED,
    )

    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
        pause_store=pause_store,
    )
    assert kernel.is_source_paused(AgendaSource.SELF_REFLECTION)


@pytest.mark.asyncio
async def test_kernel_pause_source_writes_to_store(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    store = AgendaStore()
    pause_store = PauseStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
        pause_store=pause_store,
    )
    kernel.pause_source(AgendaSource.PROVIDER_WATCH, reason="manual pause")
    # PauseStore now reflects the pause.
    assert pause_store.is_paused(AgendaSource.PROVIDER_WATCH)

    # A fresh PauseStore (simulating a restart) reads the same state.
    fresh = PauseStore()
    assert fresh.is_paused(AgendaSource.PROVIDER_WATCH)


@pytest.mark.asyncio
async def test_kernel_resume_source_clears_store(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    store = AgendaStore()
    pause_store = PauseStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
        pause_store=pause_store,
    )
    kernel.pause_source(AgendaSource.PROVIDER_WATCH, reason="oops")
    kernel.resume_source(AgendaSource.PROVIDER_WATCH, by="operator")

    assert not pause_store.is_paused(AgendaSource.PROVIDER_WATCH)
    fresh = PauseStore()
    assert not fresh.is_paused(AgendaSource.PROVIDER_WATCH)


@pytest.mark.asyncio
async def test_mapper_short_circuits_paused_source(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    """A self_reflection event arriving after pause MUST NOT mint an item."""
    store = AgendaStore()
    pause_store = PauseStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
        pause_store=pause_store,
    )
    kernel.pause_source(AgendaSource.SELF_REFLECTION, reason="loop_detected")
    _emit_self_reflection(kernel, "post-pause")
    result = await kernel.tick()
    assert result.items_created == 0
    assert result.drafts_emitted == 0
    assert store.list_active() == []


@pytest.mark.asyncio
async def test_existing_proposed_item_blocks_on_pause(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    """An item already PROPOSED before the source pauses still routes
    through ``REASON_GOVERNOR_PAUSED`` at selection time (the mapper
    short-circuit only protects against new events post-pause)."""
    from datetime import datetime, timezone

    from tesseract.orchestrator.autonomy.kernel import REASON_GOVERNOR_PAUSED
    from tesseract.orchestrator.autonomy.models import (
        AgendaItem,
        AgendaStatus,
        mint_agenda_id,
    )

    store = AgendaStore()
    pause_store = PauseStore()
    now = datetime.now(timezone.utc)
    seed = AgendaItem(
        id=mint_agenda_id("preexisting", now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.SELF_REFLECTION,
        goal="propose pre-existing seed",
        risk_class=RiskClass.PROPOSE,
    )
    store.add(seed)

    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
        pause_store=pause_store,
    )
    kernel.pause_source(AgendaSource.SELF_REFLECTION, reason="loop_detected")
    result = await kernel.tick()
    assert any(r["reason"] == REASON_GOVERNOR_PAUSED for r in result.rejections)
    refreshed = store.get(seed.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.BLOCKED


@pytest.mark.asyncio
async def test_kernel_is_source_paused_accessor(
    isolated_home: Path,
    permissive_lane: WorkerLane,
    all_mappers_enabled: dict[AgendaSource, MapperConfig],
) -> None:
    store = AgendaStore()
    pause_store = PauseStore()
    kernel = AutonomyKernel(
        agenda_store=store,
        worker_lane=permissive_lane,
        config=KernelConfig(top_k=3),
        mapper_configs=all_mappers_enabled,
        pause_store=pause_store,
    )
    assert not kernel.is_source_paused(AgendaSource.SELF_REFLECTION)
    kernel.pause_source(AgendaSource.SELF_REFLECTION)
    assert kernel.is_source_paused(AgendaSource.SELF_REFLECTION)
