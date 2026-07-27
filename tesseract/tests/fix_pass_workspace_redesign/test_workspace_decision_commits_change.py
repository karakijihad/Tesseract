"""POST /api/workspace/.../decision — commits change_proposal on Approve."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.kernel.workspace_changes import compute_diff, hash_text, preview_change
from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceEvent


def _seed_workspace() -> None:
    from tesseract.paths import workspace_dir
    ws = workspace_dir()
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text(
        "# Soul\n\n## Growth\n\n*Currently empty.*\n", encoding="utf-8",
    )


def _build_app(tmp_path: Path) -> tuple[web.Application, EventStore, str]:
    from tesseract.paths import workspace_dir
    _seed_workspace()
    store = EventStore(tmp_path / "logs")
    body = (workspace_dir() / "SOUL.md").read_text(encoding="utf-8")
    after = preview_change(
        current_text=body,
        action="append_to_section",
        content="- a stable bullet\n",
        section="Growth",
    )
    ev = WorkspaceEvent.new(
        kind="change_proposal",
        source="tars",
        title="Soul growth bullet — testing",
        summary="testing",
        payload={
            "target_path": "tesseract/workspace/SOUL.md",
            "label": "Soul",
            "action": "append_to_section",
            "content": "- a stable bullet\n",
            "section": "Growth",
            "summary": "testing",
            "expected_hash_before": hash_text(body),
            "bytes_before": len(body.encode("utf-8")),
            "bytes_after": len(after.encode("utf-8")),
            "diff": compute_diff(body, after, target_label="Soul"),
            "kind_origin": "soul_growth",
        },
    )
    store.append_event(ev)

    app = web.Application()
    app["workspace_event_store"] = store
    app.router.add_post(
        "/api/workspace/event/{event_id}/decision", ws_routes.post_decision,
    )
    app.router.add_get(
        "/api/workspace/event/{event_id}", ws_routes.get_event,
    )
    return app, store, ev.event_id


@pytest_asyncio.fixture
async def client_factory(tmp_path: Path, monkeypatch):  # type: ignore[no-untyped-def]
    home = tmp_path / "home"
    home.mkdir()
    # apply_change resolves workspace targets via workspace_dir(), which
    # reads TESSERACT_HOME at call time.
    monkeypatch.setenv("TESSERACT_HOME", str(home))
    app, store, event_id = _build_app(tmp_path)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, store, event_id, home
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_commits_to_target_file(client_factory):
    client, store, event_id, home = client_factory
    resp = await client.post(
        f"/api/workspace/event/{event_id}/decision",
        json={"decision": "approve"},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["status"] == "approved"
    soul_after = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert "- a stable bullet" in soul_after
    assert "*Currently empty" not in soul_after


@pytest.mark.asyncio
async def test_reject_leaves_file_untouched(client_factory):
    client, store, event_id, home = client_factory
    before = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    resp = await client.post(
        f"/api/workspace/event/{event_id}/decision",
        json={"decision": "reject", "reason": "wrong wording"},
    )
    assert resp.status == 200
    after = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert after == before
    body = await resp.json()
    assert body["status"] == "rejected"
    assert body["decided_reason"] == "wrong wording"


@pytest.mark.asyncio
async def test_double_approve_is_idempotent(client_factory):
    client, store, event_id, home = client_factory
    r1 = await client.post(
        f"/api/workspace/event/{event_id}/decision", json={"decision": "approve"},
    )
    assert r1.status == 200
    soul_first = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    r2 = await client.post(
        f"/api/workspace/event/{event_id}/decision", json={"decision": "approve"},
    )
    assert r2.status == 200, "second decision must be no-op (settled)"
    soul_second = (home / "workspace" / "SOUL.md").read_text(encoding="utf-8")
    assert soul_first == soul_second, "second approve must not double-append"


@pytest.mark.asyncio
async def test_concurrent_modification_returns_409(client_factory):
    client, store, event_id, home = client_factory
    # Simulate a concurrent operator edit between propose and approve.
    soul_path = home / "workspace" / "SOUL.md"
    soul_path.write_text(soul_path.read_text(encoding="utf-8") + "\nmanual edit\n", encoding="utf-8")
    resp = await client.post(
        f"/api/workspace/event/{event_id}/decision", json={"decision": "approve"},
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["error"] == "concurrent_modification"
    assert body.get("fresh_diff") is not None
    assert body.get("fresh_expected_hash_before")
    assert body["expected_hash_before"] != body["actual_hash"]


@pytest.mark.asyncio
async def test_approve_unknown_event_returns_404(client_factory):
    client, *_ = client_factory
    resp = await client.post(
        "/api/workspace/event/evt_doesnotexist/decision",
        json={"decision": "approve"},
    )
    assert resp.status == 404
