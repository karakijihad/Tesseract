"""Deferred-item fix — ``mission-digest``'s underlying data source was
deleted with the mission engine (P4 prune wave 1). This module reads
the still-live AgendaStore instead; these are the pre-fetcher's unit
tests. Renderer integration lives in
``test_mission_digest_renderer_integration.py``.

Per CLAUDE.md log-safety: every test monkeypatches ``TESSERACT_HOME``
before any writer is exercised, even though this module exercises only
the pre-fetcher (which is pure reads).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.brief.activity import (
    DEFAULT_SINCE_HOURS,
    MAX_ITEMS,
    collect_yesterday_activity,
)


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def today() -> date:
    return date(2026, 7, 12)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)


def _write_agenda_item(path: Path, **overrides) -> None:
    record = {
        "id": "ag_seed",
        "status": "done",
        "goal": "review scout finding",
        "blocked_reason": "",
        "source": "scout",
        "updated_at": _iso(datetime.now(timezone.utc)),
    }
    record.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# 1. No agenda directory at all → empty items list, no crash.
# ────────────────────────────────────────────────────────────────────


def test_no_agenda_dir_returns_empty(tmp_path: Path, today: date) -> None:
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert payload["since_hours"] == DEFAULT_SINCE_HOURS
    assert payload["items"] == []


# ────────────────────────────────────────────────────────────────────
# 2. A DONE item archived within the window is picked up; one outside
#    the window is excluded.
# ────────────────────────────────────────────────────────────────────


def test_done_item_in_archive_within_window(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    in_window = anchor - timedelta(hours=2)
    too_old = anchor - timedelta(hours=48)
    _write_agenda_item(
        tmp_path / "agenda" / "archive" / "2026-07" / "ag_done.json",
        id="ag_done",
        status="done",
        goal="ship the pricing sync",
        source="strategist",
        updated_at=_iso(in_window),
    )
    _write_agenda_item(
        tmp_path / "agenda" / "archive" / "2026-05" / "ag_old.json",
        id="ag_old",
        status="done",
        goal="stale item",
        updated_at=_iso(too_old),
    )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert len(payload["items"]) == 1
    row = payload["items"][0]
    assert row["status"] == "done"
    assert row["goal"] == "ship the pricing sync"
    assert row["source"] == "strategist"


# ────────────────────────────────────────────────────────────────────
# 3. A BLOCKED item stays in active/ (not terminal) and is picked up
#    with its blocked_reason.
# ────────────────────────────────────────────────────────────────────


def test_blocked_item_in_active_within_window(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    _write_agenda_item(
        tmp_path / "agenda" / "active" / "ag_blocked.json",
        id="ag_blocked",
        status="blocked",
        goal="rotate the tavily key",
        blocked_reason="missing operator approval",
        source="operator",
        updated_at=_iso(anchor - timedelta(hours=1)),
    )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert len(payload["items"]) == 1
    row = payload["items"][0]
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "missing operator approval"


# ────────────────────────────────────────────────────────────────────
# 4. Non-terminal statuses (running, proposed, selected, ...) are
#    excluded even if fresh.
# ────────────────────────────────────────────────────────────────────


def test_in_flight_statuses_excluded(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    for i, status in enumerate(("running", "proposed", "selected", "unvetted")):
        _write_agenda_item(
            tmp_path / "agenda" / "active" / f"ag_{i}.json",
            id=f"ag_{i}",
            status=status,
            goal=f"in flight {status}",
            updated_at=_iso(anchor - timedelta(minutes=5)),
        )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert payload["items"] == []


# ────────────────────────────────────────────────────────────────────
# 5. Malformed JSON and non-agenda files (e.g. source-pauses.json) are
#    skipped without raising.
# ────────────────────────────────────────────────────────────────────


def test_malformed_and_non_agenda_records_skipped(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    agenda = tmp_path / "agenda"
    agenda.mkdir(parents=True)
    (agenda / "source-pauses.json").write_text(
        json.dumps({"paused_sources": ["scout"]}), encoding="utf-8"
    )
    (agenda / "active").mkdir()
    (agenda / "active" / "ag_garbled.json").write_text("not-valid-json{{{", encoding="utf-8")
    _write_agenda_item(
        agenda / "active" / "ag_ok.json",
        id="ag_ok",
        status="blocked",
        goal="valid item",
        updated_at=_iso(anchor - timedelta(hours=1)),
    )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert [r["goal"] for r in payload["items"]] == ["valid item"]


# ────────────────────────────────────────────────────────────────────
# 6. Cap + sort — newest updated_at first, bounded at MAX_ITEMS.
# ────────────────────────────────────────────────────────────────────


def test_sorted_newest_first_and_capped(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    active = tmp_path / "agenda" / "active"
    for i in range(MAX_ITEMS + 5):
        _write_agenda_item(
            active / f"ag_{i:03d}.json",
            id=f"ag_{i:03d}",
            status="blocked",
            goal=f"item {i}",
            updated_at=_iso(anchor - timedelta(minutes=i)),
        )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert len(payload["items"]) == MAX_ITEMS
    assert payload["items"][0]["goal"] == "item 0"


# ────────────────────────────────────────────────────────────────────
# 7. since_hours is respected as a caller override.
# ────────────────────────────────────────────────────────────────────


def test_since_hours_override(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    _write_agenda_item(
        tmp_path / "agenda" / "active" / "ag_within_48.json",
        id="ag_within_48",
        status="blocked",
        goal="within 48h but not 24h",
        updated_at=_iso(anchor - timedelta(hours=30)),
    )
    payload_24h = collect_yesterday_activity(home=tmp_path, target_date=today, since_hours=24)
    payload_48h = collect_yesterday_activity(home=tmp_path, target_date=today, since_hours=48)
    assert payload_24h["items"] == []
    assert len(payload_48h["items"]) == 1
    assert payload_48h["since_hours"] == 48


# ────────────────────────────────────────────────────────────────────
# 8. Reviewer finding — upper bound. An item that transitioned AFTER
#    ``target_date``'s window (e.g. today, when backfilling a past
#    brief date via the /brief REPL tool) must not leak into that
#    stale day's digest.
# ────────────────────────────────────────────────────────────────────


def test_item_after_target_window_excluded(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    _write_agenda_item(
        tmp_path / "agenda" / "active" / "ag_future.json",
        id="ag_future",
        status="blocked",
        goal="happened after the backfilled date",
        updated_at=_iso(anchor + timedelta(hours=3)),
    )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert payload["items"] == []


# ────────────────────────────────────────────────────────────────────
# 9. Reviewer finding — de-dup by id. The same item id showing up in
#    both active/ and archive/ (a mid-transition race) must not be
#    double-counted.
# ────────────────────────────────────────────────────────────────────


def test_same_id_in_active_and_archive_deduped(tmp_path: Path, today: date) -> None:
    anchor = _midnight(today)
    when = _iso(anchor - timedelta(hours=1))
    _write_agenda_item(
        tmp_path / "agenda" / "active" / "ag_race.json",
        id="ag_race",
        status="blocked",
        goal="mid-transition item",
        updated_at=when,
    )
    _write_agenda_item(
        tmp_path / "agenda" / "archive" / "2026-07" / "ag_race.json",
        id="ag_race",
        status="done",
        goal="mid-transition item",
        updated_at=when,
    )
    payload = collect_yesterday_activity(home=tmp_path, target_date=today)
    assert len(payload["items"]) == 1
