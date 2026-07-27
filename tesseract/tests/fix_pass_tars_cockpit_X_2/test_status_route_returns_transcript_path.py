"""X-2 — ``GET /api/controller_sessions/{id}`` surfaces ``transcript_path``.

The Mirror completion card (``ControllerMirrorBlock.tsx``) renders this
field so the operator can copy the on-disk transcript path after the live
WS has dropped. The path is the value of
``ControllerSessionRecord.transcript_path`` (already under
``<TESSERACT_HOME>/tars_controller/transcripts/``) — no path-escape surface
is introduced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server.routes.controller_sessions import (
    controller_session_status_handler,
)
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get(
        "/api/controller_sessions/{session_id}",
        controller_session_status_handler,
    )
    return app


@pytest.mark.asyncio
async def test_status_response_includes_transcript_path(tmp_path: Path) -> None:
    """The freshly-minted session's transcript path comes back on the wire."""
    rec = SessionRegistry().create_session(
        mode="chat", origin="mirror", title="x-2 surface check"
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/api/controller_sessions/{rec.session_id}")
        assert resp.status == 200
        body = await resp.json()
    assert "transcript_path" in body, (
        "X-2: status route must surface transcript_path so the Mirror "
        "completion card can render the on-disk transcript location."
    )
    assert body["transcript_path"] == rec.transcript_path
    assert body["transcript_path"]  # non-empty
    # Must live under TESSERACT_HOME's controller subtree, not anywhere else.
    assert str(tmp_path) in body["transcript_path"]
    assert "tars_controller" in body["transcript_path"]


@pytest.mark.asyncio
async def test_status_response_shape_unchanged_for_existing_fields(tmp_path: Path) -> None:
    """Additive ``transcript_path`` field must not shift existing keys.
    ControllerMirrorBlock's existing render path relies on
    session_id / status / last_active_at being present."""
    rec = SessionRegistry().create_session(
        mode="chat", origin="mirror", title="shape pin"
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/api/controller_sessions/{rec.session_id}")
        body = await resp.json()
    for required_key in ("session_id", "status", "mode", "origin", "title", "last_active_at"):
        assert required_key in body, f"shape regression: missing {required_key!r}"
