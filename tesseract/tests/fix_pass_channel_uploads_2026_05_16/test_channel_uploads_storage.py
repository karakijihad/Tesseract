"""Session 1 (2026-05-16) — inbound channel media persistence.

Covers :mod:`tesseract.integrations._channel_uploads`:

1. ``save_channel_attachment`` writes bytes at the expected
   ``uploads/channels/<channel>/<chat_id>/<YYYY-MM-DD>/<message_id>/<bucket>/<filename>``
   layout and updates the per-chat index newest-first.
2. Missing filename + missing mime synthesises a safe filename with the
   right extension from kind defaults.
3. Path traversal in chat_id is neutralised.
4. ``load_channel_index`` round-trips the metadata that was written.
5. ``resolve_storage_path`` returns ``None`` for paths outside the
   uploads root (guard against tampered indexes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.integrations._channel_uploads import (
    load_channel_index,
    resolve_storage_path,
    save_channel_attachment,
)


@pytest.mark.asyncio
async def test_save_writes_bytes_and_index_at_canonical_layout(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    rec = await save_channel_attachment(
        channel="telegram",
        chat_id="99",
        message_id="7",
        kind="photo",
        data=b"\xff\xd8\xff\xe0jpegbytes",
        filename="kitten.jpg",
        mime_type="image/jpeg",
        source_ref="AgACAg-photoref",
        caption="cute kitten",
    )

    expected_root = tmp_path / "uploads" / "channels" / "telegram" / "99"
    assert expected_root.exists(), "channel/chat dir was not created"

    saved = tmp_path / "uploads" / "channels" / Path(rec.storage_path)
    assert saved.exists(), f"saved bytes missing at {saved}"
    assert saved.read_bytes() == b"\xff\xd8\xff\xe0jpegbytes"

    # Layout: <channel>/<chat_id>/<date>/<message_id>/<bucket>/<filename>
    parts = rec.storage_path.split("/")
    assert parts[0] == "telegram"
    assert parts[1] == "99"
    assert parts[3] == "7"  # message_id
    assert parts[4] == "image"  # bucket (photo→image)
    assert parts[5] == "kitten.jpg"

    # Index lives under uploads/channels/_index/<channel>/<chat_id>.json
    index_path = tmp_path / "uploads" / "channels" / "_index" / "telegram" / "99.json"
    assert index_path.exists()
    entries = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["channel"] == "telegram"
    assert entry["chat_id"] == "99"
    assert entry["message_id"] == "7"
    assert entry["kind"] == "photo"
    assert entry["filename"] == "kitten.jpg"
    assert entry["mime_type"] == "image/jpeg"
    assert entry["size"] == len(b"\xff\xd8\xff\xe0jpegbytes")
    assert entry["source_ref"] == "AgACAg-photoref"
    assert entry["caption"] == "cute kitten"


@pytest.mark.asyncio
async def test_index_is_newest_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    first = await save_channel_attachment(
        channel="telegram",
        chat_id="99",
        message_id="1",
        kind="voice",
        data=b"voice-1",
        filename="v1.ogg",
        mime_type="audio/ogg",
    )
    second = await save_channel_attachment(
        channel="telegram",
        chat_id="99",
        message_id="2",
        kind="voice",
        data=b"voice-2-bigger",
        filename="v2.ogg",
        mime_type="audio/ogg",
    )

    entries = load_channel_index("telegram", "99")
    assert len(entries) == 2
    assert entries[0]["message_id"] == "2", "newest entry must be first"
    assert entries[1]["message_id"] == "1"
    # Sanity: both records still resolve.
    assert resolve_storage_path(first.storage_path) is not None
    assert resolve_storage_path(second.storage_path) is not None


@pytest.mark.asyncio
async def test_missing_filename_synthesises_with_kind_default_ext(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    rec = await save_channel_attachment(
        channel="telegram",
        chat_id="99",
        message_id="5",
        kind="voice",
        data=b"OggS\x00voice",
        filename=None,
        mime_type="",  # mime missing too — should fall back to kind default
    )
    assert rec.filename.startswith("voice_")
    assert rec.filename.endswith(".ogg")
    assert rec.mime_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_filename_uses_mime_extension_when_kind_default_missing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    rec = await save_channel_attachment(
        channel="telegram",
        chat_id="99",
        message_id="5",
        kind="document",
        data=b"%PDF-1.4",
        filename=None,
        mime_type="application/pdf",
    )
    assert rec.filename.endswith(".pdf"), rec.filename


@pytest.mark.asyncio
async def test_chat_id_with_path_traversal_is_neutralised(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))

    rec = await save_channel_attachment(
        channel="telegram",
        chat_id="../../escape",
        message_id="../../../etc/passwd",
        kind="document",
        data=b"safe",
        filename="../../../etc/passwd",
        mime_type="text/plain",
    )
    # storage_path must remain rooted under uploads/channels/telegram.
    assert rec.storage_path.startswith("telegram/"), rec.storage_path
    full = tmp_path / "uploads" / "channels" / Path(rec.storage_path)
    assert full.is_relative_to(tmp_path / "uploads" / "channels")
    assert full.exists()


@pytest.mark.asyncio
async def test_resolve_storage_path_rejects_paths_outside_root(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    assert resolve_storage_path("../../../etc/passwd") is None
    assert resolve_storage_path("") is None
    assert resolve_storage_path("does/not/exist.bin") is None
