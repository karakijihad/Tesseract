"""Regression suite for the conscience/drift subsystem (2026-04-24 rewire).

Covers:
  - `tesseract.conscience.drift.evaluate_drift` — all three signals against
    fixture runs.jsonl + circuit-breaker JSONL files.
  - `tesseract.conscience.config.load_drift_config` — required-key raises.
  - `tesseract.scheduler.tasks.conscience_heartbeat.ConscienceHeartbeatJob`
    — writes `drift-YYYY-MM-DD.jsonl`, returns `JobResult` with summary
    payload, never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.conscience.config import load_drift_config
from tesseract.conscience.drift import evaluate_drift
from tesseract.scheduler.tasks.conscience_heartbeat import ConscienceHeartbeatJob
from tesseract.scheduler.types import JobContext


NOW = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)


_DEFAULT_THRESHOLDS = {
    "circuit_breaker_open_count": {"warn": 1.0, "bad": 3.0},
    "scheduler_failure_rate": {"warn": 0.10, "bad": 0.30},
    "scheduler_idle_hours": {"warn": 6.0, "bad": 24.0},
}


def _write_runs(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _run_entry(*, minutes_ago: int, ok: bool, name: str = "tick") -> dict:
    fired_at = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "job_name": name,
        "run_id": f"rid-{minutes_ago}-{ok}",
        "fired_at": fired_at,
        "completed_at": fired_at,
        "ok": ok,
        "detail": "",
        "payload": {},
        "duration_ms": 1.0,
    }


def _write_breaker(dir_: Path, name: str, events: list[str]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps({"event": evt, "timestamp": NOW.isoformat()}) + "\n")


# ── drift signals ─────────────────────────────────────────────────────────


def test_no_logs_returns_bad_idle_ok_rest(tmp_path: Path) -> None:
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    by_name = {s.name: s for s in report.signals}
    assert by_name["scheduler_idle_hours"].status == "bad"
    assert by_name["scheduler_idle_hours"].detail == "no_runs_in_window"
    assert by_name["scheduler_failure_rate"].status == "ok"
    assert by_name["circuit_breaker_open_count"].value == 0.0
    assert by_name["circuit_breaker_open_count"].status == "ok"
    assert report.summary == {"ok": 2, "warn": 0, "bad": 1}


def test_all_runs_outside_window_report_bad_idle(tmp_path: Path) -> None:
    _write_runs(
        tmp_path / "schedule" / "runs.jsonl",
        [_run_entry(minutes_ago=25 * 60, ok=True)],  # 25h ago, outside window
    )
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    idle = next(s for s in report.signals if s.name == "scheduler_idle_hours")
    assert idle.status == "bad"
    assert idle.detail == "no_runs_in_window"


def test_healthy_runs_are_all_ok(tmp_path: Path) -> None:
    _write_runs(
        tmp_path / "schedule" / "runs.jsonl",
        [
            _run_entry(minutes_ago=5, ok=True),
            _run_entry(minutes_ago=60, ok=True),
            _run_entry(minutes_ago=120, ok=True),
        ],
    )
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    assert report.summary == {"ok": 3, "warn": 0, "bad": 0}
    idle = next(s for s in report.signals if s.name == "scheduler_idle_hours")
    assert idle.value < 1.0


def test_warn_and_bad_failure_rate(tmp_path: Path) -> None:
    # 15% failure → warn (warn=0.10, bad=0.30)
    runs = [_run_entry(minutes_ago=i, ok=(i % 7 != 0)) for i in range(1, 21)]
    _write_runs(tmp_path / "schedule" / "runs.jsonl", runs)
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    rate = next(s for s in report.signals if s.name == "scheduler_failure_rate")
    assert rate.status == "warn"
    assert 0.10 <= rate.value < 0.30

    # 50% failure → bad
    runs_bad = [_run_entry(minutes_ago=i, ok=(i % 2 == 0)) for i in range(1, 21)]
    _write_runs(tmp_path / "schedule" / "runs.jsonl", runs_bad)
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    rate_bad = next(s for s in report.signals if s.name == "scheduler_failure_rate")
    assert rate_bad.status == "bad"


def test_idle_hours_classification(tmp_path: Path) -> None:
    # Most recent run is 10h ago → warn (warn=6h, bad=24h)
    _write_runs(
        tmp_path / "schedule" / "runs.jsonl",
        [
            _run_entry(minutes_ago=10 * 60, ok=True),
            _run_entry(minutes_ago=12 * 60, ok=True),
        ],
    )
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    idle = next(s for s in report.signals if s.name == "scheduler_idle_hours")
    assert idle.status == "warn"
    assert 6.0 <= idle.value < 24.0


def test_circuit_breaker_open_count(tmp_path: Path) -> None:
    breakers = tmp_path / "breakers"
    _write_breaker(breakers, "vault_librarian", ["tripped", "reset", "tripped"])  # open
    _write_breaker(breakers, "memory_save", ["tripped", "reset"])  # closed
    _write_breaker(breakers, "observer", ["tripped"])  # open
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=breakers,
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    cb = next(s for s in report.signals if s.name == "circuit_breaker_open_count")
    assert cb.value == 2.0
    assert cb.status == "warn"
    assert "vault_librarian" in cb.detail
    assert "observer" in cb.detail
    assert "memory_save" not in cb.detail


def test_circuit_breaker_bad_threshold(tmp_path: Path) -> None:
    breakers = tmp_path / "breakers"
    for i in range(4):
        _write_breaker(breakers, f"svc{i}", ["tripped"])
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=breakers,
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
    )
    cb = next(s for s in report.signals if s.name == "circuit_breaker_open_count")
    assert cb.value == 4.0
    assert cb.status == "bad"


# ── config loader ─────────────────────────────────────────────────────────


def test_load_drift_config_from_repo_yaml() -> None:
    cfg = load_drift_config()
    assert cfg.window_hours > 0
    for name in ("circuit_breaker_open_count", "scheduler_failure_rate", "scheduler_idle_hours"):
        assert name in cfg.thresholds
        assert "warn" in cfg.thresholds[name]
        assert "bad" in cfg.thresholds[name]


def test_load_drift_config_raises_on_missing_key(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("drift:\n  signals: {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="window_hours"):
        load_drift_config(bad)


def test_load_drift_config_raises_on_missing_drift_block(tmp_path: Path) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("other: {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="drift"):
        load_drift_config(bad)


# ── heartbeat job ─────────────────────────────────────────────────────────


def _job_ctx(tmp_path: Path, *, fired_at: datetime = NOW) -> JobContext:
    return JobContext(
        job_name="conscience_heartbeat",
        fired_at=fired_at,
        app=None,
        config={},
        log_dir=tmp_path / "schedule",
    )


async def test_heartbeat_writes_jsonl_and_returns_ok(tmp_path: Path) -> None:
    _write_runs(
        tmp_path / "schedule" / "runs.jsonl",
        [_run_entry(minutes_ago=30, ok=True)],
    )
    result = await ConscienceHeartbeatJob().run(_job_ctx(tmp_path))
    assert result.ok is True
    assert "signals=3" in result.detail
    assert "summary" in result.payload

    drift_dir = tmp_path / "conscience"
    files = list(drift_dir.glob("drift-*.jsonl"))
    assert len(files) == 1
    report_line = files[0].read_text(encoding="utf-8").strip()
    record = json.loads(report_line)
    assert record["window_hours"] > 0
    assert len(record["signals"]) == 3


async def test_heartbeat_writes_empty_ok_when_no_logs(tmp_path: Path) -> None:
    # No runs.jsonl, no breakers dir — job must still write a report.
    result = await ConscienceHeartbeatJob().run(_job_ctx(tmp_path))
    assert result.ok is True
    assert result.payload["summary"]["bad"] >= 1  # idle signal goes bad


async def test_heartbeat_never_raises_on_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tesseract.scheduler.tasks.conscience_heartbeat as mod

    def _boom(*_a, **_kw):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(mod, "load_drift_config", _boom)
    result = await ConscienceHeartbeatJob().run(_job_ctx(tmp_path))
    assert result.ok is False
    assert "synthetic" in result.detail


# ── a1 — idle-signal carve-out ────────────────────────────────────────────


def test_idle_carveout_when_no_enabled_jobs(tmp_path: Path) -> None:
    # No runs.jsonl, but operator has zero enabled jobs → not bad.
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
        enabled_job_count=0,
    )
    idle = next(s for s in report.signals if s.name == "scheduler_idle_hours")
    assert idle.status == "ok"
    assert idle.detail == "no_enabled_jobs"


def test_idle_still_bad_when_jobs_enabled_but_silent(tmp_path: Path) -> None:
    report = evaluate_drift(
        schedule_log_dir=tmp_path / "schedule",
        breakers_dir=tmp_path / "breakers",
        thresholds=_DEFAULT_THRESHOLDS,
        window_hours=24,
        now=NOW,
        enabled_job_count=5,  # jobs exist but none fired
    )
    idle = next(s for s in report.signals if s.name == "scheduler_idle_hours")
    assert idle.status == "bad"
    assert idle.detail == "no_runs_in_window"


# ── a2 — transition detection + broadcast ─────────────────────────────────


def _write_prior_report(tmp_path: Path, summary: dict) -> None:
    conscience_dir = tmp_path / "conscience"
    conscience_dir.mkdir(parents=True, exist_ok=True)
    prior_date = NOW.date().isoformat()
    prior_file = conscience_dir / f"drift-{prior_date}.jsonl"
    prior_file.write_text(
        json.dumps(
            {
                "timestamp": (NOW - timedelta(days=1)).isoformat(),
                "window_hours": 24,
                "signals": [
                    {"name": "circuit_breaker_open_count", "status": "ok", "value": 0,
                     "warn": 1, "bad": 3, "detail": ""},
                    {"name": "scheduler_failure_rate", "status": "ok", "value": 0,
                     "warn": 0.1, "bad": 0.3, "detail": ""},
                    {"name": "scheduler_idle_hours", "status": "ok", "value": 0.1,
                     "warn": 6, "bad": 24, "detail": ""},
                ],
                "summary": summary,
            }
        ) + "\n",
        encoding="utf-8",
    )


async def test_heartbeat_emits_transition_on_escalation(tmp_path: Path) -> None:
    # Prior report was fully ok; current run has a bad signal — transition fires.
    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})
    # No runs + jobs enabled → idle becomes bad on this run.
    ctx = JobContext(
        job_name="conscience_heartbeat",
        fired_at=NOW,
        app=None,  # skips WS + mood broadcast; transition is still recorded in payload
        config={},
        log_dir=tmp_path / "schedule",
    )
    result = await ConscienceHeartbeatJob().run(ctx)
    assert result.ok is True
    assert "transition=ok->bad" in result.detail
    assert result.payload["transition"]["from"] == "ok"
    assert result.payload["transition"]["to"] == "bad"


async def test_heartbeat_no_transition_on_steady_state(tmp_path: Path) -> None:
    _write_prior_report(tmp_path, summary={"ok": 2, "warn": 0, "bad": 1})
    # No runs, no enabled_job_count carve-out → idle bad. Worst status still "bad".
    result = await ConscienceHeartbeatJob().run(_job_ctx(tmp_path))
    assert result.ok is True
    assert "transition" not in result.detail
    assert "transition" not in result.payload


async def test_heartbeat_write_failure_skips_broadcast_and_returns_not_ok(
    tmp_path: Path, monkeypatch
) -> None:
    """On OSError during JSONL append, heartbeat logs + skips broadcast to avoid desync."""
    from tesseract.scheduler.tasks import conscience_heartbeat as mod

    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})

    def _fail_write(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(mod, "_write_report", _fail_write)

    from tesseract.orchestrator.mood_state import MoodState
    mood = MoodState(intensity=0.5, valence=0.1)

    class _FakeApp:
        def __init__(self):
            self._d = {"mood": mood, "server_sessions": {}, "scheduler": None}
        def get(self, k, default=None):
            return self._d.get(k, default)

    ctx = JobContext(
        job_name="conscience_heartbeat",
        fired_at=NOW,
        app=_FakeApp(),
        config={},
        log_dir=tmp_path / "schedule",
    )
    result = await ConscienceHeartbeatJob().run(ctx)
    assert result.ok is False, "write failure must flip ok=False so the run log shows desync"
    assert "write_failed=true" in result.detail
    assert result.payload["write_ok"] is False
    assert "transition" not in result.payload, "no transition payload when write failed"
    # Mood must NOT move — desync guard also covers the mood bridge.
    assert mood.valence == pytest.approx(0.1)
    assert mood.intensity == pytest.approx(0.5)


async def test_heartbeat_mood_nudge_on_transition(tmp_path: Path) -> None:
    from tesseract.orchestrator.mood_state import MoodState

    mood = MoodState(intensity=0.5, valence=0.1)

    class _FakeApp:
        def __init__(self):
            self._d = {"mood": mood, "server_sessions": {}, "scheduler": None}

        def get(self, k, default=None):
            return self._d.get(k, default)

    _write_prior_report(tmp_path, summary={"ok": 3, "warn": 0, "bad": 0})
    ctx = JobContext(
        job_name="conscience_heartbeat",
        fired_at=NOW,
        app=_FakeApp(),
        config={},
        log_dir=tmp_path / "schedule",
    )
    result = await ConscienceHeartbeatJob().run(ctx)
    assert result.ok is True
    assert result.payload["transition"]["to"] == "bad"
    assert result.payload["mood_nudged"] is True
    # ok → bad nudges valence by -0.30 and intensity by +0.10.
    assert mood.valence < 0.0
    assert mood.intensity > 0.5


# ── a3 — conscience_status tool ───────────────────────────────────────────


async def test_conscience_status_tool_empty_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tesseract.kernel.tools.conscience as tool_mod
    from tesseract.kernel.tools.base import ToolContext

    monkeypatch.setattr(tool_mod, "_DRIFT_DIR", tmp_path / "empty")
    result = await tool_mod.ConscienceStatusTool().run(
        tool_mod.ConscienceStatusInput(verbose=False),
        ToolContext(),
    )
    assert "no report yet" in result.output.lower()
    assert result.metadata["report_available"] is False


async def test_conscience_status_tool_renders_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tesseract.kernel.tools.conscience as tool_mod
    from tesseract.kernel.tools.base import ToolContext

    drift_dir = tmp_path / "conscience"
    drift_dir.mkdir()
    (drift_dir / f"drift-{NOW.date().isoformat()}.jsonl").write_text(
        json.dumps(
            {
                "timestamp": NOW.isoformat(),
                "window_hours": 24,
                "signals": [
                    {"name": "circuit_breaker_open_count", "status": "warn", "value": 1,
                     "warn": 1, "bad": 3, "detail": "vault_librarian"},
                    {"name": "scheduler_failure_rate", "status": "ok", "value": 0,
                     "warn": 0.1, "bad": 0.3, "detail": "0/10 failed"},
                    {"name": "scheduler_idle_hours", "status": "ok", "value": 0.5,
                     "warn": 6, "bad": 24, "detail": ""},
                ],
                "summary": {"ok": 2, "warn": 1, "bad": 0},
            }
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tool_mod, "_DRIFT_DIR", drift_dir)
    result = await tool_mod.ConscienceStatusTool().run(
        tool_mod.ConscienceStatusInput(verbose=False),
        ToolContext(),
    )
    assert "worst: warn" in result.output
    assert "circuit_breaker_open_count" in result.output  # flagged list
    assert result.metadata["report_available"] is True
    assert result.metadata["summary"]["warn"] == 1


# ── Push 1 — chat session ingest_conscience_transition ────────────────────


def test_chat_ingest_conscience_transition_drains_on_next_turn() -> None:
    from tesseract.brain.chat import ChatSession

    class _StubAdapter:
        def count_tokens(self, _msgs):
            return 0

    session = ChatSession(
        adapter=_StubAdapter(),
        system_prompt="",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
    )
    session.ingest_conscience_transition({
        "from": "ok", "to": "bad",
        "summary": {"ok": 2, "warn": 0, "bad": 1},
        "changed_signals": [{"name": "circuit_breaker_open_count", "from": "ok", "to": "bad"}],
    })
    drained = session._drain_pending_suggestions()
    assert "[conscience_drift]" in drained
    assert "ok → bad" in drained
    assert "circuit_breaker_open_count" in drained
    # Queue is now empty.
    assert session._drain_pending_suggestions() == ""


# ── Push 2 — prompt.py _drift_snippet ─────────────────────────────────────


def test_drift_snippet_emits_when_non_ok(tmp_path: Path) -> None:
    from tesseract.brain.prompt import _drift_snippet

    (tmp_path / f"drift-{NOW.date().isoformat()}.jsonl").write_text(
        json.dumps(
            {
                "timestamp": NOW.isoformat(),
                "window_hours": 24,
                "signals": [
                    {"name": "scheduler_failure_rate", "status": "bad", "value": 0.5,
                     "warn": 0.1, "bad": 0.3, "detail": "5/10 failed"},
                ],
                "summary": {"ok": 2, "warn": 0, "bad": 1},
            }
        ) + "\n",
        encoding="utf-8",
    )
    snippet = _drift_snippet(conscience_dir=tmp_path)
    assert snippet.startswith("- Drift: bad")
    assert "scheduler_failure_rate" in snippet


def test_drift_snippet_empty_when_healthy(tmp_path: Path) -> None:
    from tesseract.brain.prompt import _drift_snippet

    (tmp_path / f"drift-{NOW.date().isoformat()}.jsonl").write_text(
        json.dumps({"timestamp": NOW.isoformat(), "window_hours": 24,
                    "signals": [], "summary": {"ok": 3, "warn": 0, "bad": 0}}) + "\n",
        encoding="utf-8",
    )
    assert _drift_snippet(conscience_dir=tmp_path) == ""


def test_drift_snippet_empty_when_no_report(tmp_path: Path) -> None:
    from tesseract.brain.prompt import _drift_snippet
    assert _drift_snippet(conscience_dir=tmp_path / "missing") == ""


# ── b1 — observer log append ──────────────────────────────────────────────


def test_observer_log_append_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tesseract.brain.observer as observer_mod

    monkeypatch.setattr(observer_mod, "_OBSERVER_LOG_DIR", tmp_path / "observer")
    observer_mod._append_observation_log(
        mode="meta", session_id="sess-1", text="subtle pivot in topic"
    )
    files = list((tmp_path / "observer").glob("*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["mode"] == "meta"
    assert rec["session_id"] == "sess-1"
    assert rec["text"] == "subtle pivot in topic"


def test_observer_log_append_is_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point log dir at a path that can't be written (file in place of dir)
    import tesseract.brain.observer as observer_mod

    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(observer_mod, "_OBSERVER_LOG_DIR", blocker / "nested")
    # Must not raise.
    observer_mod._append_observation_log(
        mode="meta", session_id="", text="x"
    )
