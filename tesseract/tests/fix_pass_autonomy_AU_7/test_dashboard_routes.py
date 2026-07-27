"""AU-7 S1 — dashboard read-only route surface.

Three endpoints: ``/api/workers/active`` (writes from
``workers/active/<id>/record.json``), ``/api/governor/state`` (live
flag + config + last tick + pauses), ``/api/recovery/latest`` (last
RecoveryManager pass). Anonymous-readable; mutating actions land in
AU-7 S2 with the operator-session auth gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.governor import (
    Governor,
    GovernorConfig,
    PauseStore,
)
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
    mint_agenda_id,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    RiskClass as WorkerRiskClass,
    WorkerRecord,
    WorkerStatus,
    mint_worker_id,
    write_record,
)


def _make_worker(
    *,
    role: str = "tars_chat",
    kind: WorkerKind = WorkerKind.TARS_SELF,
    status: WorkerStatus = WorkerStatus.RUNNING,
    risk: WorkerRiskClass = WorkerRiskClass.AUTONOMOUS,
    agenda_id: str = "ag-2026-05-18-1200-doe",
    when: datetime | None = None,
) -> WorkerRecord:
    now = when or datetime.now(timezone.utc)
    record = WorkerRecord(
        id=mint_worker_id(kind, now=now),
        kind=kind,
        created_at=now,
        updated_at=now,
        agenda_item_id=agenda_id,
        risk_class=risk,
        role=role,
        status=status,
    )
    write_record(record)
    return record


# -- /api/workers/active --------------------------------------------------


@pytest.mark.asyncio
async def test_workers_active_empty(client: TestClient) -> None:
    resp = await client.get("/api/workers/active")
    assert resp.status == 200
    assert (await resp.json()) == {"workers": []}


@pytest.mark.asyncio
async def test_workers_active_returns_records(
    client: TestClient,
    isolated_home: Path,
) -> None:
    a = _make_worker(role="tars_chat")
    b = _make_worker(role="claude_cli", kind=WorkerKind.CLAUDE_CLI)

    resp = await client.get("/api/workers/active")
    body = await resp.json()
    ids = {w["id"] for w in body["workers"]}
    assert {a.id, b.id}.issubset(ids)
    chat = next(w for w in body["workers"] if w["id"] == a.id)
    assert chat["role"] == "tars_chat"
    assert chat["status"] == "running"
    assert chat["risk_class"] == "autonomous"
    assert chat["agenda_item_id"] == a.agenda_item_id


@pytest.mark.asyncio
async def test_workers_active_sorted_newest_first(client: TestClient) -> None:
    older = _make_worker(
        when=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        role="older",
    )
    newer = _make_worker(
        when=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        role="newer",
    )
    resp = await client.get("/api/workers/active")
    body = await resp.json()
    rows = [w for w in body["workers"] if w["id"] in {older.id, newer.id}]
    assert [w["id"] for w in rows] == [newer.id, older.id]


@pytest.mark.asyncio
async def test_workers_active_includes_last_transition(client: TestClient) -> None:
    rec = _make_worker(status=WorkerStatus.RUNNING)
    rec.transition_to(WorkerStatus.AWAITING_IO, reason="propose_pending")
    write_record(rec)

    resp = await client.get("/api/workers/active")
    body = await resp.json()
    payload = next(w for w in body["workers"] if w["id"] == rec.id)
    assert payload["status"] == "awaiting_io"
    assert payload["last_transition"]["from_status"] == "running"
    assert payload["last_transition"]["to_status"] == "awaiting_io"
    assert payload["last_transition"]["reason"] == "propose_pending"


# -- /api/workers/{id} --------------------------------------------------


@pytest.mark.asyncio
async def test_worker_detail_returns_full_record(
    client: TestClient,
    isolated_home: Path,
) -> None:
    rec = _make_worker(role="tars_chat", kind=WorkerKind.CLAUDE_CLI)
    rec.prompt = "audit recovery summary"
    rec.summary = "found 3 items to address"
    rec.transition_to(WorkerStatus.AWAITING_IO, reason="propose_pending")
    rec.transition_to(WorkerStatus.DONE, reason="completed")
    write_record(rec)

    resp = await client.get(f"/api/workers/{rec.id}")
    assert resp.status == 200
    body = await resp.json()
    payload = body["worker"]
    assert payload["id"] == rec.id
    assert payload["prompt"] == "audit recovery summary"
    assert payload["summary"] == "found 3 items to address"
    # billing default surfaces even on records written without an explicit posture
    assert payload["billing"] == "unknown"
    # Full status_history (not just last_transition)
    transitions = [(t["from_status"], t["to_status"]) for t in payload["status_history"]]
    assert ("running", "awaiting_io") in transitions
    assert ("awaiting_io", "done") in transitions
    # Identity surface fields are present (None values are fine)
    for key in ("pid", "pane_id", "cli_invocation", "transcript_path", "exit_code"):
        assert key in payload


@pytest.mark.asyncio
async def test_worker_detail_404_for_unknown_id(client: TestClient) -> None:
    resp = await client.get("/api/workers/wk-2026-05-23-0000-doesnotexist-abcdef")
    assert resp.status == 404
    body = await resp.json()
    assert "not found" in body["error"]


# -- /api/governor/state --------------------------------------------------


@pytest.mark.asyncio
async def test_governor_state_no_governor_wired(client: TestClient) -> None:
    resp = await client.get("/api/governor/state")
    body = await resp.json()
    assert body["running"] is False
    assert body["last_tick"] is None
    assert body["pauses"] == []
    # Fallback config is the GovernorConfig defaults — not None — so the
    # dashboard can render the values without a separate sentinel branch.
    assert body["config"]["loop_n"] >= 1


@pytest.mark.asyncio
async def test_governor_state_with_running_governor(
    client: TestClient,
    isolated_home: Path,
) -> None:
    agenda_store = AgendaStore()
    pause_store = PauseStore()
    governor = Governor(
        agenda_store=agenda_store,
        pause_store=pause_store,
        config=GovernorConfig(cadence_seconds=900.0, loop_n=4),
    )
    await governor.start()
    try:
        client.app["autonomy_governor"] = governor
        client.app["autonomy_pause_store"] = pause_store
        resp = await client.get("/api/governor/state")
        body = await resp.json()
        assert body["running"] is True
        assert body["config"]["cadence_seconds"] == 900.0
        assert body["config"]["loop_n"] == 4
    finally:
        await governor.stop()


@pytest.mark.asyncio
async def test_governor_state_surfaces_last_tick(
    client: TestClient,
    isolated_home: Path,
) -> None:
    agenda_store = AgendaStore()
    pause_store = PauseStore()
    governor = Governor(
        agenda_store=agenda_store,
        pause_store=pause_store,
        config=GovernorConfig(),
    )
    tick = await governor.run_once()
    client.app["autonomy_governor"] = governor
    client.app["autonomy_pause_store"] = pause_store

    resp = await client.get("/api/governor/state")
    body = await resp.json()
    assert body["last_tick"] is not None
    assert body["last_tick"]["at"] == tick.at.isoformat()
    assert body["last_tick"]["pauses_added"] == []


@pytest.mark.asyncio
async def test_governor_state_surfaces_pauses(
    client: TestClient,
    isolated_home: Path,
) -> None:
    pause_store = PauseStore()
    pause_store.add(
        AgendaSource.SELF_REFLECTION,
        detector="loop",
        reason="loop_detected",
        evidence={"count": 3},
    )
    client.app["autonomy_pause_store"] = pause_store

    resp = await client.get("/api/governor/state")
    body = await resp.json()
    assert len(body["pauses"]) == 1
    assert body["pauses"][0]["source"] == "self_reflection"
    assert body["pauses"][0]["reason"] == "loop_detected"


# -- /api/recovery/latest -------------------------------------------------


class _StubSummary:
    """Minimal stand-in for ``RecoverySummary`` — the route's serializer
    only calls ``to_payload()`` + reads ``started_at``."""

    def __init__(self, payload: dict[str, Any], started_at: datetime) -> None:
        self._payload = payload
        self.started_at = started_at

    def to_payload(self) -> dict[str, Any]:
        return dict(self._payload)


@pytest.mark.asyncio
async def test_recovery_latest_empty(client: TestClient) -> None:
    resp = await client.get("/api/recovery/latest")
    body = await resp.json()
    assert body["recovery"] is None
    assert body["state"] == "ready"


@pytest.mark.asyncio
async def test_recovery_latest_state_recovering(client: TestClient) -> None:
    client.app["recovery_state"] = "recovering"
    resp = await client.get("/api/recovery/latest")
    body = await resp.json()
    assert body["state"] == "recovering"


@pytest.mark.asyncio
async def test_recovery_latest_serializes_summary(client: TestClient) -> None:
    started = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    summary = _StubSummary(
        {
            "boot_id": "boot-abc12345",
            "downtime_seconds": 1.5,
            "scans": {"workers": {"resumed": 1}},
            "operator_attention": [
                {"kind": "agenda", "id": "ag-x", "reason": "blocked"}
            ],
        },
        started_at=started,
    )
    client.app["last_recovery_summary"] = summary
    client.app["recovery_state"] = "ready"

    resp = await client.get("/api/recovery/latest")
    body = await resp.json()
    rec = body["recovery"]
    assert rec["boot_id"] == "boot-abc12345"
    assert rec["downtime_seconds"] == 1.5
    assert rec["scans"] == {"workers": {"resumed": 1}}
    assert rec["operator_attention"][0]["reason"] == "blocked"
    assert rec["started_at"] == started.isoformat()


@pytest.mark.asyncio
async def test_recovery_latest_handles_to_payload_failure(
    client: TestClient,
) -> None:
    class _Broken:
        started_at = datetime(2026, 5, 18, tzinfo=timezone.utc)

        def to_payload(self) -> dict[str, Any]:
            raise RuntimeError("boom")

    client.app["last_recovery_summary"] = _Broken()
    resp = await client.get("/api/recovery/latest")
    body = await resp.json()
    assert body["recovery"] is None
    assert body["state"] == "ready"


# -- operator_attention live-reconciliation ------------------------------


def _make_agenda_item(
    *,
    item_id: str,
    status: AgendaStatus = AgendaStatus.AWAITING_OPERATOR,
) -> AgendaItem:
    now = datetime(2026, 5, 23, tzinfo=timezone.utc)
    return AgendaItem(
        id=item_id,
        created_at=now,
        updated_at=now,
        source=AgendaSource.STRATEGIST,
        goal="doe",
        risk_class=RiskClass.OPERATOR_GATE,
        status=status,
    )


@pytest.mark.asyncio
async def test_recovery_attention_drops_stale_agenda_items(
    client: TestClient,
) -> None:
    """The boot-time snapshot lists agenda items needing operator
    attention, but those items move on after boot. The serializer must
    reconcile against the live agenda store so cleared items no longer
    surface in the RecoveryPane card."""
    store = AgendaStore()
    alive = _make_agenda_item(item_id="ag-2026-05-23-0823-alive")
    cleared = _make_agenda_item(
        item_id="ag-2026-05-23-0823-cleared", status=AgendaStatus.PROPOSED
    )
    store.save(alive)
    store.save(cleared)
    client.app["agenda_store"] = store

    summary = _StubSummary(
        {
            "boot_id": "boot-recon",
            "downtime_seconds": 0.0,
            "scans": {},
            "operator_attention": [
                {"kind": "agenda", "id": alive.id, "reason": "blocked"},
                {"kind": "agenda", "id": cleared.id, "reason": "blocked"},
                {"kind": "agenda", "id": "ag-vanished", "reason": "blocked"},
                {"kind": "mission", "id": "ms-1", "reason": "interrupted"},
            ],
        },
        started_at=datetime(2026, 5, 23, 8, 0, tzinfo=timezone.utc),
    )
    client.app["last_recovery_summary"] = summary

    resp = await client.get("/api/recovery/latest")
    body = await resp.json()
    attention = body["recovery"]["operator_attention"]
    kinds_and_ids = {(e["kind"], e["id"]) for e in attention}
    assert kinds_and_ids == {
        ("agenda", alive.id),
        ("mission", "ms-1"),
    }


@pytest.mark.asyncio
async def test_recovery_attention_passes_through_without_store(
    client: TestClient,
) -> None:
    """Backwards compat: when no ``agenda_store`` is wired on the app
    the serializer must not silently drop entries — it should pass the
    raw boot snapshot through."""
    summary = _StubSummary(
        {
            "boot_id": "boot-x",
            "downtime_seconds": 0.0,
            "scans": {},
            "operator_attention": [
                {"kind": "agenda", "id": "ag-x", "reason": "blocked"}
            ],
        },
        started_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    client.app["last_recovery_summary"] = summary

    resp = await client.get("/api/recovery/latest")
    body = await resp.json()
    assert body["recovery"]["operator_attention"] == [
        {"kind": "agenda", "id": "ag-x", "reason": "blocked"}
    ]


# -- helper used by the agenda item to ensure source enum stays in sync ---


def _seed_item() -> AgendaItem:
    """Drive a coverage check that the enums the router relies on are
    importable — tightening AgendaSource without updating the router
    would regress dashboard rendering silently."""
    now = datetime(2026, 5, 18, tzinfo=timezone.utc)
    return AgendaItem(
        id=mint_agenda_id("doe", now=now),
        created_at=now,
        updated_at=now,
        source=AgendaSource.SELF_REFLECTION,
        goal="doe",
        risk_class=RiskClass.PROPOSE,
        status=AgendaStatus.PROPOSED,
    )


def test_agenda_imports_smoke() -> None:
    item = _seed_item()
    assert item.source is AgendaSource.SELF_REFLECTION
    assert item.risk_class is RiskClass.PROPOSE
