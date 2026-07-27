"""Regression suite for scheduler S5 — Mirror schedule tab.

Covers: `GET /api/schedule` shape (online + scheduler=None); engine runtime
mutators (set_enabled, set_cadence + cron-only cadences); `_run_job`
broadcasts `schedule_job_started`/`schedule_job_done` to live WS sessions;
`/schedule-*` command handlers emit `schedule_state` envelopes with the
right action + runtime snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tesseract.mirror.server import commands as cmd_mod
from tesseract.mirror.server.routes import schedule as schedule_route
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.engine import MAX_CONSECUTIVE_FAILURES, SchedulerEngine
from tesseract.scheduler.types import JobContext, JobResult


NOW = datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc)


# ── probes ────────────────────────────────────────────────────────────────


class _OkJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True, detail="ok")


class _FailJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=False, detail="nope")


_OK_DOTPATH = f"{__name__}._OkJob"
_FAIL_DOTPATH = f"{__name__}._FailJob"


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _fake_session(session_id: str = "sess-s5") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        ws=_FakeWS(),
        event_log=[],
    )


def _minimal_job(**overrides):
    base = {
        "name": "job1",
        "cadence": "*/1 * * * *",
        "handler": _OK_DOTPATH,
        "enabled": True,
        "on_failure": "log",
        "retry_policy": {"max_retries": 0, "backoff_seconds": 0},
    }
    base.update(overrides)
    return base


def _build_engine(tmp_path: Path, *jobs_override) -> SchedulerEngine:
    jobs = list(jobs_override) if jobs_override else [_minimal_job()]
    payload = {"catchup": {"concurrency": 8}, "jobs": jobs}
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "schedule.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return SchedulerEngine(config_dir=config_dir, log_dir=tmp_path / "logs")


# ── /api/schedule route ────────────────────────────────────────────────────


async def _make_client(scheduler) -> TestClient:
    app = web.Application()
    app["scheduler"] = scheduler
    app.router.add_get("/api/schedule", schedule_route.list_jobs)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


async def test_api_schedule_returns_seeded_jobs(tmp_path):
    engine = _build_engine(
        tmp_path,
        _minimal_job(name="a"),
        _minimal_job(name="b", enabled=False),
    )
    client = await _make_client(engine)
    try:
        resp = await client.get("/api/schedule")
        assert resp.status == 200
        body = await resp.json()
        assert [j["name"] for j in body["jobs"]] == ["a", "b"]
        a, b = body["jobs"]
        assert a["enabled"] is True
        assert b["enabled"] is False
        # runtime snapshot present
        assert a["runtime"]["cadence"] == "*/1 * * * *"
        assert a["runtime"]["circuit_broken"] is False
        assert a["runtime"]["consecutive_failures"] == 0
    finally:
        await client.close()


async def test_api_schedule_null_runtime_when_scheduler_offline():
    client = await _make_client(None)
    try:
        resp = await client.get("/api/schedule")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"jobs": []}
    finally:
        await client.close()


# ── engine mutators ───────────────────────────────────────────────────────


def test_set_enabled_flips_flag(tmp_path):
    engine = _build_engine(tmp_path)
    engine.set_enabled("job1", False)
    assert engine.runtime_state("job1")["enabled"] is False
    engine.set_enabled("job1", True)
    assert engine.runtime_state("job1")["enabled"] is True


def test_set_enabled_resets_breaker_on_re_enable(tmp_path):
    engine = _build_engine(tmp_path)
    rt = engine.registry["job1"]
    rt.consecutive_failures = MAX_CONSECUTIVE_FAILURES
    rt.enabled = False

    engine.set_enabled("job1", True)

    assert rt.consecutive_failures == 0
    assert engine.runtime_state("job1")["circuit_broken"] is False


def test_set_enabled_unknown_raises(tmp_path):
    engine = _build_engine(tmp_path)
    with pytest.raises(KeyError):
        engine.set_enabled("ghost", True)


def test_set_cadence_accepts_interval_shorthand(tmp_path):
    engine = _build_engine(tmp_path)
    engine.set_cadence("job1", "15m")
    rt = engine.registry["job1"]
    assert rt.cfg.cadence == "15m"
    assert rt.interval_seconds == 15 * 60


def test_set_cadence_accepts_cron(tmp_path):
    engine = _build_engine(tmp_path)
    engine.set_cadence("job1", "0 3 * * *")
    rt = engine.registry["job1"]
    assert rt.cfg.cadence == "0 3 * * *"
    assert rt.interval_seconds is None  # cron path


def test_set_cadence_rejects_garbage(tmp_path):
    engine = _build_engine(tmp_path)
    with pytest.raises(ValueError):
        engine.set_cadence("job1", "not-a-cron")


def test_configs_property_returns_snapshot(tmp_path):
    engine = _build_engine(
        tmp_path,
        _minimal_job(name="a"),
        _minimal_job(name="b"),
    )
    assert [c.name for c in engine.configs] == ["a", "b"]


# ── _run_job broadcasts ────────────────────────────────────────────────────


async def test_run_job_broadcasts_started_and_done(tmp_path):
    engine = _build_engine(tmp_path)
    s1 = _fake_session("one")
    s2 = _fake_session("two")
    engine._app = {"server_sessions": {"one": s1, "two": s2}}

    rt = engine.registry["job1"]
    result = await engine._run_job("job1", rt, NOW)

    assert result.ok is True
    for sess in (s1, s2):
        types_seen = [env["type"] for env in sess.ws.sent]
        assert "schedule_job_started" in types_seen
        assert "schedule_job_done" in types_seen
        started = next(e for e in sess.ws.sent if e["type"] == "schedule_job_started")
        done = next(e for e in sess.ws.sent if e["type"] == "schedule_job_done")
        assert started["category"] == "schedule"
        assert done["category"] == "schedule"
        assert started["data"]["job_name"] == "job1"
        assert done["data"]["ok"] is True
        # same run_id across started+done
        assert started["data"]["run_id"] == done["data"]["run_id"]


async def test_run_job_broadcasts_failed_on_alert_mode(tmp_path):
    engine = _build_engine(
        tmp_path,
        _minimal_job(name="alerts", handler=_FAIL_DOTPATH, on_failure="alert"),
    )
    sess = _fake_session()
    engine._app = {"server_sessions": {"alerts": sess}}

    rt = engine.registry["alerts"]
    await engine._run_job("alerts", rt, NOW)

    types_seen = [e["type"] for e in sess.ws.sent]
    assert "schedule_job_failed" in types_seen
    failed = next(e for e in sess.ws.sent if e["type"] == "schedule_job_failed")
    assert failed["data"]["ok"] is False
    assert failed["data"]["consecutive_failures"] == 1


async def test_run_job_no_sessions_is_noop(tmp_path):
    engine = _build_engine(tmp_path)
    engine._app = {"server_sessions": {}}

    rt = engine.registry["job1"]
    # Must not raise
    result = await engine._run_job("job1", rt, NOW)
    assert result.ok is True


# ── WS /schedule-* commands ────────────────────────────────────────────────


async def test_cmd_schedule_enable_disable_round_trip(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_disable(app, session, "job1")
    await cmd_mod.cmd_schedule_enable(app, session, "job1")

    envs = [e for e in session.ws.sent if e["type"] == "schedule_state"]
    assert [e["data"]["action"] for e in envs] == ["disabled", "enabled"]
    assert envs[0]["data"]["enabled"] is False
    assert envs[1]["data"]["enabled"] is True
    assert envs[0]["category"] == "schedule"


async def test_cmd_schedule_enable_missing_arg(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_enable(app, session, "")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_invalid"


async def test_cmd_schedule_enable_unknown_job(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_enable(app, session, "ghost")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_not_found"
    assert env["data"]["job_name"] == "ghost"


async def test_cmd_schedule_enable_scheduler_offline():
    session = _fake_session()
    app = {"scheduler": None}

    await cmd_mod.cmd_schedule_enable(app, session, "job1")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_unavailable"


async def test_cmd_schedule_set_cadence_updates_runtime(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_set_cadence(app, session, "job1 30m")

    assert engine.registry["job1"].cfg.cadence == "30m"
    env = session.ws.sent[-1]
    assert env["data"]["action"] == "cadence_set"
    assert env["data"]["cadence"] == "30m"


async def test_cmd_schedule_set_cadence_rejects_garbage(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_set_cadence(app, session, "job1 not-a-cron")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_invalid"


async def test_cmd_schedule_set_cadence_missing_args(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_set_cadence(app, session, "job1")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_invalid"


# cmd_schedule_add / cmd_schedule_remove removed in Phase 15. Runtime job
# creation moves to Phase 18 (schedule_create tool).
