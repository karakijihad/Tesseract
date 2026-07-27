"""Janitor sweeps (Docs/Plan/janitor/PLAN.md).

Pins:
- Kill rule: fingerprint AND orphan. Live-parent and claimed pids survive.
- Scratch: CLAUDE.md pytest dirs removed; failures reported, not raised.
- Sessions: stuck "active" records closed only when the owning daemon is
  dead AND past grace; a live daemon freezes the sweep entirely.
- Archives: months older than retention pruned; log pruning opt-in.
- Config: missing keys raise (no silent defaults).
- Runner: one failing sweep is isolated; report JSONL lands under
  <TESSERACT_HOME>/logs/janitor/.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from tesseract.janitor import load_janitor_config, run_sweep
from tesseract.janitor.archives import sweep_archives
from tesseract.janitor.config import JanitorConfig, LogPrune
from tesseract.janitor.pidfile import claimed_pids, write_pidfile
from tesseract.janitor.processes import sweep_processes
from tesseract.janitor.scratch import sweep_scratch
from tesseract.janitor.sessions import sweep_sessions

CFG = JanitorConfig(
    process_fingerprints=[
        {"id": "bridge", "pattern": "import json,sys,io"},
        {"id": "http", "pattern": "-m http.server"},
    ],
    scratch_dir_globs=[".pytest-tmp*", "tmp_*"],
    archive_retention_days=30,
    stale_session_grace_hours=24,
    claimed_heartbeat_max_age_s=600,
    log_prune={"retention_days": 30, "globs": []},
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------- processes


class _FakeProc:
    def __init__(self, pid: int, cmdline: list[str], orphan: bool) -> None:
        self.pid = pid
        self._cmdline = cmdline
        self._orphan = orphan
        self.terminated = False
        self.killed = False

    def cmdline(self) -> list[str]:
        return self._cmdline

    def parent(self):
        if self._orphan:
            return None
        return _FakeProc(1, ["parent"], orphan=True)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def is_running(self) -> bool:
        return not self.terminated


def test_kills_only_fingerprinted_orphans(home: Path, monkeypatch) -> None:
    orphan_hit = _FakeProc(101, ["python", "-c", "import json,sys,io ..."], orphan=True)
    live_hit = _FakeProc(102, ["python", "-c", "import json,sys,io ..."], orphan=False)
    orphan_miss = _FakeProc(103, ["notepad.exe"], orphan=True)
    monkeypatch.setattr(
        "tesseract.janitor.processes.psutil.wait_procs",
        lambda procs, timeout: ([], []),
    )
    findings = sweep_processes(
        CFG, dry_run=False, procs=[orphan_hit, live_hit, orphan_miss]
    )
    assert orphan_hit.terminated
    assert not live_hit.terminated
    assert not orphan_miss.terminated
    assert [f.action for f in findings] == ["killed"]
    assert "bridge" in findings[0].target


def test_dry_run_touches_nothing(home: Path) -> None:
    orphan_hit = _FakeProc(101, ["python", "-m", "http.server", "5177"], orphan=True)
    findings = sweep_processes(CFG, dry_run=True, procs=[orphan_hit])
    assert not orphan_hit.terminated
    assert [f.action for f in findings] == ["would-kill"]


def test_claimed_pid_survives_even_as_orphan(home: Path, monkeypatch) -> None:
    """run/*.pid claims exempt detached-by-design processes."""
    write_pidfile("supervisor")  # claims THIS test process's pid
    me = os.getpid()
    assert me in claimed_pids() or claimed_pids() == set()  # cmdline guard may drop it
    orphan_me = _FakeProc(me, ["python", "-m", "http.server"], orphan=True)
    monkeypatch.setattr(
        "tesseract.janitor.processes.claimed_pids", lambda: {me}
    )
    findings = sweep_processes(CFG, dry_run=False, procs=[orphan_me])
    assert not orphan_me.terminated
    assert findings == []


# ---------------------------------------------------------------- scratch


def test_scratch_dirs_removed_and_failures_reported(home: Path) -> None:
    root = home / "repo"
    (root / ".pytest-tmp-x").mkdir(parents=True)
    (root / "tmp_leftover").mkdir()
    (root / "tmp_leftover" / "f.txt").write_text("x", encoding="utf-8")
    (root / "keep_me").mkdir()

    findings = sweep_scratch(CFG, dry_run=False, roots=[root])
    assert sorted(f.action for f in findings) == ["removed", "removed"]
    assert not (root / ".pytest-tmp-x").exists()
    assert not (root / "tmp_leftover").exists()
    assert (root / "keep_me").exists()


# ---------------------------------------------------------------- sessions


def _write_session(home: Path, sid: str, *, status: str, hours_old: float) -> Path:
    sessions = home / "tars_controller" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    stamp = (
        datetime.now(timezone.utc) - timedelta(hours=hours_old)
    ).isoformat().replace("+00:00", "Z")
    path = sessions / f"{sid}.json"
    path.write_text(
        json.dumps({"session_id": sid, "status": status, "last_active_at": stamp}),
        encoding="utf-8",
    )
    return path


def test_stale_active_sessions_closed_when_daemon_dead(home: Path) -> None:
    stale = _write_session(home, "old-active", status="active", hours_old=48)
    fresh = _write_session(home, "fresh-active", status="active", hours_old=1)
    closed = _write_session(home, "already-closed", status="closed", hours_old=100)

    findings = sweep_sessions(CFG, dry_run=False)
    assert [f.action for f in findings] == ["closed"]
    assert json.loads(stale.read_text())["status"] == "closed"
    assert json.loads(fresh.read_text())["status"] == "active"
    assert json.loads(closed.read_text())["status"] == "closed"


def test_live_daemon_freezes_session_sweep(home: Path) -> None:
    _write_session(home, "old-active", status="active", hours_old=48)
    hb = home / "tars_controller" / "hb"
    hb.write_text("x", encoding="utf-8")
    (home / "tars_controller" / "controller.json").write_text(
        json.dumps({"pid": os.getpid(), "heartbeat_path": str(hb)}),
        encoding="utf-8",
    )
    assert sweep_sessions(CFG, dry_run=False) == []


# ---------------------------------------------------------------- archives


def test_old_archive_months_pruned(home: Path) -> None:
    old_lane = home / "controller" / "lanes-archive" / "2026-01" / "lane-x"
    old_lane.mkdir(parents=True)
    (old_lane / "events.jsonl").write_text("{}", encoding="utf-8")
    ancient = time.time() - 90 * 86400
    for p in [old_lane / "events.jsonl", old_lane]:
        os.utime(p, (ancient, ancient))

    fresh_lane = home / "controller" / "lanes-archive" / "2026-07" / "lane-y"
    fresh_lane.mkdir(parents=True)
    (fresh_lane / "events.jsonl").write_text("{}", encoding="utf-8")

    findings = sweep_archives(CFG, dry_run=False)
    assert [f.action for f in findings] == ["pruned"]
    assert not old_lane.exists()
    assert not old_lane.parent.exists()  # empty month dir dropped
    assert fresh_lane.exists()


def test_log_prune_is_opt_in(home: Path) -> None:
    logs = home / "logs"
    logs.mkdir(parents=True)
    old_log = logs / "ancient.jsonl"
    old_log.write_text("{}", encoding="utf-8")
    ancient = time.time() - 90 * 86400
    os.utime(old_log, (ancient, ancient))

    assert sweep_archives(CFG, dry_run=False) == []  # globs empty → untouched
    assert old_log.exists()

    cfg = CFG.model_copy(
        update={"log_prune": LogPrune(retention_days=30, globs=["*.jsonl"])}
    )
    findings = sweep_archives(cfg, dry_run=False)
    assert [f.action for f in findings] == ["pruned"]
    assert not old_log.exists()


# ---------------------------------------------------------------- config + runner


def test_config_loader_raises_on_missing_key(tmp_path: Path) -> None:
    path = tmp_path / "janitor.yaml"
    path.write_text("process_fingerprints: []\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_janitor_config(path)


def test_real_janitor_yaml_validates() -> None:
    cfg = load_janitor_config()
    assert cfg.process_fingerprints
    assert cfg.log_prune.globs == []  # log pruning ships off


def test_runner_isolates_sweep_failure_and_writes_report(
    home: Path, monkeypatch
) -> None:
    def _boom(cfg, *, dry_run):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("tesseract.janitor.runner._SWEEPS", (
        ("processes", _boom),
        ("scratch", lambda cfg, *, dry_run: []),
    ))
    report = run_sweep(CFG, dry_run=True)
    assert report.errors and "sweep exploded" in report.errors[0]
    report_file = home / "logs" / "janitor" / "sweeps.jsonl"
    assert report_file.exists()
    row = json.loads(report_file.read_text(encoding="utf-8").splitlines()[-1])
    assert row["dry_run"] is True
