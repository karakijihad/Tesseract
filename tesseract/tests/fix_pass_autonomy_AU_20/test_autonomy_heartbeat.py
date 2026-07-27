"""AU-20 — autonomy heartbeat job + self_reflection mapper.

Covers:
- happy path (one observation → memory write + bus publish + cursor advance)
- idle short-circuit (no events / no memory writes → no model call)
- adapter role unavailable → ok=True, cursor still advances
- schema validation drops malformed observations
- dedup window suppresses repeat observation
- graceful degrade: no memory store still publishes
- graceful degrade: no event store still functions
- cursor advance is monotonic (quiet feed preserves watermark)
- multiple observations all publish (capped at 3)
- mapper turns event into propose-class draft
- mapper drops absolute_deny
- mapper drops empty observation
- mapper preserves goal/rationale/risk
- empty adapter output → no observations, cursor advances
- no logs pollution under tesseract/logs/ from the test run
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy import publishers
from tesseract.orchestrator.autonomy.event_bus import AutonomyEvent, AutonomyEventBus
from tesseract.orchestrator.autonomy.mappers.self_reflection import map as map_self_reflection
from tesseract.orchestrator.autonomy.models import AgendaSource, RiskClass
from tesseract.scheduler.tasks.autonomy_heartbeat import (
    AutonomyHeartbeatJob,
    _dedupe_key,
    _parse_response,
)
from tesseract.scheduler.types import JobContext


_REPO_LOGS = Path(__file__).resolve().parents[3] / "logs"
_LOGS_SNAPSHOT_AT_IMPORT = (
    sorted(p.name for p in _REPO_LOGS.iterdir()) if _REPO_LOGS.exists() else []
)


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeEvent:
    def __init__(self, *, event_id: str, ts: str, kind: str, source: str, title: str, summary: str):
        self.event_id = event_id
        self.ts = ts
        self.kind = kind
        self.source = source
        self.title = title
        self.summary = summary


class _FakeEventStore:
    def __init__(self, events: list[_FakeEvent]):
        self._events = events

    def list_events(self, *, limit: int = 200) -> list[_FakeEvent]:
        # Newest first to mirror EventStore semantics; the job's
        # `since` filter does the slicing.
        return sorted(self._events, key=lambda e: e.ts, reverse=True)[:limit]


class _FakeStore:
    """Captures memory writes without touching disk."""

    def __init__(self):
        self.writes: list[tuple[Any, str, str | None]] = []
        self.fail_next = False

    def write(self, frontmatter, body, *, subdir_override=None, skip_wnts_check=False):
        if self.fail_next:
            self.fail_next = False
            return False
        self.writes.append((frontmatter, body, subdir_override))
        return True


class _FakeBundle:
    def __init__(self, store):
        self.store = store


class _FakeAdapter:
    def __init__(self, output: str = "", raise_exc: Exception | None = None):
        self.output = output
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, Any]] = []

    async def generate(self, prompt: str, options) -> str:  # noqa: D401
        self.calls.append((prompt, options))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.output


class _FakeOptions:
    def __init__(self, provider="fake", model="model"):
        self.provider = provider
        self.model = model


def _make_app(
    *,
    events: list[_FakeEvent] | None = None,
    store: _FakeStore | None = None,
    tesseract_dir: Path | None = None,
    presence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    app: dict[str, Any] = {}
    if events is not None:
        app["workspace_event_store"] = _FakeEventStore(events)
    if store is not None:
        app["memory_bundle"] = _FakeBundle(store)
    if tesseract_dir is not None:
        app["tesseract_dir"] = tesseract_dir
    if presence is not None:
        app["operator_presence"] = presence
    return app


def _writes_jsonl(store_dir: Path, rows: list[dict[str, Any]]) -> None:
    events_dir = store_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    with (events_dir / "writes.jsonl").open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _ctx(
    *,
    app: dict[str, Any],
    fired_at: datetime,
    cursor_path: Path,
    seen_path: Path,
    memory_store_dir: Path | None = None,
) -> JobContext:
    config: dict[str, Any] = {
        "cursor_path": str(cursor_path),
        "seen_path": str(seen_path),
    }
    if memory_store_dir is not None:
        config["memory_store_dir"] = str(memory_store_dir)
    return JobContext(
        job_name="autonomy_heartbeat",
        fired_at=fired_at,
        app=app,
        config=config,
        model_role=None,
    )


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Each test owns its bus and tears it down — no cross-test leakage."""
    bus = AutonomyEventBus()
    publishers.set_active_bus(bus)
    yield bus
    publishers.set_active_bus(None)


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch):
    """Default: a single adapter that returns one valid observation."""
    adapter = _FakeAdapter(output=json.dumps({
        "observations": [
            {
                "observation": "Three docs-watch deltas in the last hour suggest the upstream provider published a breaking change.",
                "suggested_risk_class": "propose",
                "evidence_ids": ["evt_one", "evt_two"],
            }
        ]
    }))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    return adapter


