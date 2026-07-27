"""AgendaStore admission helpers — fuzzy near-dup detection + open-item
counts, consumed by the kernel's admission gate (Task 1.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.record import RiskClass


def _make_item(
    goal: str,
    *,
    source: AgendaSource,
    created_at: datetime,
) -> AgendaItem:
    return AgendaItem(
        id=mint_agenda_id(goal, now=created_at),
        created_at=created_at,
        updated_at=created_at,
        source=source,
        goal=goal,
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.PROPOSED,
    )


def test_fuzzy_dedupe_hits_rephrased_same_source_goal(isolated_home: Path) -> None:
    store = AgendaStore()
    now = datetime.now(timezone.utc)
    store.add(_make_item("add retry to worker", source=AgendaSource.SELF_REFLECTION, created_at=now))

    hit = store.find_fuzzy_dedupe(
        "add retrying to worker",
        AgendaSource.SELF_REFLECTION,
        threshold=0.9,
        window_hours=24,
        now=now,
    )
    assert hit is not None


def test_fuzzy_dedupe_misses_different_source(isolated_home: Path) -> None:
    store = AgendaStore()
    now = datetime.now(timezone.utc)
    store.add(_make_item("add retry to worker", source=AgendaSource.SELF_REFLECTION, created_at=now))

    miss = store.find_fuzzy_dedupe(
        "add retrying to worker",
        AgendaSource.PROVIDER_WATCH,
        threshold=0.9,
        window_hours=24,
        now=now,
    )
    assert miss is None


def test_fuzzy_dedupe_misses_outside_window(isolated_home: Path) -> None:
    store = AgendaStore()
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=48)
    store.add(_make_item("add retry to worker", source=AgendaSource.SELF_REFLECTION, created_at=stale))

    miss = store.find_fuzzy_dedupe(
        "add retrying to worker",
        AgendaSource.SELF_REFLECTION,
        threshold=0.9,
        window_hours=24,
        now=now,
    )
    assert miss is None


def test_count_helpers_return_correct_totals(isolated_home: Path) -> None:
    store = AgendaStore()
    now = datetime.now(timezone.utc)
    store.add(_make_item("doe-item-1", source=AgendaSource.SELF_REFLECTION, created_at=now))
    store.add(_make_item("doe-item-2", source=AgendaSource.SELF_REFLECTION, created_at=now))
    store.add(_make_item("doe-item-3", source=AgendaSource.PROVIDER_WATCH, created_at=now))

    assert store.count_open_total() == 3
    assert store.count_open_by_source(AgendaSource.SELF_REFLECTION) == 2
    assert store.count_open_by_source(AgendaSource.PROVIDER_WATCH) == 1
    assert store.count_open_by_source(AgendaSource.STRATEGIST) == 0
