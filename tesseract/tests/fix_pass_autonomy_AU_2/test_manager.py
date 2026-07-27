"""AU-2 — RecoveryManager scans + idempotency.

Integration tests that walk on-disk state through the manager and
assert the resulting summary. All file I/O is scoped to ``tmp_path``
so the production ``tesseract/logs/`` tree stays untouched.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp_path as TESSERACT_HOME for every recovery-manager test.

    Reloads `tesseract.paths` and the recovery package so the
    TESSERACT_HOME constant inside the manager picks up the new env.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    import tesseract.paths
    importlib.reload(tesseract.paths)
    import tesseract.orchestrator.recovery
    importlib.reload(tesseract.orchestrator.recovery.manager)
    importlib.reload(tesseract.orchestrator.recovery)
    return tmp_path


@pytest.mark.asyncio
async def test_clean_boot_no_state(isolated_home: Path) -> None:
    """No workers, no runs.jsonl → empty counters and no operator
    attention required."""
    from tesseract.orchestrator.recovery import new_recovery_manager

    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=False)

    assert summary.scans["schedule"] == {"completed": 0, "failed": 0}
    assert summary.operator_attention == []


@pytest.mark.asyncio
async def test_schedule_scan_counts_recent_runs(isolated_home: Path) -> None:
    """runs.jsonl rows within the lookback window are counted as
    refired (ok=True) or interrupted (ok=False)."""
    from tesseract.orchestrator.recovery import new_recovery_manager

    schedule_dir = isolated_home / "logs" / "schedule"
    schedule_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    rows = [
        {"job_name": "daily_brief", "run_id": "r1", "fired_at": now,
         "completed_at": now, "ok": True, "detail": "", "payload": {}, "duration_ms": 1.0},
        {"job_name": "dream_cycle", "run_id": "r2", "fired_at": now,
         "completed_at": now, "ok": False, "detail": "boom", "payload": {}, "duration_ms": 1.0},
    ]
    (schedule_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )

    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=False)

    assert summary.scans["schedule"]["completed"] == 1
    assert summary.scans["schedule"]["failed"] == 1


@pytest.mark.asyncio
async def test_idempotency_double_run_diff_clean(isolated_home: Path) -> None:
    """Running recovery twice on the same on-disk state produces
    identical scan counts + attention items — the design invariant."""
    from tesseract.orchestrator.recovery import new_recovery_manager

    schedule_dir = isolated_home / "logs" / "schedule"
    schedule_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().isoformat()
    row = {"job_name": "daily_brief", "run_id": "r1", "fired_at": now,
           "completed_at": now, "ok": True, "detail": "", "payload": {}, "duration_ms": 1.0}
    (schedule_dir / "runs.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    rm1 = new_recovery_manager(tesseract_home=isolated_home)
    s1 = await rm1.run(emit_event=False)
    rm2 = new_recovery_manager(tesseract_home=isolated_home)
    s2 = await rm2.run(emit_event=False)

    assert s1.scans == s2.scans
    assert [(a.kind, a.id, a.reason) for a in s1.operator_attention] == \
           [(a.kind, a.id, a.reason) for a in s2.operator_attention]


@pytest.mark.asyncio
async def test_event_emit_appends_to_workspace_store(isolated_home: Path) -> None:
    """When emit_event=True (default), the summary lands as a
    `recovery_summary` workspace event in the EventStore."""
    from tesseract.orchestrator.recovery import new_recovery_manager
    from tesseract.workspace_events import EventStore

    rm = new_recovery_manager(tesseract_home=isolated_home)
    summary = await rm.run(emit_event=True)

    store = EventStore(isolated_home / "logs")
    events = store.list_events()
    recovery_events = [e for e in events if e.kind == "recovery_summary"]
    assert len(recovery_events) == 1
    assert recovery_events[0].event_id == f"recovery-{summary.boot_id}"
    assert recovery_events[0].source == "recovery"
    assert recovery_events[0].priority == 8


@pytest.mark.asyncio
async def test_workers_scan_does_not_mutate_disk(isolated_home: Path) -> None:
    """Recovery is strictly read-only. A legacy <TESSERACT_HOME>/spawns/
    directory (or any file under it) must survive the worker scan
    untouched — sweeping disk state inside recovery would break the
    idempotency invariant (a second pass would see different state).
    """
    from tesseract.orchestrator.recovery import new_recovery_manager

    spawns = isolated_home / "spawns"
    spawns.mkdir()
    legacy = spawns / "legacy.json"
    legacy.write_text("{}", encoding="utf-8")

    rm = new_recovery_manager(tesseract_home=isolated_home)
    await rm.run(emit_event=False)

    assert legacy.exists(), "recovery must not unlink legacy spawns/"
    assert spawns.exists()
