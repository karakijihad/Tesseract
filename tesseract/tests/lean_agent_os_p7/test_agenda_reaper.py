"""P7 addendum — ``agenda_reaper`` standing self-prune (operator directive
2026-07-07: ~87 stale non-terminal agenda items starved admission against
``max_open_total``).

Covers:
- aged ``blocked`` + ``resume_queued`` items reaped to ABANDONED (by=kernel
  + reaper reason); fresh items of the same statuses untouched
- age measured from the item's last status transition, not created_at
- a status absent from the ``max_age_days`` map is never reaped
- ``exempt_sources`` honored; SCOUT/STRATEGIST skipped ONLY for the
  statuses their own dedicated reapers own (built-in, no config needed)
- missing ``max_age_days`` config -> ok=False, detail names the key
- P7 whole-phase review finding 1: an aged SCOUT/STRATEGIST item in
  BLOCKED (a status neither dedicated reaper owns) is reaped here;
  an aged SCOUT/STRATEGIST item in PROPOSED (owned by the dedicated
  reaper) is left untouched — no double-handling
- no logs pollution under tesseract/logs/ from the test run
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.drafts import AgendaItemDraft
from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus, RiskClass
from tesseract.scheduler.tasks.agenda_reaper import AgendaReaperJob
from tesseract.scheduler.types import JobContext

# This file is tesseract/tests/lean_agent_os_p7/test_agenda_reaper.py, so
# parents[2] is `tesseract/`, not the repo root.
_REPO_LOGS = Path(__file__).resolve().parents[2] / "logs"


def _logs_snapshot() -> dict[str, int]:
    """Recursive, per-file-size snapshot — a top-level-name-only diff would
    miss an appended line to an existing file, which is exactly the
    zero-tolerance case the project's logs-pollution rule exists to catch."""
    if not _REPO_LOGS.exists():
        return {}
    return {
        str(p.relative_to(_REPO_LOGS)): p.stat().st_size
        for p in _REPO_LOGS.rglob("*")
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _guard_logs_pollution():
    """Zero-tolerance guard (CLAUDE.md): no test may write under
    tesseract/logs/. Snapshot at THIS test's setup and re-check at its
    teardown, so the comparison window spans only this test's own
    execution — not the whole collection→run span, which false-positives
    on any concurrent live-service write (mirror-backend.log, tokenjuice/,
    …) during a slow suite."""
    before = _logs_snapshot()
    yield
    after = _logs_snapshot()
    assert after == before, (
        f"tesseract/logs/ changed during this test. before={before} after={after}"
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _ctx(*, config: dict, fired_at: datetime) -> JobContext:
    return JobContext(job_name="agenda_reaper", fired_at=fired_at, app={}, config=config)


def _make_item(
    store: AgendaStore,
    *,
    status: AgendaStatus,
    created_at: datetime,
    source: AgendaSource = AgendaSource.VAULT_SIGNAL,
    goal: str = "stale item",
):
    draft = AgendaItemDraft(goal=goal, source=source, risk_class=RiskClass.PROPOSE)
    item = draft.to_item(now=created_at, status=status)
    store.add(item)
    return item


# ── Test 1: aged blocked + resume_queued reaped; fresh items untouched ──


async def test_aged_items_reaped_fresh_untouched(isolated_home: Path) -> None:
    store = AgendaStore()
    fired_at = datetime(2026, 7, 7, tzinfo=timezone.utc)
    old = fired_at - timedelta(days=40)
    fresh = fired_at - timedelta(days=1)

    old_blocked = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, goal="old blocked")
    old_resume = _make_item(store, status=AgendaStatus.RESUME_QUEUED, created_at=old, goal="old resume")
    fresh_blocked = _make_item(store, status=AgendaStatus.BLOCKED, created_at=fresh, goal="fresh blocked")
    fresh_resume = _make_item(store, status=AgendaStatus.RESUME_QUEUED, created_at=fresh, goal="fresh resume")

    result = await AgendaReaperJob().run(
        _ctx(config={"max_age_days": {"blocked": 30, "resume_queued": 30}}, fired_at=fired_at)
    )

    assert result.ok is True
    assert "blocked=1" in result.detail
    assert "resume_queued=1" in result.detail

    reaped_blocked = store.get(old_blocked.id)
    assert reaped_blocked.status == AgendaStatus.ABANDONED
    assert reaped_blocked.status_history[-1].by == "kernel"
    assert "agenda_reaper: stale blocked > 30d" in reaped_blocked.status_history[-1].reason

    reaped_resume = store.get(old_resume.id)
    assert reaped_resume.status == AgendaStatus.ABANDONED
    assert "agenda_reaper: stale resume_queued > 30d" in reaped_resume.status_history[-1].reason

    assert store.get(fresh_blocked.id).status == AgendaStatus.BLOCKED
    assert store.get(fresh_resume.id).status == AgendaStatus.RESUME_QUEUED


# ── Test 2: age from last transition, not created_at ──


async def test_age_measured_from_last_transition(isolated_home: Path) -> None:
    store = AgendaStore()
    old = datetime.now(timezone.utc) - timedelta(days=400)
    item = _make_item(store, status=AgendaStatus.PROPOSED, created_at=old, goal="long-lived, recently touched")

    # Recent transition — real store API stamps `at` with wall-clock now.
    store.transition(item, AgendaStatus.BLOCKED, reason="operator follow-up", by="operator")

    result = await AgendaReaperJob().run(
        _ctx(config={"max_age_days": {"blocked": 30}}, fired_at=datetime.now(timezone.utc))
    )

    assert result.ok is True
    assert result.detail == "idle"
    assert store.get(item.id).status == AgendaStatus.BLOCKED


# ── Test 3: status absent from max_age_days map is never reaped ──


async def test_status_absent_from_map_never_reaped(isolated_home: Path) -> None:
    store = AgendaStore()
    fired_at = datetime(2026, 7, 7, tzinfo=timezone.utc)
    old = fired_at - timedelta(days=400)
    item = _make_item(store, status=AgendaStatus.RUNNING, created_at=old, goal="old running")

    result = await AgendaReaperJob().run(
        _ctx(config={"max_age_days": {"blocked": 30}}, fired_at=fired_at)
    )

    assert result.ok is True
    assert result.detail == "idle"
    assert store.get(item.id).status == AgendaStatus.RUNNING


# ── Test 4: exempt_sources honored; SCOUT/STRATEGIST built-in skip is
#            status-aware, not blanket (P7 whole-phase review finding 1) ──


async def test_exempt_sources_and_builtin_skip(isolated_home: Path) -> None:
    store = AgendaStore()
    fired_at = datetime(2026, 7, 7, tzinfo=timezone.utc)
    old = fired_at - timedelta(days=40)

    # BLOCKED is not owned by scout_reaper/strategist_reaper's _REAPABLE —
    # falls through to this reaper's general sweep, not the built-in skip.
    scout_item = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, source=AgendaSource.SCOUT, goal="scout stale")
    strategist_item = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, source=AgendaSource.STRATEGIST, goal="strategist stale")
    operator_item = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, source=AgendaSource.OPERATOR, goal="operator stale")
    reapable_item = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, source=AgendaSource.VAULT_SIGNAL, goal="vault stale")

    result = await AgendaReaperJob().run(
        _ctx(
            config={"max_age_days": {"blocked": 30}, "exempt_sources": ["operator"]},
            fired_at=fired_at,
        )
    )

    assert result.ok is True
    assert "skipped=1" in result.detail
    assert "blocked=3" in result.detail

    assert store.get(scout_item.id).status == AgendaStatus.ABANDONED
    assert store.get(strategist_item.id).status == AgendaStatus.ABANDONED
    assert store.get(operator_item.id).status == AgendaStatus.BLOCKED
    assert store.get(reapable_item.id).status == AgendaStatus.ABANDONED


