"""P7 Task 2b — scout job (identity-anchored discovery) + scout mapper +
scout reaper.

Covers:
- canned search + canned query-gen/eval chain → publish_to_bus(SCOUT, ...);
  real mapper + real kernel admission mints UNVETTED with a "why us / why
  now" line + source link in the payload
- dedup across runs via the persistent seen-store (tmp TESSERACT_HOME)
- staleness expiry: an aged unacted SCOUT item + ScoutReaperJob → ABANDONED
- missing config keys → ok=False, detail names the key
- per-source circuit breaker: 3 consecutive failures trips it; the next
  run skips that source (proven by an explosive fetcher never being
  called) while another source still proceeds
- fuzzy dedup across differently-worded/differently-id'd events → one
  open item via the real AgendaStore.find_fuzzy_dedupe path
- approval gate: a promoted SCOUT item still parks at AWAITING_OPERATOR
- feedback: a previously published item that reached a terminal state
  writes one source-tagged memory; a second run does not re-feed it
- idle short-circuit: all results already seen → ok=True "idle", no eval
  LLM call, no publish
- no logs pollution under tesseract/logs/ from the test run
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent, AutonomyEventBus
from tesseract.orchestrator.autonomy.kernel import (
    REASON_AWAITING_OPERATOR,
    AutonomyKernel,
    KernelConfig,
    MapperConfig,
)
from tesseract.orchestrator.autonomy.mappers.scout import map as map_scout
from tesseract.orchestrator.autonomy.models import AgendaItem, AgendaSource, AgendaStatus, RiskClass
from tesseract.orchestrator.autonomy import publishers
from tesseract.orchestrator.workers.lane import WorkerLane
from tesseract.scheduler.tasks import scout
from tesseract.scheduler.tasks.scout import ScoutJob
from tesseract.scheduler.tasks.scout_reaper import ScoutReaperJob
from tesseract.scheduler.types import JobContext

# This file is tesseract/tests/lean_agent_os_p7/test_scout.py, so parents[2]
# is `tesseract/`, not the repo root.
_REPO_LOGS = Path(__file__).resolve().parents[2] / "logs"


def _logs_snapshot() -> dict[str, int]:
    """Recursive, per-file-size snapshot — a top-level-name-only diff
    would miss an appended line to an existing file (e.g. a
    circuit-breakers/*.jsonl growing), which is exactly the zero-tolerance
    case the project's logs-pollution rule exists to catch."""
    if not _REPO_LOGS.exists():
        return {}
    return {
        str(p.relative_to(_REPO_LOGS)): p.stat().st_size
        for p in _REPO_LOGS.rglob("*")
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _guard_logs_pollution():
    """Zero-tolerance guard (CLAUDE.md): no test may write under
    tesseract/logs/. Snapshot at THIS test's setup and re-check at its
    teardown, so the comparison window spans only this test's own
    execution — not the whole collection→run span, which false-positives
    on any concurrent live-service write (mirror-backend.log, tokenjuice/,
    …) during a slow suite."""
    before = _logs_snapshot()
    yield
    after = _logs_snapshot()
    assert after == before, (
        f"tesseract/logs/ changed during this test. before={before} after={after}"
    )


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeOptions:
    def __init__(self, provider: str = "fake", model: str = "model"):
        self.provider = provider
        self.model = model


class _FakeChainAdapter:
    """Branches on prompt content: the eval prompt always carries the
    literal ``CANDIDATES`` section header; the query-gen prompt never
    does."""

    def __init__(self, *, query_response: dict | None = None, eval_response: dict | None = None):
        self.calls: list[tuple[str, Any]] = []
        self.query_response = query_response if query_response is not None else {"queries": []}
        self.eval_response = eval_response if eval_response is not None else {"picks": []}

    async def generate(self, prompt: str, options) -> str:
        self.calls.append((prompt, options))
        data = self.eval_response if "CANDIDATES" in prompt else self.query_response
        return json.dumps(data)


class _FakeMemoryStore:
    def __init__(self):
        self.writes: list[tuple[Any, str, str | None]] = []

    def write(self, frontmatter, body, *, subdir_override=None):
        self.writes.append((frontmatter, body, subdir_override))
        return True


class _FakeBundle:
    def __init__(self, store):
        self.store = store


def _patch_chain(monkeypatch: pytest.MonkeyPatch, adapter: _FakeChainAdapter) -> None:
    monkeypatch.setattr(scout, "build_chain_for_job", lambda *a, **k: [(adapter, _FakeOptions())])


def _patch_search(monkeypatch: pytest.MonkeyPatch, results_by_query: dict[str, list[dict] | Exception]) -> None:
    async def _fetch(query: str) -> list[dict]:
        res = results_by_query.get(query)
        if res is None:
            return []
        if isinstance(res, Exception):
            raise res
        return res

    monkeypatch.setattr(scout, "_make_search_fetcher", lambda: _fetch)


def _patch_feed(monkeypatch: pytest.MonkeyPatch, results_by_url: dict[str, list[dict] | Exception]) -> None:
    async def _fetch(url: str) -> list[dict]:
        res = results_by_url.get(url)
        if res is None:
            return []
        if isinstance(res, Exception):
            raise res
        return res

    monkeypatch.setattr(scout, "_make_feed_fetcher", lambda: _fetch)


def _ctx(*, config: dict[str, Any], app: dict | None = None, fired_at: datetime | None = None) -> JobContext:
    return JobContext(
        job_name="scout",
        fired_at=fired_at or datetime(2026, 7, 6, 4, 30, tzinfo=timezone.utc),
        app=app if app is not None else {},
        config=config,
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_bus():
    bus = AutonomyEventBus()
    publishers.set_active_bus(bus)
    yield bus
    publishers.set_active_bus(None)


def _build_kernel(*, config: KernelConfig, bus: AutonomyEventBus) -> AutonomyKernel:
    return AutonomyKernel(
        agenda_store=AgendaStore(),
        worker_lane=WorkerLane.from_mission_lanes_block({}),
        config=config,
        mappers={AgendaSource.SCOUT: map_scout},
        mapper_configs={
            AgendaSource.SCOUT: MapperConfig(
                enabled=True,
                source=AgendaSource.SCOUT,
                default_risk_class=RiskClass.PROPOSE,
                dedupe_window_hours=24,
            )
        },
        event_bus=bus,
    )


_BASE_CONFIG = {"max_searches_per_run": 3, "max_proposals_per_run": 2, "staleness_days": 14}


# ── Test 1: happy path → publish → real mapper → real kernel admission ──


async def test_happy_path_publishes_and_admits_unvetted(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_search(monkeypatch, {
        "ai discovery": [
            {"title": "New ML paper drops", "url": "https://arxiv.org/abs/9999", "content": "abstract text"},
        ],
    })
    adapter = _FakeChainAdapter(
        query_response={"queries": ["ai discovery"]},
        eval_response={"picks": [{
            "candidate_index": 0,
            "why_us_why_now": "TARS tracks ML tooling closely; this lands today.",
            "diff_text": "",
        }]},
    )
    _patch_chain(monkeypatch, adapter)

    kernel = _build_kernel(
        config=KernelConfig(top_k=3, vet_enabled=True, vet_required=frozenset({AgendaSource.SCOUT})),
        bus=_isolate_bus,
    )

    result = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG)))

    assert result.ok is True
    buffered = _isolate_bus.peek(AgendaSource.SCOUT)
    assert len(buffered) == 1
    event = buffered[0]
    assert event.payload["url"] == "https://arxiv.org/abs/9999"
    assert event.payload["why_us_why_now"]

    tick = await kernel.tick()
    assert tick.items_created == 1
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.UNVETTED
    assert "https://arxiv.org/abs/9999" in items[0].rationale
    assert "TARS tracks ML tooling closely" in items[0].rationale


# ── Test 2: dedup across runs ──


async def test_dedup_across_runs_skips_seen_urls(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_search(monkeypatch, {
        "ai discovery": [
            {"title": "Same finding", "url": "https://example.com/finding", "content": "x"},
        ],
    })
    adapter = _FakeChainAdapter(
        query_response={"queries": ["ai discovery"]},
        eval_response={"picks": [{"candidate_index": 0, "why_us_why_now": "matters now", "diff_text": ""}]},
    )
    _patch_chain(monkeypatch, adapter)

    first = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG)))
    assert first.ok is True
    assert len(_isolate_bus.peek(AgendaSource.SCOUT)) == 1
    _isolate_bus.drain(AgendaSource.SCOUT)

    second = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG)))
    assert second.ok is True
    assert "idle" in second.detail
    assert _isolate_bus.peek(AgendaSource.SCOUT) == []


# ── Test 3: staleness expiry via ScoutReaperJob ──


async def test_staleness_expiry_transitions_abandoned(isolated_home: Path) -> None:
    store = AgendaStore()
    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    draft = AgendaItemDraft(
        goal="scout finding: old thing",
        source=AgendaSource.SCOUT,
        risk_class=RiskClass.PROPOSE,
        source_event_id="evt_scout_abc123",
    )
    item = draft.to_item(now=old_time, status=AgendaStatus.PROPOSED)
    store.add(item)

    horizon_path = isolated_home / "autonomy" / "scout-horizon.jsonl"
    horizon_path.parent.mkdir(parents=True, exist_ok=True)
    horizon_path.write_text(
        json.dumps({"event_id": "evt_scout_abc123", "staleness_days": 7, "ts": old_time.isoformat()}) + "\n",
        encoding="utf-8",
    )

    reaper_ctx = JobContext(
        job_name="scout_reaper",
        fired_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        app={},
        config={},
    )
    result = await ScoutReaperJob().run(reaper_ctx)

    assert result.ok is True
    refreshed = store.get(item.id)
    assert refreshed is not None
    assert refreshed.status == AgendaStatus.ABANDONED


# ── Test 4: missing config ──


async def test_missing_max_searches_per_run_fails() -> None:
    cfg = {"max_proposals_per_run": 2, "staleness_days": 14}
    result = await ScoutJob().run(_ctx(config=cfg))
    assert result.ok is False
    assert "max_searches_per_run" in result.detail


async def test_missing_max_proposals_per_run_fails() -> None:
    cfg = {"max_searches_per_run": 3, "staleness_days": 14}
    result = await ScoutJob().run(_ctx(config=cfg))
    assert result.ok is False
    assert "max_proposals_per_run" in result.detail


async def test_missing_staleness_days_fails() -> None:
    cfg = {"max_searches_per_run": 3, "max_proposals_per_run": 2}
    result = await ScoutJob().run(_ctx(config=cfg))
    assert result.ok is False
    assert "staleness_days" in result.detail


# ── Test 5: per-source breaker ──


async def test_breaker_opens_after_three_failures_skips_source(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    """2026-07-06 second review finding: a breaker pre-tripped by calling
    ``record_failure`` directly never exercises ``scout.py``'s own
    ``_run_source``/``record_failure`` call site, so a bug there would pass
    silently. ``CircuitBreaker`` only persists a *tripped* event to disk —
    an in-progress failure count is never rehydrated across separate
    ``CircuitBreaker(...)`` constructions — so "3 consecutive failures"
    has to land within the single ``_sweep()`` call that shares one
    breaker instance across every query: here, 3 always-failing search
    queries in ONE ``ScoutJob().run()``. A second run must then skip that
    source (breaker open) while a feed source still proceeds."""
    search_calls: list[str] = []

    async def _always_fails(query: str) -> list[dict]:
        search_calls.append(query)
        raise RuntimeError("boom")

    monkeypatch.setattr(scout, "_make_search_fetcher", lambda: _always_fails)
    adapter = _FakeChainAdapter(query_response={"queries": ["q1", "q2", "q3"]})
    _patch_chain(monkeypatch, adapter)

    cfg = dict(_BASE_CONFIG)  # max_searches_per_run=3
    first = await ScoutJob().run(_ctx(config=cfg))
    assert first.ok is False
    assert len(search_calls) == 3

    from tesseract.context.circuit_breaker import CircuitBreaker

    breaker_dir = isolated_home / "logs" / "circuit-breakers"
    assert CircuitBreaker(name="scout_tavily", log_dir=breaker_dir).is_tripped

    _patch_feed(monkeypatch, {
        "https://feed.example.com/rss": [
            {"title": "Feed item", "url": "https://feed.example.com/item1"},
        ],
    })
    adapter.eval_response = {"picks": [{"candidate_index": 0, "why_us_why_now": "matters", "diff_text": ""}]}
    cfg["seed_topics"] = [{"topic": "feed test", "feeds": ["https://feed.example.com/rss"]}]

    second = await ScoutJob().run(_ctx(config=cfg))

    assert second.ok is True
    assert len(search_calls) == 3  # unchanged — breaker open, search skipped this run
    assert "scout_tavily" in second.detail
    buffered = _isolate_bus.peek(AgendaSource.SCOUT)
    assert len(buffered) == 1
    assert buffered[0].payload["url"] == "https://feed.example.com/item1"


async def test_all_sources_breaker_open_alerts_instead_of_silent_idle(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    """2026-07-06 review finding: a breaker never self-heals (nothing calls
    record_success() on a source that's never attempted again), so if the
    hard skip were reported as a quiet ok=True 'idle', a single bad morning
    would permanently and silently kill discovery — on_failure: alert only
    fires on ok=False. Every source breaker-open (no other source to fall
    back on) must surface as ok=False so the operator keeps getting toasted
    until they intervene."""
    from tesseract.context.circuit_breaker import CircuitBreaker

    breaker_dir = isolated_home / "logs" / "circuit-breakers"
    cb = CircuitBreaker(name="scout_tavily", max_failures=3, log_dir=breaker_dir)
    cb.record_failure("boom")
    cb.record_failure("boom")
    cb.record_failure("boom")
    assert cb.is_tripped

    search_calls: list[str] = []

    async def _search_should_not_run(query: str) -> list[dict]:
        search_calls.append(query)
        return []

    monkeypatch.setattr(scout, "_make_search_fetcher", lambda: _search_should_not_run)
    adapter = _FakeChainAdapter(query_response={"queries": ["should be skipped"]})
    _patch_chain(monkeypatch, adapter)

    result = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG)))

    assert result.ok is False
    assert search_calls == []
    assert "breaker-open" in result.detail
    assert _isolate_bus.peek(AgendaSource.SCOUT) == []


# ── Test 6: fuzzy dedup ──


async def test_fuzzy_dedup_across_differently_worded_events(
    isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    kernel = _build_kernel(config=KernelConfig(top_k=3), bus=_isolate_bus)

    first_event = AutonomyEvent.make(
        AgendaSource.SCOUT,
        {
            "title": "New transformer architecture beats benchmark",
            "url": "https://example.com/paper1",
            "why_us_why_now": "TARS should track model architecture advances; fresh today.",
        },
        event_id="evt_scout_run_one",
    )
    kernel.bus.publish_nowait(first_event)
    first_tick = await kernel.tick()
    assert first_tick.items_created == 1

    second_event = AutonomyEvent.make(
        AgendaSource.SCOUT,
        {
            "title": "New transformer architecture beats the benchmark",
            "url": "https://example.com/paper1-mirror",
            "why_us_why_now": "TARS should track model architecture advances; fresh today, worth a look.",
        },
        event_id="evt_scout_run_two",
    )
    assert second_event.event_id != first_event.event_id
    kernel.bus.publish_nowait(second_event)
    second_tick = await kernel.tick()
    assert second_tick.items_created == 0
    assert second_tick.items_deduped == 1

    items = kernel._agenda.list_active()
    assert len(items) == 1


# ── Test 7: approval gate ──


def test_mapper_attaches_unfulfilled_operator_review_gate() -> None:
    event = AutonomyEvent.make(
        AgendaSource.SCOUT,
        {"title": "Cool finding", "url": "https://example.com/x", "why_us_why_now": "matters"},
    )
    draft = map_scout(event)[0]
    assert len(draft.approvals_required) == 1
    gate = draft.approvals_required[0]
    assert gate.kind == "operator_review"
    assert gate.target.startswith("scout:")
    assert gate.fulfilled is False


async def test_promoted_item_awaits_operator_not_dispatched(
    isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    kernel = _build_kernel(config=KernelConfig(top_k=3, vet_enabled=False), bus=_isolate_bus)
    kernel.bus.publish_nowait(
        AutonomyEvent.make(
            AgendaSource.SCOUT,
            {"title": "Cool finding", "url": "https://example.com/y", "why_us_why_now": "matters"},
        )
    )

    result = await kernel.tick()

    assert result.items_created == 1
    assert result.selected == []
    assert any(r["reason"] == REASON_AWAITING_OPERATOR for r in result.rejections)
    items = kernel._agenda.list_active()
    assert len(items) == 1
    assert items[0].status == AgendaStatus.AWAITING_OPERATOR


# ── Test 8: accept/reject → memory feedback ──


async def test_feedback_scan_writes_memory_once_then_stops(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    store = AgendaStore()
    draft = AgendaItemDraft(
        goal="scout finding: old rejected thing",
        source=AgendaSource.SCOUT,
        risk_class=RiskClass.PROPOSE,
        source_event_id="evt_scout_rejected1",
    )
    item = draft.to_item(status=AgendaStatus.AWAITING_OPERATOR)
    store.add(item)
    store.transition(item, AgendaStatus.CANCELLED, reason="operator_rejected", by="operator")

    fake_store = _FakeMemoryStore()
    app = {"memory_bundle": _FakeBundle(fake_store)}
    monkeypatch.setattr(scout, "build_chain_for_job", lambda *a, **k: [])

    ctx = _ctx(config=dict(_BASE_CONFIG), app=app)
    result = await ScoutJob().run(ctx)
    assert result.ok is True
    assert len(fake_store.writes) == 1
    fm, body, subdir = fake_store.writes[0]
    assert fm.source_type == "scout"
    assert "rejected" in fm.tags

    result2 = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG), app=app))
    assert result2.ok is True
    assert len(fake_store.writes) == 1


async def test_abandoned_item_excluded_from_feedback(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    """2026-07-06 second review finding: ABANDONED is ScoutReaperJob's own
    staleness timeout (nobody looked at it), not an operator decision —
    must not be memory-written or counted as feedback."""
    store = AgendaStore()
    draft = AgendaItemDraft(
        goal="scout finding: stale unacted thing",
        source=AgendaSource.SCOUT,
        risk_class=RiskClass.PROPOSE,
        source_event_id="evt_scout_abandoned1",
    )
    item = draft.to_item(status=AgendaStatus.AWAITING_OPERATOR)
    store.add(item)
    store.transition(item, AgendaStatus.ABANDONED, reason="scout_proposal_expired", by="recovery")

    fake_store = _FakeMemoryStore()
    app = {"memory_bundle": _FakeBundle(fake_store)}
    monkeypatch.setattr(scout, "build_chain_for_job", lambda *a, **k: [])

    result = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG), app=app))
    assert result.ok is True
    assert fake_store.writes == []
    assert scout._summarize_history([item]) == []


async def test_superseded_item_excluded_from_feedback(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    """2026-07-06 second review finding: SUPERSEDED is the vetter's own
    dedup merge (``by="kernel"``), not an operator decision — must not be
    memory-written or counted as feedback."""
    store = AgendaStore()
    draft = AgendaItemDraft(
        goal="scout finding: merged duplicate thing",
        source=AgendaSource.SCOUT,
        risk_class=RiskClass.PROPOSE,
        source_event_id="evt_scout_superseded1",
    )
    item = draft.to_item(status=AgendaStatus.UNVETTED)
    store.add(item)
    store.transition(item, AgendaStatus.SUPERSEDED, reason="merged into ag-other", by="kernel")

    fake_store = _FakeMemoryStore()
    app = {"memory_bundle": _FakeBundle(fake_store)}
    monkeypatch.setattr(scout, "build_chain_for_job", lambda *a, **k: [])

    result = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG), app=app))
    assert result.ok is True
    assert fake_store.writes == []
    assert scout._summarize_history([item]) == []


async def test_vetter_cancelled_item_excluded_from_feedback(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path,
) -> None:
    """CANCELLED is reachable from the vetter's own low-value reject
    (``autonomy_vetter.py::_reject``, ``by="kernel"``) as well as a real
    operator rejection (``by="operator"``) — only the operator-driven
    cancellation is real feedback signal."""
    store = AgendaStore()
    draft = AgendaItemDraft(
        goal="scout finding: vet-rejected thing",
        source=AgendaSource.SCOUT,
        risk_class=RiskClass.PROPOSE,
        source_event_id="evt_scout_vetrejected1",
    )
    item = draft.to_item(status=AgendaStatus.UNVETTED)
    store.add(item)
    store.transition(item, AgendaStatus.CANCELLED, reason="vet reject", by="kernel")

    fake_store = _FakeMemoryStore()
    app = {"memory_bundle": _FakeBundle(fake_store)}
    monkeypatch.setattr(scout, "build_chain_for_job", lambda *a, **k: [])

    result = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG), app=app))
    assert result.ok is True
    assert fake_store.writes == []
    assert scout._summarize_history([item]) == []


# ── Test 9: idle short-circuit ──


async def test_idle_when_all_results_already_seen(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, _isolate_bus: AutonomyEventBus,
) -> None:
    _patch_search(monkeypatch, {
        "repeat query": [{"title": "Old news", "url": "https://example.com/old", "content": "x"}],
    })
    seen_path = isolated_home / "autonomy" / "scout-seen.jsonl"
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(
        json.dumps({"key": "https://example.com/old", "ts": "2020-01-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    adapter = _FakeChainAdapter(query_response={"queries": ["repeat query"]}, eval_response={"picks": []})
    _patch_chain(monkeypatch, adapter)

    result = await ScoutJob().run(_ctx(config=dict(_BASE_CONFIG)))

    assert result.ok is True
    assert "idle" in result.detail
    assert len(adapter.calls) == 1
    assert _isolate_bus.peek(AgendaSource.SCOUT) == []


# ── Test 10: no logs pollution ──


