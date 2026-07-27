"""Regression tests for uploads refactor (2026-04-30).

Covers:
  - _safe_segment allowlist (m6)
  - Concurrent uploads to the same session do not corrupt _index.json (C4)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tesseract.mirror.server.uploads._validation import _safe_segment
from tesseract.mirror.server.uploads._index import (
    _index_attachment,
    _unindex_attachment,
    _read_index,
    _index_path,
)
from tesseract.mirror.server.uploads._storage import StoredAttachment, UPLOAD_ROOT


# ── _safe_segment allowlist ──────────────────────────────────────────────────


def test_safe_segment_rejects_slash():
    assert _safe_segment("a/b") == ""


def test_safe_segment_rejects_dot():
    assert _safe_segment("a.b") == ""


def test_safe_segment_rejects_empty():
    assert _safe_segment("") == ""


def test_safe_segment_rejects_too_long():
    assert _safe_segment("a" * 200) == ""


def test_safe_segment_accepts_uuid_hex():
    val = uuid.uuid4().hex
    assert _safe_segment(val) == val


def test_safe_segment_accepts_underscores_and_dashes():
    assert _safe_segment("hello-world_123") == "hello-world_123"


# ── Concurrent index writes do not corrupt _index.json ──────────────────────


@pytest.mark.asyncio
async def test_concurrent_index_writes_no_corruption(tmp_path: Path):
    session_id = uuid.uuid4().hex

    def fake_index_path(sid: str) -> Path:
        return tmp_path / f"{sid}.json"

    attachments = [
        StoredAttachment(
            id=uuid.uuid4().hex,
            session_id=session_id,
            filename=f"file{i}.png",
            mime_type="image/png",
            size=100,
            kind="image",
            url=f"/uploads/{i}",
            created_at="2026-04-30T00:00:00+00:00",
            storage_path=f"image/{session_id}/2026-04-30/{i}",
        )
        for i in range(10)
    ]

    with (
        patch(
            "tesseract.mirror.server.uploads._index._index_path",
            side_effect=fake_index_path,
        ),
        patch(
            "tesseract.mirror.server.uploads._index._is_within_upload_root",
            return_value=True,
        ),
    ):
        await asyncio.gather(*[_index_attachment(att) for att in attachments])

        index = json.loads((tmp_path / f"{session_id}.json").read_text(encoding="utf-8"))

    assert len(index) == len(attachments)
    for att in attachments:
        assert att.id in index
        assert index[att.id] == att.storage_path
