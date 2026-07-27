"""Shared fixtures for AU-4 AgendaStore tests.

All tests MUST run under a monkeypatched ``TESSERACT_HOME`` — the
AgendaStore resolves paths at call time so a single ``setenv`` keeps
production state untouched. Fixture helpers build Jane/John Doe-style
records so tests never leak operator-real fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def make_item(
    *,
    goal: str = "research-doe-pattern",
    source: AgendaSource = AgendaSource.SELF_REFLECTION,
    risk_class: RiskClass = RiskClass.AUTONOMOUS,
    status: AgendaStatus = AgendaStatus.PROPOSED,
    operator_priority: int = 0,
    item_id: str | None = None,
    now: datetime | None = None,
    budget_tokens_cap: int = 0,
    budget_seconds_cap: int = 0,
) -> AgendaItem:
    when = now or datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    iid = item_id or mint_agenda_id(goal, now=when)
    return AgendaItem(
        id=iid,
        created_at=when,
        updated_at=when,
        source=source,
        goal=goal,
        risk_class=risk_class,
        status=status,
        operator_priority=operator_priority,
        budget_tokens_cap=budget_tokens_cap,
        budget_seconds_cap=budget_seconds_cap,
    )


__all__ = ["isolated_home", "make_item"]
