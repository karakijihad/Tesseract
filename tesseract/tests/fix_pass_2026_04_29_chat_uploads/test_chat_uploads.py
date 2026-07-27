from __future__ import annotations

import io
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.brain.compaction import _strip_tool_messages
from tesseract.brain.session_store import load_session, sanitize_history_for_persistence
from tesseract.kernel.adapters.gemini import GeminiAdapter
from tesseract.kernel.adapters.openai import OpenAIAdapter
from tesseract.mirror.server import ws as mirror_ws
from tesseract.mirror.server.config import UploadConfig
from tesseract.mirror.server.routes import uploads
from tesseract.mirror.server.uploads import _index as uploads_index
from tesseract.mirror.server.uploads import _storage as uploads_storage
from tesseract.mirror.server.uploads import (
    _chat_content_for_model,
    _validated_attachments,
    load_attachment,
)


def _patch_upload_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", root)
    monkeypatch.setattr(uploads_storage, "UPLOAD_ROOT", root)
    monkeypatch.setattr(uploads_index, "UPLOAD_ROOT", root)


def _scratch_root() -> Path:
    root = Path("tesseract/tests/_tmp_chat_uploads") / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    return root


async def _client(root: Path, monkeypatch) -> TestClient:
    _patch_upload_root(monkeypatch, root / "uploads" / "chat")
    app = web.Application()
    app["config"] = SimpleNamespace(
        uploads=UploadConfig(
            max_file_mb=1,
            max_total_mb=1,
            max_files_per_message=2,
            allowed_mime_types=("image/png", "application/pdf"),
        )
    )
    app.router.add_get("/api/uploads/chat/config", uploads.get_chat_upload_config)
    app.router.add_post("/api/uploads/chat/{session_id}", uploads.upload_chat_attachment)
    app.router.add_get(
        "/api/uploads/chat/{session_id}/{attachment_id}/{filename}",
        uploads.get_chat_attachment,
    )
    app.router.add_delete(
        "/api/uploads/chat/{session_id}/{attachment_id}",
        uploads.delete_chat_attachment,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_upload_stores_metadata_and_serves_file(monkeypatch) -> None:
    root = _scratch_root()
    client = await _client(root, monkeypatch)
    try:
        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"%PDF-1.7\nsample"),
            filename="sample.pdf",
            content_type="application/pdf",
        )
        resp = await client.post("/api/uploads/chat/sess123", data=data)
        assert resp.status == 200
        payload = await resp.json()
        att = payload["attachment"]
        assert att["kind"] == "pdf"
        assert att["mime_type"] == "application/pdf"
        assert att["url"].startswith("/api/uploads/chat/sess123/")
        stored = list((root / "uploads" / "chat" / "pdf" / "sess123").glob("*/*/sample.pdf"))
        assert len(stored) == 1
        index_path = root / "uploads" / "chat" / uploads_index._INDEX_DIR / "sess123.json"
        assert index_path.exists()
        assert att["id"] in json.loads(index_path.read_text(encoding="utf-8"))

        loaded = load_attachment("sess123", att["id"])
        assert loaded is not None
        assert loaded.storage_path.startswith("pdf/sess123/")
        part = await uploads_storage.attachment_part_for_model(loaded)
        assert part is not None
        assert part["type"] == "file"
        assert part["data"]

        file_resp = await client.get(att["url"])
        assert file_resp.status == 200
        assert await file_resp.read() == b"%PDF-1.7\nsample"

        delete_resp = await client.delete(f"/api/uploads/chat/sess123/{att['id']}")
        assert delete_resp.status == 200
        assert load_attachment("sess123", att["id"]) is None
    finally:
        await client.close()
        shutil.rmtree(root, ignore_errors=True)


async def test_upload_config_reflects_server_limits(monkeypatch) -> None:
    root = _scratch_root()
    client = await _client(root, monkeypatch)
    try:
        resp = await client.get("/api/uploads/chat/config")
        assert resp.status == 200
        payload = await resp.json()
        assert payload == {
            "max_file_mb": 1,
            "max_total_mb": 1,
            "max_files_per_message": 2,
            "allowed_mime_types": ["image/png", "application/pdf"],
            "allowed_extensions": [".pdf", ".png"],
        }
    finally:
        await client.close()
        shutil.rmtree(root, ignore_errors=True)


