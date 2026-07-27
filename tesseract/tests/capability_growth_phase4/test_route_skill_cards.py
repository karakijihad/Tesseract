"""Phase 4 — Workspace card actions for `skill_approval` + `skill_refinement`.

Mirrors the Stage-10 agent_approval route tests. skill_approval approve runs
the promotion core (pending → active dir move) and lands status `applied`;
reject archives + reason sidecar + comment. skill_refinement approve overwrites
the live SKILL.md with the proposed body; reject leaves it untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceEvent

_SKILL_MD = """---
name: doe-skill
description: John Doe fixture skill for when X happens
---

## Steps
1. do the thing
"""

_PROPOSED_MD = """---
name: doe-skill
description: John Doe fixture skill, revised, for when X happens
---

## Steps
1. do the thing carefully
2. verify
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


def _skills_dir(tmp_path: Path) -> Path:
    return tmp_path / "workspace" / "skills"


# ── skill_approval ───────────────────────────────────────


@pytest.fixture()
def approval_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    skills = _skills_dir(tmp_path)
    pending = skills / "pending" / "doe-skill"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="skill_approval", source="tars",
        title="Skill proposal: doe-skill", summary="why",
        payload={"name": "doe-skill", "description": "d"},
    ))
    return skills, store, ev


async def test_approve_promotes_and_applies(approval_env) -> None:
    skills, store, ev = approval_env
    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    assert _resp_json(resp)["status"] == "applied"
    assert (skills / "doe-skill" / "SKILL.md").exists()
    assert not (skills / "pending" / "doe-skill").exists()


async def test_approve_missing_pending_409_stays_pending(approval_env) -> None:
    skills, store, ev = approval_env
    import shutil

    shutil.rmtree(skills / "pending" / "doe-skill")
    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 409
    assert _resp_json(resp)["error"] == "promote_failed"
    assert store.get_event(ev.event_id).status == "pending"


async def test_reject_archives_with_reason_and_comment(approval_env) -> None:
    skills, store, ev = approval_env
    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "reject", "reason": "too niche"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    assert _resp_json(resp)["status"] == "rejected"
    assert (skills / "rejected" / "doe-skill" / "SKILL.md").exists()
    assert (skills / "rejected" / "doe-skill.reason.txt").read_text(encoding="utf-8").strip() == "too niche"
    assert not (skills / "pending" / "doe-skill").exists()
    thread = store.list_comments(ev.event_id)
    assert len(thread) == 1 and "too niche" in thread[0].body


async def test_missing_name_payload_400(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="skill_approval", source="tars", title="broken",
        summary="no name", payload={},
    ))
    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 400


# ── skill_refinement ─────────────────────────────────────


@pytest.fixture()
def refine_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    skills = _skills_dir(tmp_path)
    active = skills / "doe-skill"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="skill_refinement", source="tars",
        title="Skill needs refinement: doe-skill", summary="3/4 failed",
        payload={"name": "doe-skill", "stats": {"total": 4, "negative": 3},
                 "proposed_markdown": _PROPOSED_MD},
    ))
    return skills, store, ev


async def test_refinement_approve_applies_proposed_body(refine_env) -> None:
    skills, store, ev = refine_env
    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    assert _resp_json(resp)["status"] == "applied"
    assert (skills / "doe-skill" / "SKILL.md").read_text(encoding="utf-8") == _PROPOSED_MD


async def test_refinement_approve_empty_proposal_409(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    skills = _skills_dir(tmp_path)
    (skills / "doe-skill").mkdir(parents=True)
    (skills / "doe-skill" / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    store = EventStore(tmp_path / "logs")
    ev = store.append_event(WorkspaceEvent.new(
        kind="skill_refinement", source="tars", title="flag only", summary="s",
        payload={"name": "doe-skill", "proposed_markdown": ""},
    ))
    req = _StubReq(store, match={"event_id": ev.event_id}, body={"decision": "approve"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 409
    assert _resp_json(resp)["error"] == "skill_refinement_no_proposal"


async def test_refinement_reject_leaves_skill_untouched(refine_env) -> None:
    skills, store, ev = refine_env
    req = _StubReq(store, match={"event_id": ev.event_id},
                   body={"decision": "reject", "reason": "worse"})
    resp = await ws_routes.post_decision(req)
    assert resp.status == 200
    assert _resp_json(resp)["status"] == "rejected"
    # Live skill unchanged.
    assert (skills / "doe-skill" / "SKILL.md").read_text(encoding="utf-8") == _SKILL_MD
    thread = store.list_comments(ev.event_id)
    assert len(thread) == 1 and "worse" in thread[0].body