# ── Test 4b: aged BLOCKED SCOUT/STRATEGIST items are reaped — a status
#             neither dedicated reaper owns, so nothing else prunes it
#             (P7 whole-phase review finding 1: was permanently unreachable) ──


async def test_blocked_scout_and_strategist_items_reaped(isolated_home: Path) -> None:
    store = AgendaStore()
    fired_at = datetime(2026, 7, 7, tzinfo=timezone.utc)
    old = fired_at - timedelta(days=40)

    scout_blocked = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, source=AgendaSource.SCOUT, goal="scout blocked")
    strategist_blocked = _make_item(store, status=AgendaStatus.BLOCKED, created_at=old, source=AgendaSource.STRATEGIST, goal="strategist blocked")

    result = await AgendaReaperJob().run(
        _ctx(config={"max_age_days": {"blocked": 30}}, fired_at=fired_at)
    )

    assert result.ok is True
    assert "blocked=2" in result.detail
    assert store.get(scout_blocked.id).status == AgendaStatus.ABANDONED
    assert store.get(strategist_blocked.id).status == AgendaStatus.ABANDONED


# ── Test 4c: aged PROPOSED SCOUT/STRATEGIST items are left alone — owned
#             by their dedicated reapers; no double-handling ──


async def test_proposed_scout_and_strategist_items_left_alone(isolated_home: Path) -> None:
    store = AgendaStore()
    fired_at = datetime(2026, 7, 7, tzinfo=timezone.utc)
    old = fired_at - timedelta(days=40)

    scout_proposed = _make_item(store, status=AgendaStatus.PROPOSED, created_at=old, source=AgendaSource.SCOUT, goal="scout proposed")
    strategist_proposed = _make_item(store, status=AgendaStatus.PROPOSED, created_at=old, source=AgendaSource.STRATEGIST, goal="strategist proposed")

    result = await AgendaReaperJob().run(
        _ctx(config={"max_age_days": {"proposed": 30}}, fired_at=fired_at)
    )

    assert result.ok is True
    assert result.detail == "reaped 0 skipped=2 exempt"
    assert store.get(scout_proposed.id).status == AgendaStatus.PROPOSED
    assert store.get(strategist_proposed.id).status == AgendaStatus.PROPOSED


# ── Test 5: missing max_age_days -> ok=False, detail names the key ──


async def test_missing_max_age_days_fails() -> None:
    result = await AgendaReaperJob().run(
        _ctx(config={}, fired_at=datetime.now(timezone.utc))
    )
    assert result.ok is False
    assert "max_age_days" in result.detail