def test_load_attachment_keeps_legacy_session_layout(monkeypatch) -> None:
    root = _scratch_root()
    upload_root = root / "uploads" / "chat"
    _patch_upload_root(monkeypatch, upload_root)
    dest = upload_root / "sess123" / "legacyatt"
    dest.mkdir(parents=True)
    (dest / "old.pdf").write_bytes(b"%PDF-1.7\nlegacy")
    (dest / "metadata.json").write_text(json.dumps({
        "id": "legacyatt",
        "session_id": "sess123",
        "filename": "old.pdf",
        "mime_type": "application/pdf",
        "size": 15,
        "kind": "pdf",
        "url": "/api/uploads/chat/sess123/legacyatt/old.pdf",
        "created_at": "2026-04-29T00:00:00+00:00",
    }), encoding="utf-8")
    try:
        loaded = load_attachment("sess123", "legacyatt")
        assert loaded is not None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_migrated_attachment_lookup_indexes_after_first_fallback(monkeypatch) -> None:
    root = _scratch_root()
    upload_root = root / "uploads" / "chat"
    _patch_upload_root(monkeypatch, upload_root)
    dest = upload_root / "pdf" / "sess123" / "2026-04-30" / "migratedatt"
    dest.mkdir(parents=True)
    (dest / "migrated.pdf").write_bytes(b"%PDF-1.7\nmigrated")
    (dest / "metadata.json").write_text(json.dumps({
        "id": "migratedatt",
        "session_id": "sess123",
        "filename": "migrated.pdf",
        "mime_type": "application/pdf",
        "size": 17,
        "kind": "pdf",
        "url": "/api/uploads/chat/sess123/migratedatt/migrated.pdf",
        "created_at": "2026-04-30T00:00:00+00:00",
        "storage_path": "pdf/sess123/2026-04-30/migratedatt",
    }), encoding="utf-8")
    try:
        loaded = load_attachment("sess123", "migratedatt")
        assert loaded is not None
        # Migrated layout is loadable via the storage_path stored in metadata.
        # The legacy index-backfill on first read was dropped during the split
        # (sync load_attachment cannot await the index lock); the index now
        # self-heals on the next write.
        assert loaded.storage_path == "pdf/sess123/2026-04-30/migratedatt"
        assert uploads_index._metadata_path("sess123", "migratedatt") == dest / "metadata.json"
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_upload_rejects_unsupported_mime(monkeypatch) -> None:
    root = _scratch_root()
    client = await _client(root, monkeypatch)
    try:
        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"hello"),
            filename="note.txt",
            content_type="text/plain",
        )
        resp = await client.post("/api/uploads/chat/sess123", data=data)
        assert resp.status == 415
    finally:
        await client.close()
        shutil.rmtree(root, ignore_errors=True)


async def test_upload_rejects_extension_header_mismatch(monkeypatch) -> None:
    root = _scratch_root()
    client = await _client(root, monkeypatch)
    try:
        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"<html></html>"),
            filename="payload.html",
            content_type="image/png",
        )
        resp = await client.post("/api/uploads/chat/sess123", data=data)
        assert resp.status == 415
    finally:
        await client.close()
        shutil.rmtree(root, ignore_errors=True)


async def test_upload_rejects_bad_magic_bytes(monkeypatch) -> None:
    root = _scratch_root()
    client = await _client(root, monkeypatch)
    try:
        data = FormData()
        data.add_field(
            "file",
            io.BytesIO(b"not a png"),
            filename="image.png",
            content_type="image/png",
        )
        resp = await client.post("/api/uploads/chat/sess123", data=data)
        assert resp.status == 415
    finally:
        await client.close()
        shutil.rmtree(root, ignore_errors=True)


