"""Task 2A — ``AgendaStatus.UNVETTED`` pre-queue holding state.

Covers: config parse of ``agenda.yaml::vetter``, mint gating in
``AutonomyKernel._persist_draft`` (vet_required source -> UNVETTED,
non-vet_required -> PROPOSED, vet disabled -> always PROPOSED), and
selection-loop exclusion (an UNVETTED item is never dispatched)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel, KernelConfig
from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import RiskClass


def _build_kernel(*, config: KernelConfig) -> AutonomyKernel:
    return AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        config=config,
    )


def _draft(goal: str, *, source: AgendaSource) -> AgendaItemDraft:
    return AgendaItemDraft(goal=goal, source=source, risk_class=RiskClass.PROPOSE)


def test_vetter_block_parses_enabled_and_vet_required() -> None:
    config = KernelConfig.from_yaml_dict(
        {
            "vetter": {
                "enabled": True,
                "vet_required": ["self_reflection", "vault_signal"],
            }
        }
    )
    assert config.vet_enabled is True
    assert config.vet_required == frozenset({AgendaSource.SELF_REFLECTION, AgendaSource.VAULT_SIGNAL})


def test_vetter_block_skips_unknown_source_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        config = KernelConfig.from_yaml_dict(
            {"vetter": {"enabled": True, "vet_required": ["self_reflection", "not_a_real_source"]}}
        )
    assert config.vet_required == frozenset({AgendaSource.SELF_REFLECTION})
    assert any("not_a_real_source" in msg for msg in caplog.messages)


def test_vetter_block_absent_defaults_disabled() -> None:
    # Missing block: enabled defaults False; vet_required still falls
    # back to DEFAULT_VET_REQUIRED (the disabled flag is what actually
    # gates mint behavior, not an empty set).
    config = KernelConfig.from_yaml_dict({})
    assert config.vet_enabled is False
    assert config.vet_required == frozenset(
        {
            AgendaSource.SELF_REFLECTION,
            AgendaSource.MEMORY_SIGNAL,
            AgendaSource.VAULT_SIGNAL,
        }
    )


def test_vet_required_source_mints_unvetted(isolated_home: Path) -> None:
    kernel = _build_kernel(
        config=KernelConfig(
            max_open_total=40,
            max_open_per_source=8,
            vet_enabled=True,
            vet_required=frozenset({AgendaSource.SELF_REFLECTION}),
        )
    )
    created, deduped = kernel._persist_draft(
        _draft("doe vet required item", source=AgendaSource.SELF_REFLECTION)
    )
    assert (created, deduped) == (True, False)
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.UNVETTED


def test_non_vet_required_source_mints_proposed(isolated_home: Path) -> None:
    kernel = _build_kernel(
        config=KernelConfig(
            max_open_total=40,
            max_open_per_source=8,
            vet_enabled=True,
            vet_required=frozenset({AgendaSource.SELF_REFLECTION}),
        )
    )
    created, deduped = kernel._persist_draft(
        _draft("doe non vet required item", source=AgendaSource.PROVIDER_WATCH)
    )
    assert (created, deduped) == (True, False)
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.PROPOSED


def test_vet_disabled_always_mints_proposed(isolated_home: Path) -> None:
    kernel = _build_kernel(
        config=KernelConfig(
            max_open_total=40,
            max_open_per_source=8,
            vet_enabled=False,
            vet_required=frozenset({AgendaSource.SELF_REFLECTION}),
        )
    )
    created, deduped = kernel._persist_draft(
        _draft("doe disabled vetter item", source=AgendaSource.SELF_REFLECTION)
    )
    assert (created, deduped) == (True, False)
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.PROPOSED


@pytest.mark.asyncio
async def test_unvetted_item_not_dispatched_by_tick(isolated_home: Path) -> None:
    kernel = _build_kernel(
        config=KernelConfig(
            max_open_total=40,
            max_open_per_source=8,
            vet_enabled=True,
            vet_required=frozenset({AgendaSource.SELF_REFLECTION}),
        )
    )
    created, _ = kernel._persist_draft(
        _draft("doe unvetted not dispatched", source=AgendaSource.SELF_REFLECTION)
    )
    assert created is True
    selected, rejections = await kernel._select_and_dispatch()
    assert selected == []
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.UNVETTED
