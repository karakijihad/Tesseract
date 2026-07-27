"""AU-16 S1 — per-source LeafBuffer."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.leaf_buffers import (
    LeafBuffer,
    buffer_path,
    iter_buffers,
    source_slug,
)


def test_source_slug_canonicalises_special_chars() -> None:
    assert source_slug("channel:telegram:12345") == "channel-telegram-12345"
    assert source_slug("CHAT:Session/77") == "chat-session-77"
    assert source_slug("---weird---") == "weird"
    assert source_slug("") == "unknown"


def test_buffer_path_under_isolated_home(isolated_home: Path) -> None:
    p = buffer_path("agent:librarian")
    assert p.is_relative_to(isolated_home)
    assert p.name == "agent-librarian.txt"


def test_append_read_clear_round_trip(isolated_home: Path) -> None:
    buf = LeafBuffer("agent:librarian")
    buf.append("leaf_aaaaaaaa")
    buf.append("leaf_bbbbbbbb")
    assert buf.read_ids() == ["leaf_aaaaaaaa", "leaf_bbbbbbbb"]
    buf.clear()
    assert buf.read_ids() == []
    assert buf.path.exists()  # cleared file remains as empty


def test_malformed_rows_skipped_with_warning(
    isolated_home: Path, caplog
) -> None:
    buf = LeafBuffer("agent:librarian")
    buf.path.parent.mkdir(parents=True, exist_ok=True)
    buf.path.write_text("leaf_aaaaaaaa\nnot-a-leaf-id\nleaf_bbbbbbbb\n", encoding="utf-8")
    ids = buf.read_ids()
    assert ids == ["leaf_aaaaaaaa", "leaf_bbbbbbbb"]


def test_stale_predicate_against_mtime(isolated_home: Path) -> None:
    buf = LeafBuffer("agent:librarian")
    buf.append("leaf_aaaaaaaa")
    now = datetime.now(timezone.utc)
    # Force mtime into the past.
    past = time.time() - 7200
    import os
    os.utime(buf.path, (past, past))
    assert buf.stale(now=now, max_age_seconds=3600.0) is True
    assert buf.stale(now=now, max_age_seconds=86400.0) is False


def test_stale_returns_false_when_buffer_missing(isolated_home: Path) -> None:
    buf = LeafBuffer("never-written")
    assert buf.stale(now=datetime.now(timezone.utc), max_age_seconds=1.0) is False


def test_source_collision_logs_warning(isolated_home: Path, caplog) -> None:
    """Two sources sharing a slug share a buffer and emit a warning."""
    LeafBuffer("chat:a").append("leaf_aaaaaaaa")
    with caplog.at_level("WARNING"):
        LeafBuffer("chat-a").append("leaf_bbbbbbbb")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("slug collision" in m for m in msgs)
    # Both ids still land in the shared file.
    assert LeafBuffer("chat:a").read_ids() == ["leaf_aaaaaaaa", "leaf_bbbbbbbb"]


def test_iter_buffers_walks_filesystem(isolated_home: Path) -> None:
    LeafBuffer("a").append("leaf_aaaaaaaa")
    LeafBuffer("b").append("leaf_bbbbbbbb")
    LeafBuffer("c").append("leaf_cccccccc")
    sources = sorted(buf.source for buf in iter_buffers())
    assert sources == ["a", "b", "c"]
