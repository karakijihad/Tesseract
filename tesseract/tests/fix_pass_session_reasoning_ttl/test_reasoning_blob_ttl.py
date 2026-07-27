"""Reasoning-blob TTL — operator-tightened to 7 days, locked per-session.

Coverage:
- TTL constant is 7 days (operator choice 2026-05-14, was 14).
- `load_session()` default does NOT strip reasoning blobs — enumeration
  paths (drawer listing, by-day, archive) stay cheap.
- `load_session(strip_reasoning=True)` strips when session age (anchored
  on `ended_at`) > TTL.
- A session that *started* before the TTL but was *touched recently*
  keeps its reasoning blobs — the strip's anchor is `ended_at` so a
  resumed-old session never loses live blobs from yesterday's turn.
- `save_session` always preserves reasoning — write-time stripping
  diverged from the read-time anchor and is intentionally absent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tesseract.brain.session_store import (
    REASONING_BLOB_MAX_AGE_DAYS,
    load_session,
    save_session,
)


def _write_session(
    path: Path,
    *,
    started_at: datetime,
    ended_at: datetime | None = None,
    history: list[dict],
) -> Path:
    payload = {
        "schema": 1,
        "started_at": started_at.isoformat(),
        "ended_at": (ended_at or started_at).isoformat(),
        "turn_count": 1,
        "model": "gpt-5.4-mini",
        "history": history,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_ttl_constant_is_seven_days() -> None:
    assert REASONING_BLOB_MAX_AGE_DAYS == 7


def test_load_session_default_does_not_strip(tmp_path: Path) -> None:
    """Enumeration default — fast listing must not rewrite history."""
    old = datetime.now(timezone.utc) - timedelta(days=30)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "_reasoning": True},
    ]
    path = _write_session(tmp_path / "old.json", started_at=old, history=history)

    state = load_session(path)
    assert state is not None
    assert len(state.history) == 2
    assert any(m.get("_reasoning") for m in state.history)


def test_load_session_strip_drops_old_reasoning(tmp_path: Path) -> None:
    """Resume path — strip_reasoning=True drops blobs past the TTL."""
    old = datetime.now(timezone.utc) - timedelta(days=REASONING_BLOB_MAX_AGE_DAYS + 1)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "_reasoning": True},
    ]
    path = _write_session(tmp_path / "old.json", started_at=old, history=history)

    state = load_session(path, strip_reasoning=True)
    assert state is not None
    assert len(state.history) == 1
    assert all(not m.get("_reasoning") for m in state.history)


def test_load_session_strip_keeps_recent_reasoning(tmp_path: Path) -> None:
    """Within-TTL sessions keep their blobs even on resume."""
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "_reasoning": True},
    ]
    path = _write_session(tmp_path / "fresh.json", started_at=fresh, history=history)

    state = load_session(path, strip_reasoning=True)
    assert state is not None
    assert len(state.history) == 2
    assert any(m.get("_reasoning") for m in state.history)


def test_load_session_strip_keeps_blobs_when_ended_at_is_recent(
    tmp_path: Path,
) -> None:
    """Regression: a session whose `started_at` is past the TTL but whose
    `ended_at` (last save) is recent must keep its reasoning blobs — the
    blobs are from yesterday's turn, the API will accept them. The strip
    anchors on `ended_at`."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "_reasoning": True},
    ]
    path = _write_session(
        tmp_path / "resumed.json",
        started_at=long_ago,
        ended_at=yesterday,
        history=history,
    )

    state = load_session(path, strip_reasoning=True)
    assert state is not None
    assert len(state.history) == 2
    assert any(m.get("_reasoning") for m in state.history)


def test_save_session_preserves_reasoning_regardless_of_started_at(
    tmp_path: Path,
) -> None:
    """Save must not strip on write. A long-running session that was
    resumed last week and just had a fresh turn keeps its blobs — the
    read-time strip handles TTL on the next load, anchored on the
    correct timestamp."""
    long_ago = (
        datetime.now(timezone.utc) - timedelta(days=REASONING_BLOB_MAX_AGE_DAYS + 5)
    ).isoformat()
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "_reasoning": True},
    ]
    path = save_session(
        session_dir=tmp_path,
        name="resumed-old",
        model="gpt-5.4-mini",
        started_at=long_ago,
        history=history,
    )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert any(m.get("_reasoning") for m in data["history"])
