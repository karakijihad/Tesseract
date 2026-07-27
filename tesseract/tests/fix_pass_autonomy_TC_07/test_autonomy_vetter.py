"""Task 2B Part D — AutonomyVetterJob.

Covers: promote/reject/merge verdicts applied to the store, idle
short-circuit (no chain built), role-unavailable + empty-response
fail-safes (items stay UNVETTED), and hallucinated ids ignored.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    mint_agenda_id,
)
from tesseract.orchestrator.autonomy.prune_ledger import PruneStage, read_prunes
from tesseract.orchestrator.workers.record import RiskClass
from tesseract.scheduler.tasks.autonomy_vetter import AutonomyVetterJob
from tesseract.scheduler.types import JobContext

pytestmark = pytest.mark.asyncio


class _FakeAdapter:
    def __init__(self, output: str = ""):
        self.output = output
        self.calls: list[tuple[str, Any]] = []

    async def generate(self, prompt: str, options) -> str:  # noqa: D401
        self.calls.append((prompt, options))
        return self.output


class _FakeOptions:
    def __init__(self, provider: str = "fake", model: str = "model"):
        self.provider = provider
        self.model = model


def _make_item(*, goal: str, source: AgendaSource, when: datetime) -> AgendaItem:
    return AgendaItem(
        id=mint_agenda_id(goal, now=when),
        created_at=when,
        updated_at=when,
        source=source,
        goal=goal,
        rationale="doe rationale",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.UNVETTED,
    )


def _ctx(*, app: dict[str, Any], fired_at: datetime) -> JobContext:
    return JobContext(job_name="autonomy_vetter", fired_at=fired_at, app=app, config={}, model_role=None)


async def test_promote_reject_merge_applied(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    when = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    item_promote = store.add(_make_item(goal="doe promote candidate", source=AgendaSource.SELF_REFLECTION, when=when))
    item_reject = store.add(_make_item(goal="doe reject candidate", source=AgendaSource.SELF_REFLECTION, when=when))
    item_merge_target = store.add(_make_item(goal="doe merge target", source=AgendaSource.SELF_REFLECTION, when=when))
    item_merge = store.add(_make_item(goal="doe merge candidate", source=AgendaSource.SELF_REFLECTION, when=when))

    payload = {
        "verdicts": [
            {"id": item_promote.id, "verdict": "promote", "score": 0.8, "reason": "useful"},
            {"id": item_reject.id, "verdict": "reject", "score": 0.1, "reason": "vague"},
            {
                "id": item_merge.id,
                "verdict": "merge",
                "score": 0.5,
                "reason": "dup",
                "merge_into": item_merge_target.id,
            },
        ]
    }
    adapter = _FakeAdapter(output=json.dumps(payload))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_vetter.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    result = await AutonomyVetterJob().run(_ctx(app={"agenda_store": store}, fired_at=when))

    assert result.ok is True
    assert result.payload == {"unvetted": 4, "promoted": 1, "rejected": 1, "merged": 1}

    promoted = store.get(item_promote.id)
    assert promoted.status == AgendaStatus.PROPOSED
    assert promoted.vet_score == pytest.approx(0.8)

    rejected = store.get(item_reject.id)
    assert rejected.status == AgendaStatus.CANCELLED

    merged = store.get(item_merge.id)
    assert merged.status == AgendaStatus.SUPERSEDED

    # Merge target itself received no verdict this batch — stays UNVETTED.
    assert store.get(item_merge_target.id).status == AgendaStatus.UNVETTED

    prunes = read_prunes()
    stages = {p.item_id: p.stage for p in prunes}
    assert stages[item_reject.id] == PruneStage.LOW_VALUE
    assert stages[item_merge.id] == PruneStage.DUPLICATE


async def test_idle_no_unvetted_no_chain_built(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    called = {"hit": False}

    def _boom(*a, **k):
        called["hit"] = True
        return []

    monkeypatch.setattr("tesseract.scheduler.tasks.autonomy_vetter.build_chain_for_job", _boom)
    result = await AutonomyVetterJob().run(
        _ctx(app={"agenda_store": store}, fired_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
    )
    assert result.ok is True
    assert result.detail == "idle"
    assert result.payload == {"unvetted": 0}
    assert called["hit"] is False


async def test_role_unavailable_leaves_items_unvetted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    when = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    item = store.add(_make_item(goal="doe role unavailable", source=AgendaSource.SELF_REFLECTION, when=when))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_vetter.build_chain_for_job", lambda *a, **k: []
    )
    result = await AutonomyVetterJob().run(_ctx(app={"agenda_store": store}, fired_at=when))
    assert result.ok is True
    assert result.detail == "role_unavailable"
    assert store.get(item.id).status == AgendaStatus.UNVETTED


async def test_empty_response_leaves_items_unvetted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    when = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    item = store.add(_make_item(goal="doe empty response", source=AgendaSource.SELF_REFLECTION, when=when))
    adapter = _FakeAdapter(output="")
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_vetter.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    result = await AutonomyVetterJob().run(_ctx(app={"agenda_store": store}, fired_at=when))
    assert result.ok is True
    assert result.detail == "empty_response"
    assert store.get(item.id).status == AgendaStatus.UNVETTED


async def test_hallucinated_id_ignored_item_stays_unvetted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AgendaStore()
    when = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    item = store.add(_make_item(goal="doe real item", source=AgendaSource.SELF_REFLECTION, when=when))
    adapter = _FakeAdapter(
        output=json.dumps({"verdicts": [{"id": "ag-not-in-batch", "verdict": "promote", "score": 0.9}]})
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_vetter.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    result = await AutonomyVetterJob().run(_ctx(app={"agenda_store": store}, fired_at=when))
    assert result.ok is True
    assert result.payload == {"unvetted": 1, "promoted": 0, "rejected": 0, "merged": 0}
    assert store.get(item.id).status == AgendaStatus.UNVETTED


async def test_self_merge_treated_as_reject(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A MERGE verdict whose merge_into equals its own id is a hallucinated
    self-reference — must downgrade to reject, not SUPERSEDED-into-itself."""
    store = AgendaStore()
    when = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    item = store.add(_make_item(goal="doe self merge candidate", source=AgendaSource.SELF_REFLECTION, when=when))
    adapter = _FakeAdapter(
        output=json.dumps(
            {"verdicts": [{"id": item.id, "verdict": "merge", "score": 0.5, "merge_into": item.id}]}
        )
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_vetter.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    result = await AutonomyVetterJob().run(_ctx(app={"agenda_store": store}, fired_at=when))
    assert result.ok is True
    assert result.payload == {"unvetted": 1, "promoted": 0, "rejected": 1, "merged": 0}
    assert store.get(item.id).status == AgendaStatus.CANCELLED
