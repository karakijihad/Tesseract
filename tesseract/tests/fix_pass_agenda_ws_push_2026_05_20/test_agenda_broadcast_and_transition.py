"""Operator-reported bug fixes 2026-05-20:

- Fix 2: agenda mutations now broadcast WS envelopes so the Autonomy tab
  refreshes without polling.
- Fix 3: ``POST /api/agenda/{id}/approve`` transitions
  ``AWAITING_OPERATOR`` → ``PROPOSED`` once every approval gate is
  fulfilled. The kernel only iterates PROPOSED items; leaving the item
  in awaiting was the "Approved toast but nothing changes" symptom.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


@pytest.fixture
def captured_broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None]]:
    calls: list[tuple[str, str, str | None]] = []

    async def fake_broadcast(
        app: Any, event_type: str, item: Any, *, prior_status: str | None = None
    ) -> None:
        calls.append((event_type, item.id, prior_status))

    monkeypatch.setattr(
        "tesseract.mirror.server.routes.agenda.broadcast_agenda_event",
        fake_broadcast,
    )
    return calls


async def _make_client() -> TestClient:
    from tesseract.mirror.server.routes import agenda as agenda_route
    app = web.Application()
    app["server_sessions"] = {}
    agenda_route.register(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def _inject_operator_session(app: web.Application, sid: str = "sess_op") -> None:
    fake = SimpleNamespace(
        chat_session=SimpleNamespace(ask_fn=lambda *a, **kw: True),
    )
    app["server_sessions"][sid] = fake


@pytest.mark.asyncio
async def test_create_broadcasts_added(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda",
            json={"session_id": "sess_op", "goal": "x", "risk_class": "propose"},
        )
        assert resp.status == 201
        item_id = (await resp.json())["item"]["id"]
        assert captured_broadcasts == [("agenda_item_added", item_id, None)]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_broadcasts_updated(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "y", "risk_class": "propose"},
            )
        ).json()
        item_id = created["item"]["id"]
        resp = await client.patch(
            f"/api/agenda/{item_id}",
            json={"session_id": "sess_op", "operator_priority": 3},
        )
        assert resp.status == 200
        assert ("agenda_item_updated", item_id, None) in captured_broadcasts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_noop_skips_broadcast(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    """An empty PATCH body returns noop=true; no envelope should fire so
    the inbox doesn't refresh for a non-event."""
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "z", "risk_class": "propose"},
            )
        ).json()
        item_id = created["item"]["id"]
        captured_broadcasts.clear()  # drop the create event
        resp = await client.patch(
            f"/api/agenda/{item_id}",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["noop"] is True
        assert captured_broadcasts == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_broadcasts_transitioned(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "k", "risk_class": "propose"},
            )
        ).json()
        item_id = created["item"]["id"]
        resp = await client.post(
            f"/api/agenda/{item_id}/cancel",
            json={"session_id": "sess_op", "reason": "operator_cancel"},
        )
        assert resp.status == 200
        assert ("agenda_item_transitioned", item_id, "proposed") in captured_broadcasts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_awaiting_operator_transitions_to_proposed(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    """Fix 3 — once all gates are fulfilled on an AWAITING_OPERATOR item,
    approve transitions it back to PROPOSED so the kernel picks it up."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaStatus,
        ApprovalGate,
    )

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "t", "risk_class": "propose"},
            )
        ).json()
        item_id = created["item"]["id"]
        store = AgendaStore()
        item = store.get(item_id)
        assert item is not None
        item.approvals_required = [
            ApprovalGate(kind="config_apply", target="mirror.yaml"),
        ]
        store.transition(
            item,
            AgendaStatus.AWAITING_OPERATOR,
            reason="awaiting_operator_approval",
            by="kernel",
        )
        captured_broadcasts.clear()

        resp = await client.post(
            f"/api/agenda/{item_id}/approve",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["transitioned"] is True
        assert body["fulfilled_count"] == 1
        assert body["item"]["status"] == "proposed"
        assert (
            "agenda_item_transitioned",
            item_id,
            "awaiting_operator",
        ) in captured_broadcasts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_proposed_stays_proposed(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    """An item in PROPOSED (no transition) when its only gate is
    fulfilled should NOT be transitioned again — already PROPOSED."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import ApprovalGate

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "p", "risk_class": "propose"},
            )
        ).json()
        item_id = created["item"]["id"]
        store = AgendaStore()
        item = store.get(item_id)
        assert item is not None
        item.approvals_required = [
            ApprovalGate(kind="config_apply", target="mirror.yaml"),
        ]
        store.save(item)
        captured_broadcasts.clear()

        resp = await client.post(
            f"/api/agenda/{item_id}/approve",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["transitioned"] is False
        assert body["item"]["status"] == "proposed"
        assert ("agenda_item_updated", item_id, None) in captured_broadcasts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_partial_gates_no_transition(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    """If gate_kinds restricts approval to a subset, status stays in
    AWAITING_OPERATOR until every gate is fulfilled."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaStatus,
        ApprovalGate,
    )

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "pp", "risk_class": "propose"},
            )
        ).json()
        item_id = created["item"]["id"]
        store = AgendaStore()
        item = store.get(item_id)
        assert item is not None
        item.approvals_required = [
            ApprovalGate(kind="config_apply", target="mirror.yaml"),
            ApprovalGate(kind="dependency_install", target="httpx"),
        ]
        store.transition(
            item,
            AgendaStatus.AWAITING_OPERATOR,
            reason="awaiting_operator_approval",
            by="kernel",
        )
        captured_broadcasts.clear()

        resp = await client.post(
            f"/api/agenda/{item_id}/approve",
            json={"session_id": "sess_op", "gate_kinds": ["config_apply"]},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["transitioned"] is False
        assert body["item"]["status"] == "awaiting_operator"
        assert ("agenda_item_updated", item_id, None) in captured_broadcasts
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reapprove_fulfilled_awaiting_item_transitions(
    captured_broadcasts: list[tuple[str, str, str | None]],
) -> None:
    """Regression — operator-reported 2026-05-21.

    Legacy items whose gates were fulfilled BEFORE the 2026-05-20
    awaiting→proposed transition fix landed (commit 320109c) stay stuck
    in ``AWAITING_OPERATOR`` forever: subsequent Approve clicks find
    no unfulfilled gates so ``fulfilled_count == 0`` and the old route
    short-circuited with ``noop=True`` BEFORE the transition logic ran.
    Re-approving such an item must release it to ``PROPOSED``."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import (
        AgendaStatus,
        ApprovalGate,
    )

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "stuck", "risk_class": "operator_gate"},
            )
        ).json()
        item_id = created["item"]["id"]
        store = AgendaStore()
        item = store.get(item_id)
        assert item is not None
        # Simulate the stuck legacy shape: gate already marked fulfilled
        # by a prior session, item still in AWAITING_OPERATOR because the
        # old route never transitioned it.
        item.approvals_required = [
            ApprovalGate(
                kind="operator_review",
                target="strategist:stuck",
                fulfilled=True,
                fulfilled_by="legacy_session",
            ),
        ]
        store.transition(
            item,
            AgendaStatus.AWAITING_OPERATOR,
            reason="awaiting_operator_approval",
            by="kernel",
        )
        captured_broadcasts.clear()

        resp = await client.post(
            f"/api/agenda/{item_id}/approve",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["fulfilled_count"] == 0
        assert body["transitioned"] is True
        assert body["item"]["status"] == "proposed"
        assert (
            "agenda_item_transitioned",
            item_id,
            "awaiting_operator",
        ) in captured_broadcasts
    finally:
        await client.close()
