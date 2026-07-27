"""AU-4 — AgendaStore CRUD + atomic writes + archive + index.jsonl + dedupe."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import (
    AgendaItem,
    AgendaSource,
    AgendaStatus,
    RiskClass,
)
from tesseract.orchestrator.autonomy.paths import (
    agenda_active_dir,
    agenda_archive_dir,
    agenda_index_path,
    agenda_item_path,
)
from tesseract.tests.fix_pass_autonomy_AU_4.conftest import make_item


def test_add_then_get_round_trip(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item(goal="audit-doe-flow")
    store.add(item)

    loaded = store.get(item.id)
    assert loaded is not None
    assert loaded.id == item.id
    assert loaded.goal == "audit-doe-flow"
    assert loaded.status == AgendaStatus.PROPOSED
    # add() must seed a status_history row at creation.
    assert len(loaded.status_history) == 1
    assert loaded.status_history[0].from_status is None


def test_add_assigns_score_components(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item(operator_priority=2)
    store.add(item)
    loaded = store.get(item.id)
    assert loaded is not None
    assert loaded.priority_score != 0.0
    assert "operator_priority" in loaded.score_components
    assert loaded.score_computed_at is not None


def test_add_refuses_absolute_deny(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item(risk_class=RiskClass.ABSOLUTE_DENY)
    with pytest.raises(ValueError):
        store.add(item)


def test_add_refuses_duplicate_id(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item()
    store.add(item)
    with pytest.raises(ValueError):
        store.add(item)


def test_get_returns_none_for_missing(isolated_home: Path) -> None:
    store = AgendaStore()
    assert store.get("ag-never-existed") is None


def test_no_leftover_tmp_files(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item()
    store.add(item)
    active = agenda_active_dir()
    leftover = list(active.glob("*.tmp"))
    assert leftover == []


def test_transition_updates_file_and_index(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item()
    store.add(item)

    store.transition(item, AgendaStatus.SELECTED, reason="kernel_pick", by="kernel")
    loaded = store.get(item.id)
    assert loaded is not None
    assert loaded.status == AgendaStatus.SELECTED

    rows = _read_index(agenda_index_path())
    # One create row, one transition row.
    assert len(rows) == 2
    assert rows[0]["event"] == "created"
    assert rows[1]["event"] == "transition"
    assert rows[1]["from"] == "proposed"
    assert rows[1]["to"] == "selected"


def test_terminal_transition_archives(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item()
    store.add(item)
    assert agenda_item_path(item.id).exists()

    store.transition(item, AgendaStatus.DONE, reason="finished")
    assert not agenda_item_path(item.id).exists()

    # YYYY-MM bucket from updated_at — UTC normalisation keeps it stable.
    month = item.updated_at.astimezone(timezone.utc).strftime("%Y-%m")
    archived = agenda_archive_dir() / month / f"{item.id}.json"
    assert archived.exists()

    # get() still finds it via the archive lookup.
    loaded = store.get(item.id)
    assert loaded is not None
    assert loaded.status == AgendaStatus.DONE


def test_save_refuses_non_terminal_archive_call(isolated_home: Path) -> None:
    """``_archive`` is private but the invariant — refuse to archive
    non-terminal items — protects callers that misroute the save."""
    store = AgendaStore()
    item = make_item(status=AgendaStatus.RUNNING)
    with pytest.raises(ValueError):
        store._archive(item)


def test_iter_active_skips_malformed(isolated_home: Path) -> None:
    store = AgendaStore()
    item = make_item()
    store.add(item)

    bad = agenda_active_dir() / "ag-broken-doe.json"
    bad.write_text("{not-json", encoding="utf-8")

    records = list(store.iter_active())
    assert [r.id for r in records] == [item.id]


def test_ranked_orders_by_score(isolated_home: Path) -> None:
    store = AgendaStore()
    low = make_item(
        goal="low-priority",
        operator_priority=0,
        item_id="ag-2026-05-18-1000-low",
    )
    high = make_item(
        goal="urgent",
        operator_priority=5,
        item_id="ag-2026-05-18-1000-high",
    )
    store.add(low)
    store.add(high)

    ranked = store.ranked()
    assert [r.id for r in ranked] == [high.id, low.id]


def test_ranked_tiebreaker_is_created_at(isolated_home: Path) -> None:
    store = AgendaStore()
    older = make_item(
        goal="older",
        now=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        item_id="ag-2026-05-18-1000-a",
    )
    newer = make_item(
        goal="newer",
        now=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
        item_id="ag-2026-05-18-1200-b",
    )
    store.add(older)
    store.add(newer)
    ranked = store.ranked()
    # Same operator_priority + same risk + budget; older wins the tie.
    assert [r.id for r in ranked] == [older.id, newer.id]


def test_find_dedupe_matches_normalised_goal(isolated_home: Path) -> None:
    store = AgendaStore()
    existing = make_item(goal="audit the repo")
    store.add(existing)

    match = store.find_dedupe("AUDIT  the   repo", AgendaSource.SELF_REFLECTION)
    assert match is not None
    assert match.id == existing.id

    no_match = store.find_dedupe("audit the repo", AgendaSource.OPERATOR)
    assert no_match is None


def test_today_spend_aggregates(isolated_home: Path) -> None:
    store = AgendaStore()
    a = make_item(goal="a", item_id="ag-2026-05-18-1200-a")
    a.budget_tokens_spent = 100
    a.budget_seconds_spent = 30
    b = make_item(goal="b", item_id="ag-2026-05-18-1201-b")
    b.budget_tokens_spent = 250
    b.budget_seconds_spent = 75
    store.add(a)
    store.add(b)
    totals = store.today_spend()
    assert totals == {"tokens": 350, "seconds": 105}


def test_transition_timestamps_are_consistent(isolated_home: Path) -> None:
    """The status_history entry's `at`, the file's `updated_at`, and the
    index.jsonl row's `ts` must all match — a single transition event
    has one canonical timestamp, not three. Reviewer flag at S1: save()
    was overwriting `updated_at` after `transition_to` already set it."""
    store = AgendaStore()
    item = make_item()
    store.add(item)
    store.transition(item, AgendaStatus.SELECTED, reason="kernel_pick")

    loaded = store.get(item.id)
    assert loaded is not None
    # The most recent history entry's `at` matches the file's `updated_at`.
    assert loaded.status_history[-1].at == loaded.updated_at

    rows = _read_index(agenda_index_path())
    transition_row = next(r for r in rows if r["event"] == "transition")
    assert transition_row["ts"] == loaded.updated_at.isoformat()


def test_archive_lookup_handles_cross_month(isolated_home: Path) -> None:
    """An item created in month N and archived in month N+1 lives in
    the archive bucket keyed by `updated_at`, not by the id's encoded
    creation date. `_find_in_archive` must locate it via full scan."""
    store = AgendaStore()
    created = datetime(2026, 4, 30, 23, 30, tzinfo=timezone.utc)
    item = make_item(now=created, item_id="ag-2026-04-30-2330-cross-month")
    store.add(item)

    # Bump updated_at into the next month before terminal transition.
    item.transition_to(AgendaStatus.DONE, reason="archive-cross-month")
    item.updated_at = datetime(2026, 5, 1, 0, 30, tzinfo=timezone.utc)
    store.save(item)

    archived = agenda_archive_dir() / "2026-05" / f"{item.id}.json"
    assert archived.exists(), "cross-month archive must use updated_at bucket"
    found = store.get(item.id)
    assert found is not None
    assert found.id == item.id


def _read_index(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
