"""Admission gate in ``AutonomyKernel._persist_draft`` (Task 1.4) —
degenerate / fuzzy-duplicate / over-cap drafts are pruned before they
become AgendaItems, and every prune is recorded to the ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.kernel import AutonomyKernel, KernelConfig
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.prune_ledger import PruneStage, read_prunes
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.orchestrator.workers.record import RiskClass


def _build_kernel(*, config: KernelConfig | None = None) -> AutonomyKernel:
    return AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        config=config or KernelConfig(max_open_total=40, max_open_per_source=8),
    )


def _draft(goal: str, *, source: AgendaSource = AgendaSource.SELF_REFLECTION) -> AgendaItemDraft:
    return AgendaItemDraft(goal=goal, source=source, risk_class=RiskClass.PROPOSE)


def test_fuzzy_dedupe_uses_mapper_window_when_configured(isolated_home: Path) -> None:
    """Deferred 2026-07-12 — per-mapper ``dedupe_window_hours`` was parsed
    but never read; the fuzzy dedupe now prefers it over the kernel-wide
    ``fuzzy_window_hours`` (which stays the fallback for unmapped sources)."""
    from tesseract.orchestrator.autonomy.kernel import MapperConfig

    kernel = AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        config=KernelConfig(max_open_total=40, max_open_per_source=8),
        mapper_configs={
            AgendaSource.SELF_REFLECTION: MapperConfig(
                enabled=True,
                source=AgendaSource.SELF_REFLECTION,
                default_risk_class=RiskClass.PROPOSE,
                dedupe_window_hours=99,
            )
        },
    )
    seen: dict[str, int] = {}
    original = kernel._agenda.find_fuzzy_dedupe

    def _spy(goal, source, *, threshold, window_hours, now):
        seen["window_hours"] = window_hours
        return original(goal, source, threshold=threshold, window_hours=window_hours, now=now)

    kernel._agenda.find_fuzzy_dedupe = _spy  # type: ignore[method-assign]

    kernel._persist_draft(_draft("mapper window check"))
    assert seen["window_hours"] == 99

    # Unmapped source falls back to the kernel-wide window.
    seen.clear()
    kernel._persist_draft(_draft("scout window check", source=AgendaSource.SCOUT))
    assert seen["window_hours"] == kernel._config.fuzzy_window_hours


def test_degenerate_goal_pruned_as_malformed(isolated_home: Path) -> None:
    kernel = _build_kernel()
    created, deduped = kernel._persist_draft(_draft("}"))
    assert (created, deduped) == (False, True)
    prunes = read_prunes()
    assert len(prunes) == 1
    assert prunes[0].stage == PruneStage.MALFORMED


def test_fuzzy_near_duplicate_pruned_as_duplicate(isolated_home: Path) -> None:
    kernel = _build_kernel()
    first_created, _ = kernel._persist_draft(_draft("add retry to worker"))
    assert first_created is True

    created, deduped = kernel._persist_draft(_draft("add retrying to worker"))
    assert (created, deduped) == (False, True)
    prunes = read_prunes()
    assert len(prunes) == 1
    assert prunes[0].stage == PruneStage.DUPLICATE


def test_max_open_per_source_prunes_as_capped(isolated_home: Path) -> None:
    kernel = _build_kernel(
        config=KernelConfig(max_open_total=40, max_open_per_source=2)
    )
    assert kernel._persist_draft(_draft("doe item alpha"))[0] is True
    assert kernel._persist_draft(_draft("doe item beta"))[0] is True

    created, deduped = kernel._persist_draft(_draft("doe item gamma"))
    assert (created, deduped) == (False, True)
    prunes = read_prunes()
    assert len(prunes) == 1
    assert prunes[0].stage == PruneStage.CAPPED


def test_max_open_total_prunes_as_capped(isolated_home: Path) -> None:
    kernel = _build_kernel(
        config=KernelConfig(max_open_total=1, max_open_per_source=8)
    )
    assert kernel._persist_draft(_draft("doe item one", source=AgendaSource.SELF_REFLECTION))[0] is True

    created, deduped = kernel._persist_draft(
        _draft("doe item two", source=AgendaSource.PROVIDER_WATCH)
    )
    assert (created, deduped) == (False, True)
    prunes = read_prunes()
    assert len(prunes) == 1
    assert prunes[0].stage == PruneStage.CAPPED


def test_operator_source_exempt_from_gate(isolated_home: Path) -> None:
    kernel = _build_kernel()
    created, deduped = kernel._persist_draft(
        _draft("}", source=AgendaSource.OPERATOR)
    )
    assert created is True
    assert read_prunes() == []


def test_clean_unique_draft_created_with_no_prune(isolated_home: Path) -> None:
    kernel = _build_kernel()
    created, deduped = kernel._persist_draft(_draft("doe unique clean goal"))
    assert (created, deduped) == (True, False)
    assert read_prunes() == []
