"""Audit fix m1 — rolling 24h counters tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tesseract.integrations.telegram.state import PollState, _prune_24h


def _iso(when: datetime) -> str:
    return when.isoformat()


def test_count_inbound_24h_excludes_old_entries() -> None:
    state = PollState()
    now = datetime.now(timezone.utc)
    state.recent_inbound_ts.append(_iso(now - timedelta(hours=30)))  # old
    state.recent_inbound_ts.append(_iso(now - timedelta(hours=10)))  # in
    state.recent_inbound_ts.append(_iso(now - timedelta(minutes=5)))  # in
    assert state.count_inbound_24h() == 2
    # Old entry pruned from the underlying list too.
    assert len(state.recent_inbound_ts) == 2


def test_count_outbound_24h_zero_when_empty() -> None:
    state = PollState()
    assert state.count_outbound_24h() == 0


def test_record_helpers_append_and_prune() -> None:
    state = PollState()
    now = datetime.now(timezone.utc)
    # Seed with an old entry so the next record_inbound prunes it.
    state.recent_inbound_ts.append(_iso(now - timedelta(hours=48)))
    state.record_inbound("111", _iso(now))
    assert state.count_inbound_24h() == 1

    state.record_outbound("111", _iso(now))
    assert state.count_outbound_24h() == 1


def test_prune_24h_skips_malformed() -> None:
    items = ["not-a-date", datetime.now(timezone.utc).isoformat()]
    _prune_24h(items)
    # Malformed entry treated as expired and dropped.
    assert "not-a-date" not in items
    assert len(items) == 1
