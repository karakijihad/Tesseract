"""Shared fixtures for AU-6 Governor tests."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from tesseract.orchestrator.autonomy import AgendaStore, PauseStore
from tesseract.orchestrator.autonomy.governor import Governor, GovernorConfig
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    ApprovalGate,
    RiskClass,
    mint_agenda_id,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(isolated_home: Path) -> AgendaStore:
    return AgendaStore()


@pytest.fixture
def pause_store(isolated_home: Path) -> PauseStore:
    return PauseStore()


@pytest.fixture
def governor(
    store: AgendaStore,
    pause_store: PauseStore,
) -> Iterator[Governor]:
    g = Governor(
        agenda_store=store,
        pause_store=pause_store,
        config=GovernorConfig(
            cadence_seconds=60.0,
            loop_n=3,
            loop_window_hours=24,
            cost_threshold_multiplier=2.0,
            trust_consecutive_rejections=3,
        ),
    )
    yield g


def make_item(
    store: AgendaStore,
    *,
    goal: str,
    source: AgendaSource = AgendaSource.SELF_REFLECTION,
    risk_class: RiskClass = RiskClass.PROPOSE,
    status: AgendaStatus = AgendaStatus.PROPOSED,
    now: datetime | None = None,
    budget_tokens_cap: int = 0,
    budget_seconds_cap: int = 0,
    operator_priority: int = 0,
    approvals: list[ApprovalGate] | None = None,
) -> AgendaItem:
    """Mint + persist an item; helper for the detector tests.

    The id is suffixed with a 4-hex token so repeated calls with the same
    goal (which the loop detector intentionally generates) don't collide
    on the deterministic mint_agenda_id minute stamp.
    """
    when = now or datetime.now(timezone.utc)
    suffix = secrets.token_hex(2)
    item = AgendaItem(
        id=f"{mint_agenda_id(goal[:30], now=when)}-{suffix}",
        created_at=when,
        updated_at=when,
        source=source,
        goal=goal,
        risk_class=risk_class,
        budget_tokens_cap=budget_tokens_cap,
        budget_seconds_cap=budget_seconds_cap,
        operator_priority=operator_priority,
        approvals_required=approvals or [],
    )
    store.add(item)
    if status != AgendaStatus.PROPOSED:
        store.transition(item, status, reason="test_seed")
    return item


__all__ = [
    "governor",
    "isolated_home",
    "make_item",
    "pause_store",
    "store",
]
