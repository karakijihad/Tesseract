"""AU-23 — `AutonomyStrategistJob` + `StrategistReaperJob` integration.

End-to-end shape: pre-fetch → idle short-circuit, parse, dedup, publish,
workspace summary, ledger append. Full chain via the autonomy bus into
an AgendaStore through the strategist mapper, then expire via the
reaper.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy import publishers
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.event_bus import AutonomyEventBus
from tesseract.orchestrator.autonomy.mappers.strategist import map as map_strategist
from tesseract.orchestrator.autonomy.models import (
    AgendaSource,
    AgendaStatus,
)
from tesseract.orchestrator.autonomy.strategist import (
    Initiative,
    initiative_key,
    seen_ledger_path,
)
from tesseract.scheduler.tasks.autonomy_strategist import AutonomyStrategistJob
from tesseract.scheduler.tasks.strategist_reaper import StrategistReaperJob
from tesseract.scheduler.types import JobContext
from tesseract.workspace_events.events import EventStore


class _FakeAdapter:
    def __init__(self, output: str = ""):
        self.output = output
        self.calls = 0

    async def generate(self, prompt: str, options) -> str:  # noqa: D401
        self.calls += 1
        return self.output


class _FakeOptions:
    def __init__(self):
        self.provider = "fake"
        self.model = "model"


def _ctx(
    *,
    app: dict[str, Any] | None = None,
    fired_at: datetime,
    home: Path,
    extra_config: dict[str, Any] | None = None,
) -> JobContext:
    config: dict[str, Any] = {
        "tesseract_home": str(home),
        "lookback_days": 7,
        "dedupe_window_days": 14,
    }
    if extra_config:
        config.update(extra_config)
    return JobContext(
        job_name="autonomy_strategist",
        fired_at=fired_at,
        app=app or {},
        config=config,
        model_role=None,
    )


@pytest.fixture(autouse=True)
def _isolate_bus():
    bus = AutonomyEventBus()
    publishers.set_active_bus(bus)
    yield bus
    publishers.set_active_bus(None)


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_strategist.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    return adapter


def _seed_activity(home: Path, now: datetime) -> None:
    """Drop one entry in each substrate so `is_idle()` is False."""
    agenda = home / "agenda"
    agenda.mkdir(parents=True, exist_ok=True)
    (agenda / "index.jsonl").write_text(json.dumps({
        "item_id": "ag-test",
        "ts": (now - timedelta(hours=4)).isoformat(),
        "event": "transition",
        "from_status": "running",
        "to_status": "done",
        "reason": "ok",
        "goal": "a recently completed item",
        "source": "scheduler",
    }) + "\n", encoding="utf-8")


# ── strategist job ──────────────────────────────────────────────────


async def test_idle_short_circuit_skips_model(tmp_path: Path, chain: _FakeAdapter):
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    result = await AutonomyStrategistJob().run(_ctx(fired_at=now, home=tmp_path))
    assert result.ok
    assert result.detail == "idle"
    assert chain.calls == 0


async def test_happy_path_publishes_and_writes_workspace_event(
    tmp_path: Path, chain: _FakeAdapter, _isolate_bus: AutonomyEventBus,
):
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    _seed_activity(tmp_path, now)
    chain.output = json.dumps({
        "initiatives": [
            {
                "slug": "rotate-tavily-key",
                "goal": "Rotate the Tavily API key and update tesseract/.env.",
                "rationale": "Three rotations overdue per the security policy.",
                "success_criteria": [
                    "TAVILY_API_KEY in .env is regenerated",
                    "provider_probe passes",
                ],
                "suggested_risk_class": "operator_gate",
                "evidence": ["ag-test"],
                "confidence": 0.85,
                "horizon_days": 5,
            }
        ]
    })
    event_store = EventStore(tmp_path / "logs")
    app: dict[str, Any] = {"workspace_event_store": event_store}

    result = await AutonomyStrategistJob().run(
        _ctx(fired_at=now, home=tmp_path, app=app)
    )

    assert result.ok, result.detail
    assert result.payload["initiatives_returned"] == 1
    assert result.payload["initiatives_published"] == 1
    assert result.payload["workspace_event_id"]

    # Ledger persisted.
    seen = (tmp_path / "autonomy" / "strategist-seen.jsonl").read_text(encoding="utf-8")
    assert "rotate-tavily-key" in seen

    # Bus received one publish under STRATEGIST.
    pending = _isolate_bus.peek(AgendaSource.STRATEGIST)
    assert len(pending) == 1
    assert pending[0].payload["slug"] == "rotate-tavily-key"

    # Workspace event recorded under strategist_summary.
    events = event_store.list_events(kinds=("strategist_summary",))
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "strategist_summary"
    assert ev.source == "strategist"
    assert "rotate-tavily-key" in ev.summary


async def test_low_confidence_dropped(
    tmp_path: Path, chain: _FakeAdapter, _isolate_bus: AutonomyEventBus,
):
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    _seed_activity(tmp_path, now)
    chain.output = json.dumps({"initiatives": [{
        "slug": "weak",
        "goal": "Do a thing that does not really need doing.",
        "rationale": "Maybe worth investigating but probably not.",
        "success_criteria": ["maybe done"],
        "confidence": 0.3,
        "horizon_days": 3,
    }]})
    result = await AutonomyStrategistJob().run(_ctx(fired_at=now, home=tmp_path))
    assert result.ok
    assert result.payload["initiatives_returned"] == 1
    assert result.payload["initiatives_kept"] == 0
    assert result.payload["initiatives_published"] == 0


async def test_dedup_blocks_repeat_initiative(
    tmp_path: Path, chain: _FakeAdapter, _isolate_bus: AutonomyEventBus,
):
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    _seed_activity(tmp_path, now)
    chain.output = json.dumps({"initiatives": [{
        "slug": "x",
        "goal": "Ingest the new SDK and refresh the wiki page.",
        "rationale": "Failures traced to outdated calls.",
        "success_criteria": ["wiki updated"],
        "confidence": 0.8,
        "horizon_days": 5,
    }]})
    first = await AutonomyStrategistJob().run(_ctx(fired_at=now, home=tmp_path))
    assert first.payload["initiatives_published"] == 1

    # Re-fire 1h later — same goal, should dedupe.
    later = now + timedelta(hours=1)
    second = await AutonomyStrategistJob().run(_ctx(fired_at=later, home=tmp_path))
    assert second.payload["initiatives_returned"] == 1
    assert second.payload["initiatives_published"] == 0


async def test_role_unavailable_returns_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    _seed_activity(tmp_path, now)
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_strategist.build_chain_for_job",
        lambda *a, **k: [],
    )
    result = await AutonomyStrategistJob().run(_ctx(fired_at=now, home=tmp_path))
    assert result.ok
    assert result.detail == "role_unavailable"


async def test_ledger_append_failure_blocks_publish(
    tmp_path: Path,
    chain: _FakeAdapter,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
):
    """Codex audit 2026-05-20 §M2 — if the dedup ledger write fails, the
    initiative must NOT publish; otherwise it would re-fire on every
    tick of the dedup window."""
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    _seed_activity(tmp_path, now)
    chain.output = json.dumps({"initiatives": [{
        "slug": "ledger-fail",
        "goal": "Rotate the Tavily API key and update tesseract/.env.",
        "rationale": "Overdue rotation per security policy.",
        "success_criteria": ["env updated", "probe passes"],
        "confidence": 0.9,
        "horizon_days": 5,
    }]})

    def _fail(*_a, **_kw) -> bool:
        return False

    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_strategist.append_seen",
        _fail,
    )
    result = await AutonomyStrategistJob().run(_ctx(fired_at=now, home=tmp_path))
    assert result.ok, result.detail
    assert result.payload["initiatives_returned"] == 1
    assert result.payload["initiatives_kept"] == 1
    assert result.payload["initiatives_fresh"] == 1
    assert result.payload["initiatives_published"] == 0
    assert result.payload["ledger_failures"] == 1
    # No publish reached the bus.
    pending = _isolate_bus.peek(AgendaSource.STRATEGIST)
    assert pending == []


async def test_malformed_response_drops_silently(
    tmp_path: Path, chain: _FakeAdapter,
):
    now = datetime(2026, 5, 20, 19, 0, tzinfo=timezone.utc)
    _seed_activity(tmp_path, now)
    chain.output = "this is not JSON"
    result = await AutonomyStrategistJob().run(_ctx(fired_at=now, home=tmp_path))
    assert result.ok
    assert result.payload["initiatives_published"] == 0


# ── full chain into the agenda store via the mapper ─────────────────


def test_full_chain_publish_then_mapper_yields_agenda_draft():
    """Bus event built by `_publish` → mapper draft round-trip."""
    from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent
    from tesseract.scheduler.tasks.autonomy_strategist import _publish

    bus_events: list[AutonomyEvent] = []

    def _capture(event):
        bus_events.append(event)

    bus = AutonomyEventBus()
    bus.subscribe(AgendaSource.STRATEGIST, _async_noop_factory(_capture))
    publishers.set_active_bus(bus)
    try:
        when = datetime(2026, 5, 20, tzinfo=timezone.utc)
        i = Initiative(
            slug="reap-test",
            goal="Rotate the Tavily API key and update .env.",
            rationale="Overdue rotation.",
            success_criteria=["env updated", "probe passes"],
            confidence=0.85,
            horizon_days=4,
            suggested_risk_class="operator_gate",
        )
        _publish(i, when=when)
    finally:
        publishers.set_active_bus(None)

    # _publish uses publish_nowait → event is buffered, drained via bus.drain
    drained = bus.drain(AgendaSource.STRATEGIST)
    assert len(drained) == 1
    draft = map_strategist(drained[0])
    assert len(draft) == 1
    assert draft[0].risk_class.value == "operator_gate"
    assert draft[0].approvals_required[0].kind == "operator_review"


# helper for the chain test
def _async_noop_factory(capture):
    async def _noop(event):
        capture(event)
    return _noop


# ── reaper ──────────────────────────────────────────────────────────


def _make_agenda_item_for(
    *,
    store: AgendaStore,
    initiative: Initiative,
    created_at: datetime,
):
    """Mint an agenda item via the mapper and persist it as if the
    kernel folded the strategist bus event."""
    from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent

    event = AutonomyEvent.make(AgendaSource.STRATEGIST, {
        "slug": initiative.slug,
        "goal": initiative.goal,
        "rationale": initiative.rationale,
        "success_criteria": list(initiative.success_criteria),
        "suggested_risk_class": initiative.suggested_risk_class.value,
        "evidence": list(initiative.evidence),
        "confidence": initiative.confidence,
        "horizon_days": initiative.horizon_days,
    })
    drafts = map_strategist(event)
    assert len(drafts) == 1
    item = drafts[0].to_item(now=created_at)
    store.recompute_score(item)
    store.save(item)
    return item


async def test_reaper_expires_past_horizon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    store = AgendaStore()

    expired = Initiative(
        slug="expired",
        goal="Do an old thing that was never approved.",
        rationale="x" * 15,
        success_criteria=["ok"],
        confidence=0.8,
        horizon_days=2,
    )
    fresh = Initiative(
        slug="fresh",
        goal="Do a new thing the operator can still review.",
        rationale="y" * 15,
        success_criteria=["ok"],
        confidence=0.8,
        horizon_days=5,
    )

    item_expired = _make_agenda_item_for(
        store=store, initiative=expired, created_at=now - timedelta(days=4),
    )
    item_fresh = _make_agenda_item_for(
        store=store, initiative=fresh, created_at=now - timedelta(days=1),
    )

    # Ledger so reaper can recover horizon days.
    ledger = seen_ledger_path(tmp_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("w", encoding="utf-8") as fh:
        for i in (expired, fresh):
            fh.write(json.dumps({
                "key": initiative_key(i),
                "slug": i.slug,
                "goal": i.goal,
                "horizon_days": i.horizon_days,
                "ts": now.isoformat(),
            }) + "\n")

    ctx = JobContext(
        job_name="strategist_reaper",
        fired_at=now,
        app={"agenda_store": store},
        config={"tesseract_home": str(tmp_path)},
    )
    result = await StrategistReaperJob().run(ctx)
    assert result.ok, result.detail
    assert result.payload["scanned"] == 2
    assert result.payload["expired"] == 1

    reaped = store.get(item_expired.id)
    assert reaped.status is AgendaStatus.ABANDONED
    assert any(
        t.reason == "initiative_expired"
        for t in reaped.status_history
    )
    survived = store.get(item_fresh.id)
    assert survived.status is AgendaStatus.PROPOSED


