"""POST /api/workspace/.../decision — applies vault_raw_ingest_batch on Approve."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes import workspace as ws_routes
from tesseract.workspace_events import EventStore, WorkspaceEvent


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _seed_event(home: Path) -> tuple[EventStore, str, Path]:
    (home / "vault" / "raw" / "20260518").mkdir(parents=True)
    target = home / "vault" / "raw" / "20260518" / "doc.md"
    target.write_text("# title\nbody", encoding="utf-8")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()

    store = EventStore(home / "logs")
    ev = WorkspaceEvent.new(
        kind="vault_raw_ingest_batch",
        source="tars",
        title="Vault inbox",
        summary="1 file",
        payload={
            "files": [
                {
                    "folder": "20260518",
                    "relpath": "20260518/doc.md",
                    "sha256": sha,
                    "size_bytes": target.stat().st_size,
                    "suggested_path": "raw/20260518/doc.md",
                    "ask_reason": "size>50MB",
                    "extractor_preview": "...",
                }
            ],
            "folders": ["20260518"],
            "queued_at": "2026-05-18T04:00:00+00:00",
        },
    )
    store.append_event(ev)
    return store, ev.event_id, target


def _build_app(store: EventStore) -> web.Application:
    app = web.Application()
    app["workspace_event_store"] = store
    app.router.add_post(
        "/api/workspace/event/{event_id}/decision", ws_routes.post_decision,
    )
    app.router.add_get(
        "/api/workspace/event/{event_id}", ws_routes.get_event,
    )
    return app


@pytest_asyncio.fixture
async def approve_client(isolated_home: Path):  # type: ignore[no-untyped-def]
    store, event_id, target = _seed_event(isolated_home)
    server = TestServer(_build_app(store))
    client = TestClient(server)
    await client.start_server()
    try:
        yield client, store, event_id, isolated_home, target
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approve_triggers_apply_ask_batch(approve_client):
    client, store, event_id, home, target = approve_client
    resp = await client.post(
        f"/api/workspace/event/{event_id}/decision",
        json={"decision": "approve"},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["status"] == "applied"
    # Sidecar was written next to the file
    assert (target.parent / "doc.md.meta.yaml").exists()
    # Cursor jsonl has an ASK row with ingest_status=ingested
    cursor = home / "autonomy" / "vault-raw-cursors.jsonl"
    rows = [json.loads(line) for line in cursor.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r["decision"] == "ask" and r["ingest_status"] == "ingested" for r in rows)


@pytest.mark.asyncio
async def test_reject_denies_every_file(approve_client):
    client, store, event_id, home, target = approve_client
    resp = await client.post(
        f"/api/workspace/event/{event_id}/decision",
        json={"decision": "reject"},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["status"] == "rejected"
    cursor = home / "autonomy" / "vault-raw-cursors.jsonl"
    rows = [json.loads(line) for line in cursor.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r["decision"] == "ask" and r["ingest_status"] == "denied" for r in rows)
    # No sidecar should land on reject
    assert not (target.parent / "doc.md.meta.yaml").exists()
