"""Supervisor startup orphan-reaping.

2026-07-01: a hard-killed supervisor skips its finally-block teardown, so
its controller daemon child orphans and lingers. Stacked generations then
kill each other's healthy backend/Vite (exit code=1, no traceback). The
supervisor now reaps leaked daemon/backend processes at startup. Tests
exercise the pure filter + the stubbed reap flow — no real processes
touched, so the suite stays portable + fast.
"""

from __future__ import annotations

import pytest

from tesseract.supervisor import reap


def test_orphan_pids_matches_each_marker() -> None:
    procs = [
        (101, "python.exe -m tesseract.scripts.tars_controller"),
        (102, "python.exe -m tesseract.mirror.server"),
    ]
    assert reap.orphan_pids(procs, self_pid=999) == [101, 102]


def test_orphan_pids_excludes_self_and_supervisor_and_unrelated() -> None:
    procs = [
        (7, "python.exe -m tesseract.supervisor"),   # never kill a supervisor
        (8, "python.exe -m tesseract.mirror.server"),   # orphan → reap
        (9, "python.exe -m some.other.module"),      # unrelated
        (8, "self would be excluded by pid"),        # duplicate pid == self
    ]
    # self_pid == 8 must be skipped even though its cmdline carries a marker.
    assert reap.orphan_pids(procs, self_pid=8) == []


def test_orphan_pids_skips_nonpositive() -> None:
    procs = [(0, "tesseract.mirror.server"), (-1, "tesseract.mirror.server")]
    assert reap.orphan_pids(procs, self_pid=999) == []


def test_reap_orphans_kills_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_DISABLE_REAP", raising=False)
    monkeypatch.setattr(reap.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(
        reap, "_enumerate_posix",
        lambda: [
            (100, "python -m tesseract.scripts.tars_controller"),
            (101, "python -m tesseract.mirror.server"),
            (102, "python -m tesseract.supervisor"),  # spared
        ],
    )
    killed: list[int] = []
    monkeypatch.setattr(reap, "_kill", lambda pid: (killed.append(pid) or True))

    reaped = reap.reap_orphans()
    assert sorted(reaped) == [100, 101]
    assert sorted(killed) == [100, 101]


def test_reap_orphans_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_DISABLE_REAP", "1")
    called = {"enumerated": False}

    def _boom() -> list[tuple[int, str]]:
        called["enumerated"] = True
        return []

    monkeypatch.setattr(reap, "_enumerate_posix", _boom)
    monkeypatch.setattr(reap, "_enumerate_windows", _boom)
    assert reap.reap_orphans() == []
    assert called["enumerated"] is False


def test_reap_orphans_skips_pids_that_fail_to_die(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_DISABLE_REAP", raising=False)
    monkeypatch.setattr(reap.sys, "platform", "linux", raising=False)
    monkeypatch.setattr(
        reap, "_enumerate_posix",
        lambda: [
            (100, "tesseract.scripts.tars_controller"),
            (101, "tesseract.scripts.tars_controller"),
        ],
    )
    # 100 refuses to die (already gone / permission); only 101 is reported.
    monkeypatch.setattr(reap, "_kill", lambda pid: pid == 101)
    assert reap.reap_orphans() == [101]
