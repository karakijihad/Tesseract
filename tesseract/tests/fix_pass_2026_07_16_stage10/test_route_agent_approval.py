"""Stage 10 — Workspace card actions for `agent_approval` proposals.

Approve on the card runs the shared promotion core (pending → active +
INDEX row) and lands status `applied` (the yaml_change_proposal
convention: operator approved AND the side-effect ran). Reject archives
the pending file to `agents/rejected/` + writes the reason sidecar
agent_create's dedup reads, and leaves the reason as an operator comment
so the next-turn delivery rail carries it to TARS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceEvent

_AGENT_MD = """---
name: doe-specialist
version: "0.1"
model_role: agents_default
description: John Doe fixture
---

## Role

Fixture stance.
"""


class _StubReq:
    def __init__(self, store: EventStore, *, match=None, body=None):
        self.app = {"workspace_event_store": store}
        self.match_info = match or {}
        self.query = {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _resp_json(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    agents_dir = tmp_path / "agents"
    pending = agents_dir / "pending"
    pending.mkdir(parents=True)
    (pending / "doe-specialist.md").write_text(_AGENT_MD, encoding="utf-8")
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="agent_approval",
        source="tars",
        title="Agent proposal: doe-specialist",
        summary="why",
        payload={"name": "doe-specialist", "model_role": "agents_default"},
    ))
    return agents_dir, store, ev


async def test_approve_promotes_and_applies(env) -> None:
    agents_dir, store, ev = env
    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)

    assert resp.status == 200
    assert _resp_json(resp)["status"] == "applied"
    assert (agents_dir / "doe-specialist.md").exists()
    assert not (agents_dir / "pending" / "doe-specialist.md").exists()
    index = (agents_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "doe-specialist" in index


async def test_approve_missing_pending_409_stays_pending(env) -> None:
    agents_dir, store, ev = env
    (agents_dir / "pending" / "doe-specialist.md").unlink()

    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)

    assert resp.status == 409
    assert _resp_json(resp)["error"] == "promote_failed"
    refreshed = store.get_event(ev.event_id)
    assert refreshed is not None and refreshed.status == "pending"


async def test_reject_archives_with_reason_and_comment(env) -> None:
    agents_dir, store, ev = env
    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "reject", "reason": "too niche for the fleet"})
    resp = await ws_routes.post_decision(req)

    assert resp.status == 200
    data = _resp_json(resp)
    assert data["status"] == "rejected"
    assert data["decided_reason"] == "too niche for the fleet"

    rejected = agents_dir / "rejected"
    assert (rejected / "doe-specialist.md").exists()
    assert (rejected / "doe-specialist.reason.txt").read_text(
        encoding="utf-8",
    ) == "too niche for the fleet"
    assert not (agents_dir / "pending" / "doe-specialist.md").exists()
    assert not (agents_dir / "doe-specialist.md").exists()

    thread = store.list_comments(ev.event_id)
    assert len(thread) == 1
    assert thread[0].author == "operator"
    assert "too niche for the fleet" in thread[0].body


async def test_reject_without_reason_still_archives(env) -> None:
    agents_dir, store, ev = env
    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "reject"})
    resp = await ws_routes.post_decision(req)

    assert resp.status == 200
    assert (agents_dir / "rejected" / "doe-specialist.md").exists()
    assert not (agents_dir / "rejected" / "doe-specialist.reason.txt").exists()
    thread = store.list_comments(ev.event_id)
    assert len(thread) == 1


async def test_missing_name_payload_400(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="agent_approval", source="tars", title="broken",
        summary="no name", payload={},
    ))
    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 400
