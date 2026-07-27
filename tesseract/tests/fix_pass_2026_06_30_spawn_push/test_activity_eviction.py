"""AS-1 gap-c — terminal ephemeral eviction sweep (Stage 2, 2026-06-30).

Finished delegate ActivityRecords are ephemeral and had no owner to remove
them, so they accumulated in the process-global registry until restart.
`sweep_terminal_ephemeral` bounds them by count (keep newest N), never touching
persistent (lane/session) or still-running records. register() calls it
opportunistically on the live (publish) path.
"""

from __future__ import annotations

import pytest

from tesseract.orchestrator.activity.models import ActivityRecord
from tesseract.orchestrator.activity.registry import (
    get_activity_registry,
    reset_activity_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_activity_registry()
    yield
    reset_activity_registry()


def _rec(aid, *, state, durability, ts):
    return ActivityRecord(
        activity_id=aid,
        kind="delegate" if durability == "ephemeral" else "lane",
        label="t",
        state=state,
        durability=durability,
        started_at=ts,
        updated_at=ts,
    )


def test_sweep_keeps_newest_evicts_oldest_terminal_ephemeral() -> None:
    reg = get_activity_registry()
    for i in range(5):
        r = _rec(f"delegate:{i}", state="done", durability="ephemeral",
                 ts=f"2026-06-30T00:00:0{i}")
        reg._records[r.activity_id] = r

    evicted = reg.sweep_terminal_ephemeral(max_keep=2)

    assert {r.activity_id for r in evicted} == {"delegate:0", "delegate:1", "delegate:2"}
    assert set(reg._records) == {"delegate:3", "delegate:4"}


def test_persistent_terminal_records_never_swept() -> None:
    reg = get_activity_registry()
    for i in range(5):
        r = _rec(f"lane:{i}", state="closed", durability="persistent",
                 ts=f"2026-06-30T00:00:0{i}")
        reg._records[r.activity_id] = r

    evicted = reg.sweep_terminal_ephemeral(max_keep=2)

    assert evicted == []
    assert len(reg._records) == 5  # lanes are durable state, not finished work


def test_running_ephemeral_never_swept() -> None:
    reg = get_activity_registry()
    for i in range(5):
        r = _rec(f"delegate:{i}", state="running", durability="ephemeral",
                 ts=f"2026-06-30T00:00:0{i}")
        reg._records[r.activity_id] = r

    evicted = reg.sweep_terminal_ephemeral(max_keep=2)

    assert evicted == []
    assert len(reg._records) == 5  # in-flight work is never evicted


def test_sweep_noop_under_cap() -> None:
    reg = get_activity_registry()
    reg._records["delegate:0"] = _rec(
        "delegate:0", state="failed", durability="ephemeral", ts="2026-06-30T00:00:00"
    )
    assert reg.sweep_terminal_ephemeral(max_keep=2) == []
    assert len(reg._records) == 1


def test_update_state_and_remove_are_noops_on_evicted_id() -> None:
    """A concurrent update_state/remove on an id sweep just evicted must be a
    silent no-op (not KeyError) — the registry's missing-id paths cover this."""
    reg = get_activity_registry()
    for i in range(3):
        reg._records[f"delegate:{i}"] = _rec(
            f"delegate:{i}", state="done", durability="ephemeral",
            ts=f"2026-06-30T00:00:0{i}"
        )
    evicted = reg.sweep_terminal_ephemeral(max_keep=0)
    assert len(evicted) == 3

    reg.update_state("delegate:0", "failed")  # no-op, no raise
    reg.remove("delegate:0")  # no-op, no raise
    assert "delegate:0" not in reg._records


def test_register_opportunistically_bounds_growth() -> None:
    reg = get_activity_registry()
    # Pre-seed 51 finished delegates with old timestamps (direct, no restamp).
    for i in range(51):
        r = _rec(f"delegate:{i:03d}", state="done", durability="ephemeral",
                 ts=f"2026-06-30T00:00:{i:02d}")
        reg._records[r.activity_id] = r
    # A live register() fires the opportunistic sweep (default cap 50).
    reg.register(_rec("delegate:new", state="running", durability="ephemeral",
                      ts="2026-06-30T01:00:00"))

    terminal = [r for r in reg._records.values()
                if r.durability == "ephemeral" and r.state in {"done", "failed", "cancelled", "closed"}]
    assert len(terminal) == 50  # oldest finished delegate evicted
    assert "delegate:000" not in reg._records  # the oldest went
    assert "delegate:new" in reg._records  # the running one stays
