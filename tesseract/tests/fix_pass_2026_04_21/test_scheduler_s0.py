"""Regression suite for scheduler S0 — heartbeat runtime.

Covers: config load happy + error paths, frozen result, abstract base,
circuit-breaker trip + reset, runs.jsonl append, engine start/stop clean.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.config_loader import load_schedule_config
from tesseract.scheduler.engine import MAX_CONSECUTIVE_FAILURES, SchedulerEngine, _parse_interval
from tesseract.scheduler.log import append_run_log, load_last_runs
from tesseract.scheduler.types import JobContext, JobResult


_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO_ROOT / "tesseract" / "config"


def _write_schedule_yaml(root: Path, payload: dict) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"catchup": {"concurrency": 8}, **payload}
    (config_dir / "schedule.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_dir


def _minimal_job(**overrides):
    base = {
        "name": "ticker",
        "cadence": "*/1 * * * *",
        "handler": "tests.does_not_exist.NoOp",
        "enabled": True,
        "on_failure": "log",
        "retry_policy": {"max_retries": 0, "backoff_seconds": 0},
    }
    base.update(overrides)
    return base


class _FailingJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=False, detail="boom")


class _SucceedingJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True, detail="fine")


def _build_engine(tmp_path: Path, job_overrides: dict | None = None) -> SchedulerEngine:
    payload = {"jobs": [_minimal_job(**(job_overrides or {}))]}
    config_dir = _write_schedule_yaml(tmp_path, payload)
    engine = SchedulerEngine(config_dir=config_dir, log_dir=tmp_path / "logs")
    # Tests register a bogus `tests.does_not_exist.NoOp` handler so they can
    # mutate `rt.handler_cls` on the fly. audit-1 m1 (2026-04-24) forces
    # placeholder rows `enabled=False`; re-enable here to simulate the
    # post-deploy state where the handler path really does import.
    for rt in engine.registry.values():
        rt.enabled = True
    return engine


# ── config loader ─────────────────────────────────────────────────────────

def test_load_schedule_config_parses_seed():
    cfg = load_schedule_config(_CONFIG_DIR)
    # Job count is intentionally not pinned — adding a new scheduled job
    # must not break this test. Regression coverage lives in the named
    # asserts below: each one guards a specific job we shipped and must
    # keep shipping. Uniqueness guards against accidental duplicates.
    names = [j.name for j in cfg.jobs]
    assert len(names) == len(set(names)), f"duplicate job names: {names}"
    assert any(j.name == "daily_writer" for j in cfg.jobs)
    assert any(j.name == "sessions_archive" for j in cfg.jobs)
    assert any(j.name == "daily_brief" for j in cfg.jobs)
    assert any(j.name == "provider_watch" for j in cfg.jobs)
    assert any(j.name == "interests_decay" for j in cfg.jobs)
    # vault_lint went live on 2026-04-22 after vault-librarian-rewire closed (S6).
    assert any(j.name == "vault_lint" and j.enabled is True for j in cfg.jobs)
    # chat_digest landed on 2026-04-23 as part of memory-retune M3.
    assert any(j.name == "chat_digest" and j.enabled is True for j in cfg.jobs)
    # conscience_heartbeat added 2026-04-24; operator-enabled in schedule.yaml as
    # of Phase 15 (cadence 0 22 * * * — 22:00 daily). Operator can flip via the
    # Mirror schedule view; this test pins the shipped default.
    assert any(j.name == "conscience_heartbeat" and j.enabled is True for j in cfg.jobs)
    # dream_cycle added 2026-04-29 as part of audit-3 M2 fix — wires the
    # nightly memory consolidation that the engine class had supported
    # since Phase 4 but no scheduler entry ever invoked.
    assert any(j.name == "dream_cycle" and j.enabled is True for j in cfg.jobs)


def test_load_schedule_config_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_schedule_config(tmp_path)


def test_load_schedule_config_bad_schema_raises(tmp_path):
    # missing required 'handler'
    (tmp_path / "schedule.yaml").write_text(
        "jobs:\n  - name: x\n    cadence: '* * * * *'\n    enabled: true\n    on_failure: log\n    retry_policy: {max_retries: 0, backoff_seconds: 0}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_schedule_config(tmp_path)


def test_load_schedule_config_rejects_unknown_on_failure(tmp_path):
    config_dir = _write_schedule_yaml(tmp_path, {"jobs": [_minimal_job(on_failure="explode")]})
    with pytest.raises(ValidationError):
        load_schedule_config(config_dir)


# ── types / base ─────────────────────────────────────────────────────────

def test_job_result_frozen():
    r = JobResult(job_name="a", run_id="rid", ok=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_base_job_is_abstract():
    with pytest.raises(TypeError):
        BaseJob()  # type: ignore[abstract]


# ── interval parser ─────────────────────────────────────────────────────

def test_parse_interval_shorthand():
    assert _parse_interval("15m") == 900
    assert _parse_interval("1h") == 3600
    assert _parse_interval("30s") == 30
    assert _parse_interval("0 0 * * *") is None
    assert _parse_interval("not an interval") is None


# ── circuit breaker ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_trips_at_three(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = _build_engine(tmp_path)
    rt = engine.registry["ticker"]
    rt.handler_cls = _FailingJob
    now = datetime.now(timezone.utc)
    for _ in range(MAX_CONSECUTIVE_FAILURES):
        await engine._run_job("ticker", rt, now)
    assert rt.consecutive_failures == MAX_CONSECUTIVE_FAILURES
    assert rt.enabled is False


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = _build_engine(tmp_path)
    rt = engine.registry["ticker"]
    rt.handler_cls = _FailingJob
    now = datetime.now(timezone.utc)
    await engine._run_job("ticker", rt, now)
    await engine._run_job("ticker", rt, now)
    assert rt.consecutive_failures == 2
    rt.handler_cls = _SucceedingJob
    await engine._run_job("ticker", rt, now)
    assert rt.consecutive_failures == 0
    assert rt.enabled is True


@pytest.mark.asyncio
async def test_on_failure_disable_skips_breaker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = _build_engine(tmp_path, job_overrides={"on_failure": "disable"})
    rt = engine.registry["ticker"]
    rt.handler_cls = _FailingJob
    await engine._run_job("ticker", rt, datetime.now(timezone.utc))
    assert rt.enabled is False  # disabled immediately, not after 3


@pytest.mark.asyncio
async def test_on_failure_alert_enqueues(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = _build_engine(tmp_path, job_overrides={"on_failure": "alert"})
    rt = engine.registry["ticker"]
    rt.handler_cls = _FailingJob
    await engine._run_job("ticker", rt, datetime.now(timezone.utc))
    assert len(engine.pending_alerts) == 1
    assert engine.pending_alerts[0].ok is False


# ── run log ──────────────────────────────────────────────────────────────

def test_append_run_log_creates_dir_and_appends_json(tmp_path):
    ctx = JobContext(job_name="x", fired_at=datetime.now(timezone.utc))
    result = JobResult(job_name="x", run_id=ctx.run_id, ok=True, detail="ran", payload={"k": 1}, duration_ms=42.0)

    log_dir = tmp_path / "schedule"
    path = append_run_log(ctx, result, log_dir=log_dir)
    assert path.exists()

    append_run_log(ctx, result, log_dir=log_dir)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["job_name"] == "x"
    assert parsed["ok"] is True
    assert parsed["payload"] == {"k": 1}


# ── load_last_runs / catch-up ────────────────────────────────────────────

def test_load_last_runs_returns_latest_fired_at_per_job(tmp_path):
    log_dir = tmp_path / "schedule"
    ctx1 = JobContext(job_name="a", fired_at=datetime(2026, 4, 20, 10, tzinfo=timezone.utc))
    ctx2 = JobContext(job_name="a", fired_at=datetime(2026, 4, 21, 10, tzinfo=timezone.utc))
    ctx3 = JobContext(job_name="b", fired_at=datetime(2026, 4, 19, 10, tzinfo=timezone.utc))
    for ctx in (ctx1, ctx2, ctx3):
        append_run_log(ctx, JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True), log_dir=log_dir)
    latest = load_last_runs(log_dir=log_dir)
    assert latest["a"] == ctx2.fired_at
    assert latest["b"] == ctx3.fired_at


def test_load_last_runs_empty_when_file_missing(tmp_path):
    assert load_last_runs(log_dir=tmp_path / "nowhere") == {}


@pytest.mark.asyncio
async def test_catchup_refires_missed_cron(tmp_path):
    # Engine with daily noon cron, last successful run was 2 days ago.
    engine = _build_engine(tmp_path, job_overrides={"cadence": "0 12 * * *"})
    log_dir = tmp_path / "logs" / "schedule"
    log_dir.mkdir(parents=True)
    old_ctx = JobContext(job_name="ticker", fired_at=datetime(2026, 4, 18, 12, tzinfo=timezone.utc))
    append_run_log(
        old_ctx,
        JobResult(job_name="ticker", run_id=old_ctx.run_id, ok=True),
        log_dir=tmp_path / "logs",
    )
    now = datetime(2026, 4, 21, 9, tzinfo=timezone.utc)  # 21st 09:00 UTC, well past 20th noon
    assert engine._compute_catchup(now) == ["ticker"]


@pytest.mark.asyncio
async def test_catchup_skips_first_boot(tmp_path):
    # No runs.jsonl → no catch-up fires even though the cron time has passed.
    engine = _build_engine(tmp_path, job_overrides={"cadence": "0 12 * * *"})
    now = datetime(2026, 4, 21, 15, tzinfo=timezone.utc)
    assert engine._compute_catchup(now) == []


@pytest.mark.asyncio
async def test_catchup_respects_interval(tmp_path):
    engine = _build_engine(tmp_path, job_overrides={"cadence": "15m"})
    log_dir = tmp_path / "logs"
    (log_dir / "schedule").mkdir(parents=True)
    fired_at = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    append_run_log(
        JobContext(job_name="ticker", fired_at=fired_at),
        JobResult(job_name="ticker", run_id="r", ok=True),
        log_dir=log_dir,
    )
    # 10 min later → inside the 15-minute interval, no catch-up
    assert engine._compute_catchup(fired_at + timedelta(minutes=10)) == []
    # 20 min later → interval elapsed, catch-up
    assert engine._compute_catchup(fired_at + timedelta(minutes=20)) == ["ticker"]


# ── engine start/stop ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_start_stop_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = _build_engine(tmp_path)
    engine.tick_seconds = 0.01
    await engine.start(app=None)
    await asyncio.sleep(0.05)
    await engine.stop()
    # Only our single scheduler-tick task should have been created; everything cleaned up.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert pending == [], f"dangling tasks: {pending}"