# ── Tests ───────────────────────────────────────────────────────────


async def test_happy_path_writes_memory_and_publishes(
    tmp_path: Path,
    chain: _FakeAdapter,
    _isolate_bus: AutonomyEventBus,
) -> None:
    store = _FakeStore()
    fired_at = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    events = [
        _FakeEvent(
            event_id="evt_one",
            ts="2026-05-19T11:50:00+00:00",
            kind="change_proposal",
            source="orchestrator",
            title="docs delta",
            summary="watchlist source openhuman flagged a delta",
        )
    ]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    _writes_jsonl(tesseract_dir / "memory-store", [{
        "memory_id": "mem_abc",
        "timestamp": "2026-05-19T11:55:00+00:00",
        "type": "project",
        "title": "AU-20 progress",
        "status": "written",
    }])

    app = _make_app(events=events, store=store, tesseract_dir=tesseract_dir)
    ctx = _ctx(
        app=app,
        fired_at=fired_at,
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["events"] == 1
    assert result.payload["memory_writes"] == 1
    assert result.payload["published"] == 1
    assert result.payload["memory_written"] == 1
    assert len(store.writes) == 1
    fm, body, subdir = store.writes[0]
    assert subdir == "conscience/autonomy"
    assert "Three docs-watch deltas" in body
    assert "evidence" in body.lower()
    # Bus carries one event with our minted event_id prefix.
    buffered = _isolate_bus.peek(AgendaSource.SELF_REFLECTION)
    assert len(buffered) == 1
    assert buffered[0].payload["observation"].startswith("Three docs-watch")
    assert buffered[0].event_id.startswith("evt_heartbeat_")
    assert buffered[0].payload["memory_id"] == fm.id


async def test_idle_short_circuits_no_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeAdapter(output="{\"observations\": []}")
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    store = _FakeStore()
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    # No events, no writes.jsonl.
    app = _make_app(events=[], store=store, tesseract_dir=tesseract_dir)
    ctx = _ctx(
        app=app,
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.detail == "idle"
    assert adapter.calls == []
    assert store.writes == []


async def test_role_unavailable_advances_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
) -> None:
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [],
    )
    store = _FakeStore()
    fired_at = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    events = [_FakeEvent(
        event_id="evt_one",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    cursor_path = tmp_path / "cursor.json"
    app = _make_app(events=events, store=store, tesseract_dir=tesseract_dir)
    ctx = _ctx(
        app=app,
        fired_at=fired_at,
        cursor_path=cursor_path,
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.detail == "role_unavailable"
    assert result.payload["observations"] == 0
    # Cursor advanced to the newest event ts even though no observations
    # fired — otherwise we'd loop on the same window every tick.
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor["last_event_ts"] == "2026-05-19T11:50:00+00:00"


async def test_schema_validation_drops_malformed_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
) -> None:
    adapter = _FakeAdapter(output=json.dumps({
        "observations": [
            # Too short — fails min_length=10.
            {"observation": "too short", "suggested_risk_class": "propose"},
            # Valid.
            {"observation": "This is a perfectly fine observation about activity.",
             "suggested_risk_class": "propose"},
        ]
    }))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    store = _FakeStore()
    events = [_FakeEvent(
        event_id="evt_x",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    ctx = _ctx(
        app=_make_app(events=events, store=store, tesseract_dir=tesseract_dir),
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    # Only the valid one survives.
    assert result.payload["observations_accepted"] == 1
    assert len(store.writes) == 1


async def test_dedup_window_suppresses_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
) -> None:
    obs_text = "A repeated observation about a pattern that appeared twice."
    adapter = _FakeAdapter(output=json.dumps({
        "observations": [
            {"observation": obs_text, "suggested_risk_class": "propose"},
            {"observation": obs_text + " ", "suggested_risk_class": "propose"},  # whitespace-equivalent
        ]
    }))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    store = _FakeStore()
    events = [_FakeEvent(
        event_id="evt_x",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    seen_path = tmp_path / "seen.jsonl"
    # Pre-seed the seen ledger with the observation key in-window.
    earlier = datetime(2026, 5, 19, 11, 0, tzinfo=timezone.utc)
    seen_path.write_text(
        json.dumps({"key": _dedupe_key(obs_text), "ts": earlier.isoformat()}) + "\n",
        encoding="utf-8",
    )
    ctx = _ctx(
        app=_make_app(events=events, store=store, tesseract_dir=tesseract_dir),
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=seen_path,
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    # Both items collapse to a prior key → zero accepted.
    assert result.payload["observations_accepted"] == 0
    assert store.writes == []


async def test_publish_without_memory_store(
    tmp_path: Path,
    chain: _FakeAdapter,
    _isolate_bus: AutonomyEventBus,
) -> None:
    events = [_FakeEvent(
        event_id="evt_x",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    # No store key in the app.
    app = _make_app(events=events, tesseract_dir=tesseract_dir)
    ctx = _ctx(
        app=app,
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["published"] == 1
    assert result.payload["memory_written"] == 0
    buffered = _isolate_bus.peek(AgendaSource.SELF_REFLECTION)
    assert len(buffered) == 1
    assert buffered[0].payload["memory_id"] is None


async def test_no_event_store_still_functions(
    tmp_path: Path,
    chain: _FakeAdapter,
    _isolate_bus: AutonomyEventBus,
) -> None:
    """Empty events list (no event store) + recent memory writes still
    drives a model call."""
    store = _FakeStore()
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    _writes_jsonl(tesseract_dir / "memory-store", [{
        "memory_id": "mem_xy",
        "timestamp": "2026-05-19T11:55:00+00:00",
        "type": "feedback",
        "title": "fb item",
        "status": "written",
    }])
    app = {"memory_bundle": _FakeBundle(store), "tesseract_dir": tesseract_dir}
    ctx = _ctx(
        app=app,
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["events"] == 0
    assert result.payload["memory_writes"] == 1
    assert result.payload["published"] == 1


async def test_cursor_preserves_watermark_on_quiet_feed(
    tmp_path: Path,
    chain: _FakeAdapter,
    _isolate_bus: AutonomyEventBus,
) -> None:
    """A tick with no events but with memory writes must not erase the
    last_event_ts watermark — otherwise the next tick replays the entire
    event log."""
    store = _FakeStore()
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    _writes_jsonl(tesseract_dir / "memory-store", [{
        "memory_id": "mem_xy",
        "timestamp": "2026-05-19T11:55:00+00:00",
        "type": "feedback",
        "title": "fb item",
        "status": "written",
    }])
    cursor_path = tmp_path / "cursor.json"
    cursor_path.write_text(json.dumps({
        "last_event_ts": "2026-05-19T10:00:00+00:00",
        "last_memory_ts": "2026-05-19T10:00:00+00:00",
    }), encoding="utf-8")
    app = _make_app(events=[], store=store, tesseract_dir=tesseract_dir)
    ctx = _ctx(
        app=app,
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=cursor_path,
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor["last_event_ts"] == "2026-05-19T10:00:00+00:00"
    assert cursor["last_memory_ts"] == "2026-05-19T11:55:00+00:00"


async def test_self_amplification_filter_skips_own_writes(
    tmp_path: Path,
    chain: _FakeAdapter,
    _isolate_bus: AutonomyEventBus,
) -> None:
    """The heartbeat must not observe its own CONSCIENCE writes — would
    self-amplify into infinite reflection-on-reflection. Memory writes
    whose title starts with ``autonomy heartbeat`` are filtered out at
    input collection time."""
    store = _FakeStore()
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    _writes_jsonl(tesseract_dir / "memory-store", [
        {
            "memory_id": "mem_self_one",
            "timestamp": "2026-05-19T11:55:00+00:00",
            "type": "conscience",
            "title": "autonomy heartbeat — Three docs deltas observed",
            "status": "written",
        },
        {
            "memory_id": "mem_self_two",
            "timestamp": "2026-05-19T11:56:00+00:00",
            "type": "conscience",
            "title": "autonomy heartbeat — Pattern X detected",
            "status": "written",
        },
        # A non-heartbeat write that SHOULD surface as input.
        {
            "memory_id": "mem_real",
            "timestamp": "2026-05-19T11:57:00+00:00",
            "type": "project",
            "title": "real operator-relevant write",
            "status": "written",
        },
    ])
    app = _make_app(events=[], store=store, tesseract_dir=tesseract_dir)
    ctx = _ctx(
        app=app,
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    # Only the non-heartbeat write is visible — 2 self-writes filtered.
    assert result.payload["memory_writes"] == 1


async def test_caps_at_three_observations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
) -> None:
    payload = {"observations": [
        {"observation": f"Observation number {i} about activity #{i}.",
         "suggested_risk_class": "propose"}
        for i in range(5)
    ]}
    adapter = _FakeAdapter(output=json.dumps(payload))
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    store = _FakeStore()
    events = [_FakeEvent(
        event_id="evt_x",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    ctx = _ctx(
        app=_make_app(events=events, store=store, tesseract_dir=tesseract_dir),
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["observations_returned"] == 5
    assert result.payload["observations_accepted"] == 3
    assert len(_isolate_bus.peek(AgendaSource.SELF_REFLECTION)) == 3


async def test_empty_adapter_output_advances_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
) -> None:
    adapter = _FakeAdapter(output="")
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.autonomy_heartbeat.build_chain_for_job",
        lambda *a, **k: [(adapter, _FakeOptions())],
    )
    store = _FakeStore()
    events = [_FakeEvent(
        event_id="evt_x",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    cursor_path = tmp_path / "cursor.json"
    ctx = _ctx(
        app=_make_app(events=events, store=store, tesseract_dir=tesseract_dir),
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=cursor_path,
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["observations_accepted"] == 0
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor["last_event_ts"] == "2026-05-19T11:50:00+00:00"


async def test_no_bus_attached_does_not_crash(
    tmp_path: Path,
    chain: _FakeAdapter,
) -> None:
    """When no bus is registered, publish_to_bus is a no-op — the job
    must still write memory and report ok."""
    publishers.set_active_bus(None)
    store = _FakeStore()
    events = [_FakeEvent(
        event_id="evt_x",
        ts="2026-05-19T11:50:00+00:00",
        kind="nudge",
        source="orchestrator",
        title="x",
        summary="y",
    )]
    tesseract_dir = tmp_path / "tess"
    tesseract_dir.mkdir()
    ctx = _ctx(
        app=_make_app(events=events, store=store, tesseract_dir=tesseract_dir),
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        cursor_path=tmp_path / "cursor.json",
        seen_path=tmp_path / "seen.jsonl",
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["memory_written"] == 1


# ── Mapper tests ────────────────────────────────────────────────────


def _make_event(payload: dict[str, Any]) -> AutonomyEvent:
    return AutonomyEvent.make(AgendaSource.SELF_REFLECTION, payload, event_id="evt_t")


def test_mapper_turns_observation_into_propose_draft() -> None:
    event = _make_event({
        "observation": "Three identical errors in the last hour suggest a regression.",
        "suggested_risk_class": "propose",
        "evidence_ids": ["evt_a", "evt_b"],
        "memory_id": "mem_x",
    })
    drafts = map_self_reflection(event)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.source is AgendaSource.SELF_REFLECTION
    assert d.risk_class is RiskClass.PROPOSE
    assert d.source_event_id == "evt_t"
    assert "Three identical errors" in d.goal
    assert "memory_id=mem_x" in d.rationale
    assert "evt_a, evt_b" in d.rationale


def test_mapper_drops_absolute_deny() -> None:
    event = _make_event({
        "observation": "Should rm -rf the world (heartbeat must not produce this).",
        "suggested_risk_class": "absolute_deny",
    })
    assert map_self_reflection(event) == []


def test_mapper_drops_empty_observation() -> None:
    event = _make_event({"observation": "", "suggested_risk_class": "propose"})
    assert map_self_reflection(event) == []


def test_mapper_unknown_risk_falls_back_to_propose() -> None:
    event = _make_event({
        "observation": "An observation about activity worth noting.",
        "suggested_risk_class": "nonsense_value",
    })
    drafts = map_self_reflection(event)
    assert len(drafts) == 1
    assert drafts[0].risk_class is RiskClass.PROPOSE


# ── Parse helper ────────────────────────────────────────────────────


def test_parse_response_handles_garbage() -> None:
    """Embedded JSON inside prose still parses; pure garbage returns empty."""
    raw = "Sure, here:\n{\"observations\":[{\"observation\":\"valid one here please.\",\"suggested_risk_class\":\"autonomous\"}]} done"
    parsed = _parse_response(raw)
    assert len(parsed.observations) == 1
    assert parsed.observations[0].suggested_risk_class == "autonomous"

    parsed_empty = _parse_response("not json at all")
    assert parsed_empty.observations == []


async def test_default_paths_resolve_under_tesseract_home_env(
    tmp_path: Path,
    chain: _FakeAdapter,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_bus: AutonomyEventBus,
) -> None:
    """Phase-gate regression: cursor / seen / memory_store_dir must all
    resolve via the call-time ``TESSERACT_HOME`` env override when no
    config keys are set, so the autouse ``_isolate_tesseract_home``
    fixture cannot leak writes into the production tree.
    """
    home = tmp_path / "tesseract_home"
    home.mkdir()
    monkeypatch.setenv("TESSERACT_HOME", str(home))

    # No tesseract_dir, no memory_store_dir override — exercises every
    # path-resolution fallback at once. Pre-populate writes.jsonl so the
    # job goes through the full happy path against the env-resolved
    # memory-store dir.
    (home / "memory-store" / "events").mkdir(parents=True)
    (home / "memory-store" / "events" / "writes.jsonl").write_text(
        json.dumps({
            "memory_id": "mem_env",
            "timestamp": "2026-05-19T11:55:00+00:00",
            "type": "project",
            "title": "env-routed write",
            "status": "written",
        }) + "\n",
        encoding="utf-8",
    )
    store = _FakeStore()
    app = {"memory_bundle": _FakeBundle(store)}  # no tesseract_dir
    ctx = JobContext(
        job_name="autonomy_heartbeat",
        fired_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        app=app,
        config={},  # no overrides — exercise the env fallback
        model_role=None,
    )
    result = await AutonomyHeartbeatJob().run(ctx)
    assert result.ok is True
    # Cursor + seen landed under the env-resolved home (not the
    # production tree).
    assert (home / "autonomy" / "heartbeat-cursor.json").exists()
    assert (home / "autonomy" / "heartbeat-seen.jsonl").exists()
    # Memory write went through (one observation, one write).
    assert len(store.writes) == 1


def test_repo_logs_untouched_after_suite() -> None:
    """Asserts no `tesseract/logs/**` rows accrue from this test file.

    Snapshot is captured at module import; this test fires last in
    collection order to compare. Pollution = a regression in fixture
    isolation, not a heartbeat bug per se, but the heartbeat path is the
    one most likely to write outside its sandbox."""
    repo_logs = Path(__file__).resolve().parents[3] / "logs"
    if not repo_logs.exists():
        return
    current = sorted(p.name for p in repo_logs.iterdir())
    # Compare against the snapshot taken at module import time.
    assert current == _LOGS_SNAPSHOT_AT_IMPORT, (
        f"tesseract/logs/ changed during test run. "
        f"before={_LOGS_SNAPSHOT_AT_IMPORT} after={current}"
    )
