"""Regression suite for scheduler S6 — VaultLintJob + engine.run_now().

Covers:
  * VaultLintJob maps ToolResult.metadata['lint_report'] → JobResult.payload
  * VaultLintJob graceful diagnostics when tool_registry or vault_lint tool is absent
  * SchedulerEngine.run_now() fires off-schedule, writes runs.jsonl, resets/keeps breaker
  * SchedulerEngine.run_now() raises KeyError on unknown jobs
  * cmd_schedule_run_now emits schedule_state ack + handles error paths
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.mirror.server import commands as cmd_mod
from tesseract.scheduler import log as scheduler_log
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.engine import SchedulerEngine
from tesseract.scheduler.tasks.vault_lint import VaultLintJob
from tesseract.scheduler.types import JobContext, JobResult


NOW = datetime(2026, 4, 22, 16, 0, tzinfo=timezone.utc)


# ── fakes ────────────────────────────────────────────────────────────────


class _FakeVaultLintTool(Tool):
    """Mimics VaultLintTool contract — returns a canned lint report in metadata."""

    def __init__(self, report: dict, *, raises: Exception | None = None) -> None:
        self._report = report
        self._raises = raises

    @property
    def name(self) -> str:
        return "vault_lint"

    @property
    def description(self) -> str:
        return "fake"

    @property
    def input_schema(self):  # type: ignore[override]
        from tesseract.kernel.tools.vault_lint import VaultLintInput
        return VaultLintInput

    async def run(self, tool_input, context: ToolContext) -> ToolResult:
        if self._raises is not None:
            raise self._raises
        return ToolResult(output="fake", metadata={"lint_report": self._report})


class _FakeRegistry:
    def __init__(self, tools: dict) -> None:
        self.tools = tools


class _OkJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True, detail="ok")


_OK_DOTPATH = f"{__name__}._OkJob"


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _fake_session(session_id: str = "sess-s6") -> SimpleNamespace:
    return SimpleNamespace(session_id=session_id, ws=_FakeWS(), event_log=[])


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
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "schedule.yaml").write_text(
        yaml.safe_dump({"catchup": {"concurrency": 8}, "jobs": jobs}), encoding="utf-8"
    )
    return SchedulerEngine(config_dir=config_dir, log_dir=tmp_path / "logs")


# ── VaultLintJob ──────────────────────────────────────────────────────────


async def test_vault_lint_job_maps_report_into_payload():
    report = {
        "orphans": ["a", "b"],
        "stale": ["c"],
        "contradictions": [{"slug_a": "x", "slug_b": "y", "verdict": "weaken", "reason": "r"}],
        "missing_hubs": [{"term": "Ollama", "mention_count": 4, "suggested_slug": "ollama"}],
        "scale_alarm": False,
        "scale_page_count": 12,
        "failures": [],
    }
    app = {"tool_registry": _FakeRegistry({"vault_lint": _FakeVaultLintTool(report)})}
    ctx = JobContext(job_name="vault_lint", fired_at=NOW, app=app)

    result = await VaultLintJob().run(ctx)

    assert result.ok is True
    assert result.payload == report
    assert "orphans=2" in result.detail
    assert "stale=1" in result.detail
    assert "contradictions=1" in result.detail
    assert "missing_hubs=1" in result.detail
    assert "scale=ok" in result.detail


async def test_vault_lint_job_reports_scale_alarm_in_detail():
    report = {
        "orphans": [], "stale": [], "contradictions": [], "missing_hubs": [],
        "scale_alarm": True, "scale_page_count": 100, "failures": [],
    }
    app = {"tool_registry": _FakeRegistry({"vault_lint": _FakeVaultLintTool(report)})}
    ctx = JobContext(job_name="vault_lint", fired_at=NOW, app=app)

    result = await VaultLintJob().run(ctx)

    assert result.ok is True
    assert "scale=alarm" in result.detail


async def test_vault_lint_job_missing_registry_reports_diagnostic():
    ctx = JobContext(job_name="vault_lint", fired_at=NOW, app=None)
    result = await VaultLintJob().run(ctx)
    assert result.ok is False
    assert "tool_registry" in result.detail


async def test_vault_lint_job_missing_tool_reports_diagnostic():
    app = {"tool_registry": _FakeRegistry({})}  # empty registry, no vault_lint entry
    ctx = JobContext(job_name="vault_lint", fired_at=NOW, app=app)
    result = await VaultLintJob().run(ctx)
    assert result.ok is False
    assert "vault_lint" in result.detail


async def test_vault_lint_job_tool_exception_handled():
    app = {"tool_registry": _FakeRegistry({
        "vault_lint": _FakeVaultLintTool({}, raises=RuntimeError("boom")),
    })}
    ctx = JobContext(job_name="vault_lint", fired_at=NOW, app=app)
    result = await VaultLintJob().run(ctx)
    assert result.ok is False
    assert "boom" in result.detail


# ── SchedulerEngine.run_now ───────────────────────────────────────────────


async def test_run_now_fires_off_schedule_and_logs(tmp_path, monkeypatch):
    engine = _build_engine(tmp_path)
    engine._app = {"server_sessions": {}}
    monkeypatch.setattr(scheduler_log, "_DEFAULT_LOG_DIR", engine.log_dir)

    result = await engine.run_now("job1")

    assert result.ok is True
    rt = engine.registry["job1"]
    assert rt.last_fired_at is not None
    assert rt.last_result is not None and rt.last_result.ok is True

    runs_path = engine.log_dir / "runs.jsonl"
    assert runs_path.exists()
    lines = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["job_name"] == "job1"
    assert lines[0]["ok"] is True
    # run_now is explicitly NOT a catchup fire
    assert lines[0]["payload"].get("catchup") is not True


async def test_run_now_raises_on_unknown(tmp_path):
    engine = _build_engine(tmp_path)
    with pytest.raises(KeyError):
        await engine.run_now("ghost")


async def test_run_now_broadcasts_like_tick(tmp_path):
    engine = _build_engine(tmp_path)
    sess = _fake_session()
    engine._app = {"server_sessions": {"one": sess}}

    await engine.run_now("job1")

    types_seen = [env["type"] for env in sess.ws.sent]
    assert "schedule_job_started" in types_seen
    assert "schedule_job_done" in types_seen
    done = next(e for e in sess.ws.sent if e["type"] == "schedule_job_done")
    assert done["data"]["ok"] is True


# ── cmd_schedule_run_now ──────────────────────────────────────────────────


async def test_cmd_schedule_run_now_dispatches_and_acks(tmp_path):
    engine = _build_engine(tmp_path)
    engine._app = {"server_sessions": {}}
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_run_now(app, session, "job1")

    acks = [e for e in session.ws.sent if e["type"] == "schedule_state"]
    assert acks, "expected a schedule_state ack envelope"
    assert acks[0]["data"]["action"] == "run_now"
    assert acks[0]["data"]["job_name"] == "job1"


async def test_cmd_schedule_run_now_missing_arg(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_run_now(app, session, "")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_invalid"


async def test_cmd_schedule_run_now_unknown_job(tmp_path):
    engine = _build_engine(tmp_path)
    session = _fake_session()
    app = {"scheduler": engine}

    await cmd_mod.cmd_schedule_run_now(app, session, "ghost")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_not_found"
    assert env["data"]["job_name"] == "ghost"


async def test_cmd_schedule_run_now_scheduler_offline():
    session = _fake_session()
    app = {"scheduler": None}

    await cmd_mod.cmd_schedule_run_now(app, session, "job1")

    env = session.ws.sent[-1]
    assert env["data"]["action"] == "schedule_unavailable"


# ── engine interval parser extension ──────────────────────────────────────


def test_interval_parser_accepts_d_shorthand():
    from tesseract.scheduler.engine import _parse_interval
    assert _parse_interval("1d") == 86400
    assert _parse_interval("2d") == 2 * 86400
    # existing units still work
    assert _parse_interval("30s") == 30
    assert _parse_interval("15m") == 15 * 60
    assert _parse_interval("1h") == 3600
    # garbage still returns None
    assert _parse_interval("foo") is None
