"""TC-1 — approval-route integration: approving an AWAITING_OPERATOR
item via ``POST /api/agenda/{id}/approve`` writes an ``approval`` row.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _read_today(home: Path) -> list[dict]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = home / "operator_journal" / f"{day}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


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
async def test_approve_writes_journal_approval_row(
    isolated_home: Path,
) -> None:
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import ApprovalGate

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={
                    "session_id": "sess_op",
                    "goal": "doe approval flow",
                    "risk_class": "propose",
                },
            )
        ).json()
        store = AgendaStore()
        item = store.get(created["item"]["id"])
        assert item is not None
        item.approvals_required = [
            ApprovalGate(kind="config_apply", target="mirror.yaml"),
        ]
        store.save(item)

        # Push the item to AWAITING_OPERATOR so the approve route's
        # can_transition branch fires (the journal hook lives there).
        from tesseract.orchestrator.autonomy.models import AgendaStatus
        store.transition(
            item, AgendaStatus.AWAITING_OPERATOR, reason="doe-test"
        )

        resp = await client.post(
            f"/api/agenda/{item.id}/approve",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["transitioned"] is True

        rows = _read_today(isolated_home)
        approvals = [r for r in rows if r["event_type"] == "approval"]
        assert len(approvals) == 1
        row = approvals[0]
        assert row["agenda_item_id"] == item.id
        assert row["summary"] == "doe approval flow"
        assert row["session_id"] == "sess_op"
        assert row["prior_status"] == "awaiting_operator"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_noop_does_not_write_row(
    isolated_home: Path,
) -> None:
    """If no gate is fulfilled and item isn't AWAITING_OPERATOR, the
    route short-circuits as noop and must NOT emit a journal row."""
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import ApprovalGate

    client = await _make_client()
    _inject_operator_session(client.app)
    try:
        created = await (
            await client.post(
                "/api/agenda",
                json={"session_id": "sess_op", "goal": "doe noop"},
            )
        ).json()
        # No gates injected → first /approve has nothing to fulfil and
        # item is still PROPOSED → noop branch.
        store = AgendaStore()
        item = store.get(created["item"]["id"])
        assert item is not None
        # Pre-fulfill so fulfilled_count == 0 hits, and the item stays
        # in PROPOSED (no can_transition path).
        item.approvals_required = [
            ApprovalGate(kind="config_apply", target="x.yaml", fulfilled=True),
        ]
        store.save(item)

        resp = await client.post(
            f"/api/agenda/{item.id}/approve",
            json={"session_id": "sess_op"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["noop"] is True

        rows = _read_today(isolated_home)
        approvals = [r for r in rows if r["event_type"] == "approval"]
        assert approvals == []
    finally:
        await client.close()