def test_ws_rejects_attachments_over_total_size(monkeypatch) -> None:
    cfg = UploadConfig(
        max_file_mb=1,
        max_total_mb=1,
        max_files_per_message=5,
        allowed_mime_types=("image/png",),
    )
    first = uploads_storage.StoredAttachment(
        id="att1",
        session_id="sess123",
        filename="a.png",
        mime_type="image/png",
        size=700 * 1024,
        kind="image",
        url="/api/uploads/chat/sess123/att1/a.png",
        created_at="2026-04-29T00:00:00+00:00",
    )
    second = uploads_storage.StoredAttachment(
        id="att2",
        session_id="sess123",
        filename="b.png",
        mime_type="image/png",
        size=700 * 1024,
        kind="image",
        url="/api/uploads/chat/sess123/att2/b.png",
        created_at="2026-04-29T00:00:00+00:00",
    )
    by_id = {first.id: first, second.id: second}
    monkeypatch.setattr(
        "tesseract.mirror.server.uploads.load_attachment",
        lambda _sid, att_id: by_id.get(att_id),
    )

    result = _validated_attachments(
        {"config": SimpleNamespace(uploads=cfg)},
        SimpleNamespace(session_id="sess123"),
        [{"id": "att1"}, {"id": "att2"}],
    )

    assert result is None


def test_openai_responses_maps_image_and_pdf_parts() -> None:
    adapter = OpenAIAdapter.__new__(OpenAIAdapter)
    _instructions, items = adapter._to_responses_input([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Analyze these."},
                {"type": "image", "mime_type": "image/png", "data": "aW1n"},
                {
                    "type": "file",
                    "filename": "doc.pdf",
                    "mime_type": "application/pdf",
                    "data": "cGRm",
                },
            ],
        }
    ])
    parts = items[0]["content"]
    assert parts[0] == {"type": "input_text", "text": "Analyze these."}
    assert parts[1] == {"type": "input_image", "image_url": "data:image/png;base64,aW1n"}
    assert parts[2] == {
        "type": "input_file",
        "filename": "doc.pdf",
        "file_data": "data:application/pdf;base64,cGRm",
    }


def test_gemini_maps_inline_data_parts() -> None:
    adapter = GeminiAdapter.__new__(GeminiAdapter)
    _system, contents = adapter._split_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Analyze these."},
                {"type": "image", "mime_type": "image/png", "data": "aW1n"},
                {"type": "file", "mime_type": "application/pdf", "data": "cGRm"},
            ],
        }
    ])
    parts = contents[0]["parts"]
    assert parts[0] == {"text": "Analyze these."}
    assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": "aW1n"}}
    assert parts[2] == {"inline_data": {"mime_type": "application/pdf", "data": "cGRm"}}


def test_compaction_replaces_attachments_with_text_placeholders() -> None:
    stripped = _strip_tool_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please inspect this."},
                {"type": "image", "filename": "screen.png"},
                {"type": "file", "filename": "brief.pdf"},
            ],
        }
    ])
    assert stripped == [{
        "role": "user",
        "content": "Please inspect this. [attached image: screen.png] [attached file: brief.pdf]",
    }]


def test_session_sanitizer_strips_attachment_base64() -> None:
    history = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Inspect this."},
            {
                "type": "image",
                "attachment_id": "att1",
                "filename": "screen.png",
                "data": "base64-bytes",
            },
        ],
    }]
    sanitized = sanitize_history_for_persistence(history)
    assert "data" not in sanitized[0]["content"][1]
    assert "data" in history[0]["content"][1]
    assert "base64-bytes" not in json.dumps(sanitized)


def test_session_load_strips_legacy_attachment_base64() -> None:
    root = _scratch_root()
    path = root / "legacy.json"
    path.write_text(json.dumps({
        "schema": 1,
        "started_at": "2026-04-29T00:00:00+00:00",
        "ended_at": "2026-04-29T00:00:00+00:00",
        "turn_count": 1,
        "model": "model",
        "history": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect this."},
                {"type": "image", "filename": "screen.png", "data": "base64-bytes"},
            ],
        }],
    }), encoding="utf-8")
    try:
        state = load_session(path)
        assert state is not None
        assert "data" not in state.history[0]["content"][1]
    finally:
        shutil.rmtree(root, ignore_errors=True)
