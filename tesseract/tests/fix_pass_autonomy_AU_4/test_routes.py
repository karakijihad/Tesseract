"""AU-4 S2 — /api/agenda REST endpoints + operator-session auth.

Mirrors the AU-1 runtime-routes fixture pattern: a per-test aiohttp app
with the agenda routes registered and a fake operator session injected
into ``app["server_sessions"]`` so the auth gate accepts mutating calls.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    return tmp_path


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
async def test_get_empty_list(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/agenda")
        assert resp.status == 200
        assert (await resp.json())["items"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_anonymous_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.post("/api/agenda", json={"goal": "x"})
        assert resp.status == 401
        body = await resp.json()
        assert "session_id required" in body["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_unknown_session_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.post(
            "/api/agenda", json={"session_id": "stranger", "goal": "x"},
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_round_trip(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda",
            json={
                "session_id": "sess_op",
                "goal": "audit doe flow",
                "rationale": "operator suspected drift",
                "risk_class": "propose",
                "operator_priority": 3,
            },
        )
        assert resp.status == 201
        body = await resp.json()
        assert body["deduped"] is False
        item = body["item"]
        assert item["goal"] == "audit doe flow"
        assert item["risk_class"] == "propose"
        assert item["operator_priority"] == 3
        # Re-fetch via GET.
        listing = await (await client.get("/api/agenda")).json()
        assert len(listing["items"]) == 1
        assert listing["items"][0]["id"] == item["id"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_dedupes_identical_goal(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        first = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "audit doe flow"},
            )
        ).json()
        second_resp = await client.post(
            "/api/agenda",
            json={"session_id": "sess_op", "goal": "AUDIT  DOE  FLOW"},
        )
        assert second_resp.status == 200
        second = await second_resp.json()
        assert second["deduped"] is True
        assert second["item"]["id"] == first["item"]["id"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_invalid_risk_class(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda",
            json={"session_id": "sess_op", "goal": "x", "risk_class": "made_up"},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_priority_out_of_range(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda",
            json={"session_id": "sess_op", "goal": "x", "operator_priority": 99},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_priority_then_rerank(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        a = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "a", "operator_priority": 0},
            )
        ).json()
        b = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "b", "operator_priority": 0},
            )
        ).json()
        # Bump b's priority — it should overtake a in the ranked list.
        await client.patch(
            f"/api/agenda/{b['item']['id']}",
            json={"session_id": "sess_op", "operator_priority": 5},
        )
        listing = (await (await client.get("/api/agenda")).json())["items"]
        assert listing[0]["id"] == b["item"]["id"]
        assert listing[1]["id"] == a["item"]["id"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_anonymous_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda", json={"session_id": "sess_op", "goal": "x"},
            )
        ).json()
        resp = await client.patch(
            f"/api/agenda/{created['item']['id']}", json={"operator_priority": 4},
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_patch_refuses_absolute_deny(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda", json={"session_id": "sess_op", "goal": "x"},
            )
        ).json()
        resp = await client.patch(
            f"/api/agenda/{created['item']['id']}",
            json={"session_id": "sess_op", "risk_class": "absolute_deny"},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_transitions_to_cancelled(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda", json={"session_id": "sess_op", "goal": "x"},
            )
        ).json()
        item_id = created["item"]["id"]
        resp = await client.post(
            f"/api/agenda/{item_id}/cancel",
            json={"session_id": "sess_op", "reason": "operator_cancel"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["item"]["status"] == "cancelled"
        # The item is now archived → list_items returns no actives.
        listing = (await (await client.get("/api/agenda")).json())["items"]
        assert listing == []
        # GET by id finds it in archive.
        fetched = await (await client.get(f"/api/agenda/{item_id}")).json()
        assert fetched["status"] == "cancelled"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_anonymous_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda", json={"session_id": "sess_op", "goal": "x"},
            )
        ).json()
        resp = await client.post(
            f"/api/agenda/{created['item']['id']}/cancel", json={},
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_fulfills_gates(_isolated_home: Path) -> None:
    """Operator approving an item with two pending gates fulfils both
    (no gate_kinds filter) and sets fulfilled_by to the session id."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import ApprovalGate

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={
                    "session_id": "sess_op", "goal": "x", "risk_class": "propose",
                },
            )
        ).json()
        # Inject pending gates directly (the REST surface doesn't allow
        # creating gates yet — AU-5 will when mappers land).
        store = AgendaStore()
        item = store.get(created["item"]["id"])
        assert item is not None
        item.approvals_required = [
            ApprovalGate(kind="config_apply", target="mirror.yaml"),
            ApprovalGate(kind="dependency_install", target="httpx"),
        ]
        store.save(item)

        resp = await client.post(
            f"/api/agenda/{item.id}/approve",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["fulfilled_count"] == 2
        for gate in body["item"]["approvals_required"]:
            assert gate["fulfilled"] is True
            assert gate["fulfilled_by"] == "sess_op"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_blocked_item_goes_to_proposed(_isolated_home: Path) -> None:
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import AgendaStatus

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "x", "risk_class": "propose"},
            )
        ).json()
        store = AgendaStore()
        item = store.get(created["item"]["id"])
        assert item is not None
        item.blocked_reason = "worker_blocked:wk-x"
        store.transition(item, AgendaStatus.BLOCKED, reason="test_setup", by="kernel")

        resp = await client.post(
            f"/api/agenda/{item.id}/resume",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["transitioned"] is True
        assert body["item"]["status"] == "proposed"
        assert body["item"]["blocked_reason"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_on_non_blocked_is_noop(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "x", "risk_class": "propose"},
            )
        ).json()
        resp = await client.post(
            f"/api/agenda/{created['item']['id']}/resume",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["noop"] is True
        assert body["status"] == "proposed"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_terminal_item_rejected(_isolated_home: Path) -> None:
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import AgendaStatus

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "x", "risk_class": "propose"},
            )
        ).json()
        store = AgendaStore()
        item = store.get(created["item"]["id"])
        assert item is not None
        store.transition(item, AgendaStatus.CANCELLED, reason="op_cancel", by="operator")

        resp = await client.post(
            f"/api/agenda/{item.id}/resume",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_unknown_item_404(_isolated_home: Path) -> None:
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda/ag-2026-05-23-1200-doesnotexist/resume",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_anonymous_rejected(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.post(
            "/api/agenda/ag-x/resume",
            json={},
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_post_rejects_absolute_deny(_isolated_home: Path) -> None:
    """REST layer: POST with risk_class=absolute_deny → 400. The store
    refuses admission in add(); the route surfaces the ValueError as a
    400 instead of letting it bubble as 500."""
    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        resp = await client.post(
            "/api/agenda",
            json={"session_id": "sess_op", "goal": "x", "risk_class": "absolute_deny"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert "absolute_deny" in body["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_404_for_unknown_id(_isolated_home: Path) -> None:
    client = await _make_client()
    try:
        resp = await client.get("/api/agenda/ag-never-existed")
        assert resp.status == 404
    finally:
        await client.close()
