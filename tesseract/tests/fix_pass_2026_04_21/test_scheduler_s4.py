"""Regression suite for scheduler S4 — in-memory alarm registry + WS commands.

Covers: AlarmRegistry add/cancel/list_pending/tick; tick idempotency after
fire; tick skips future alarms; handler-exception isolation; AlarmHandlerJob
broadcast to live WS sessions; no-WS no-op path; relative-time parser;
/alarm-set validation; /alarm-cancel not-found path.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.mirror.server import commands as cmd_mod
from tesseract.scheduler.alarms import AlarmRegistry, PendingAlarm
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks.alarm_handler import AlarmHandlerJob
from tesseract.scheduler.types import JobContext, JobResult


NOW = datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc)


# ── probes ────────────────────────────────────────────────────────────────


class _RecordingJob(BaseJob):
    fired: list[JobContext] = []

    async def run(self, ctx: JobContext) -> JobResult:
        _RecordingJob.fired.append(ctx)
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True, detail="ran")


class _RaisingJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        raise RuntimeError("boom-from-handler")


_RECORDING_DOTPATH = f"{__name__}._RecordingJob"
_RAISING_DOTPATH = f"{__name__}._RaisingJob"


@pytest.fixture(autouse=True)
def _reset_recording_job():
    _RecordingJob.fired = []
    yield
    _RecordingJob.fired = []


class _FakeWS:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _fake_session(session_id: str = "sess-s4") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=session_id,
        ws=_FakeWS(),
        event_log=[],
    )


# ── AlarmRegistry core ────────────────────────────────────────────────────


def test_add_and_list_pending():
    reg = AlarmRegistry()
    alarm = reg.add("a1", NOW + timedelta(seconds=30), _RECORDING_DOTPATH, {"k": 1})
    assert isinstance(alarm, PendingAlarm)
    pending = reg.list_pending()
    assert [a.name for a in pending] == ["a1"]
    assert pending[0].payload == {"k": 1}


def test_add_rejects_duplicate_pending_name():
    reg = AlarmRegistry()
    reg.add("dup", NOW + timedelta(minutes=1), _RECORDING_DOTPATH)
    with pytest.raises(ValueError):
        reg.add("dup", NOW + timedelta(minutes=5), _RECORDING_DOTPATH)


def test_cancel_removes_pending():
    reg = AlarmRegistry()
    reg.add("gone", NOW + timedelta(minutes=1), _RECORDING_DOTPATH)
    assert reg.cancel("gone") is not None
    assert reg.list_pending() == []
    assert reg.cancel("gone") is None  # second cancel a no-op


async def test_tick_fires_due_alarm_and_is_idempotent(tmp_path: Path):
    reg = AlarmRegistry(log_dir=tmp_path)
    reg.add("fire", NOW - timedelta(seconds=1), _RECORDING_DOTPATH, {"alarm_name": "fire"})
    await reg.tick(app=None, now=NOW)
    assert len(_RecordingJob.fired) == 1
    assert _RecordingJob.fired[0].job_name == "alarm:fire"
    # runs.jsonl should contain exactly one entry
    runs_file = tmp_path / "runs.jsonl"
    assert runs_file.exists()
    entries = [json.loads(line) for line in runs_file.read_text(encoding="utf-8").splitlines()]
    assert [e["job_name"] for e in entries] == ["alarm:fire"]
    assert entries[0]["ok"] is True

    # second tick must not re-fire
    await reg.tick(app=None, now=NOW + timedelta(seconds=5))
    assert len(_RecordingJob.fired) == 1
    assert reg.list_pending() == []


async def test_tick_skips_future_alarm():
    reg = AlarmRegistry()
    reg.add("later", NOW + timedelta(hours=1), _RECORDING_DOTPATH)
    await reg.tick(app=None, now=NOW)
    assert _RecordingJob.fired == []
    assert [a.name for a in reg.list_pending()] == ["later"]


async def test_tick_isolates_handler_exception(tmp_path: Path):
    reg = AlarmRegistry(log_dir=tmp_path)
    reg.add("ok", NOW - timedelta(seconds=1), _RECORDING_DOTPATH, {"alarm_name": "ok"})
    reg.add("boom", NOW - timedelta(seconds=1), _RAISING_DOTPATH, {"alarm_name": "boom"})

    await reg.tick(app=None, now=NOW)

    # Recording job fired; boom wrapped + logged.
    assert [c.job_name for c in _RecordingJob.fired] == ["alarm:ok"]
    entries = [
        json.loads(line)
        for line in (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    by_name = {e["job_name"]: e for e in entries}
    assert by_name["alarm:ok"]["ok"] is True
    assert by_name["alarm:boom"]["ok"] is False
    assert "boom-from-handler" in by_name["alarm:boom"]["detail"]
    # Second tick does not retry either.
    await reg.tick(app=None, now=NOW + timedelta(seconds=10))
    assert len(_RecordingJob.fired) == 1


# ── AlarmHandlerJob ───────────────────────────────────────────────────────


async def test_alarm_handler_broadcasts_to_live_sessions():
    sess_a = _fake_session("a")
    sess_b = _fake_session("b")
    app = {"server_sessions": {"a": sess_a, "b": sess_b}}
    ctx = JobContext(
        job_name="alarm:ring",
        fired_at=NOW,
        app=app,
        config={"alarm_name": "ring", "message": "hello"},
    )

    result = await AlarmHandlerJob().run(ctx)

    assert result.ok is True
    assert result.payload["delivered_ws_count"] == 2
    assert result.payload["alarm_name"] == "ring"
    for sess in (sess_a, sess_b):
        # Post audit-1 (2026-04-24) M6: dedicated `schedule_alarm_fired`
        # envelope so the UI toast dispatcher can render it. The prior
        # `schedule_job_done` shape silently vanished because the jobs-panel
        # handler required a `job_name` that alarms don't carry.
        # v2: envelope gains alarm_id / alarm_label / recurring / snooze_options.
        matches = [env for env in sess.ws.sent if env["type"] == "schedule_alarm_fired"]
        assert len(matches) == 1
        assert matches[0]["category"] == "schedule"
        data = matches[0]["data"]
        assert data["alarm_name"] == "ring"
        assert data["message"] == "hello"


async def test_alarm_handler_no_active_ws_is_ok_noop():
    ctx = JobContext(
        job_name="alarm:silent",
        fired_at=NOW,
        app={"server_sessions": {}},
        config={"alarm_name": "silent", "message": ""},
    )

    result = await AlarmHandlerJob().run(ctx)

    assert result.ok is True
    assert result.detail == "no_active_ws"


# ── relative-time parser ──────────────────────────────────────────────────


def test_parse_alarm_when_relative_variants():
    now = NOW
    cases = {
        "30s": now + timedelta(seconds=30),
        "15m": now + timedelta(minutes=15),
        "1h": now + timedelta(hours=1),
        "1h30m": now + timedelta(hours=1, minutes=30),
        "2h15m30s": now + timedelta(hours=2, minutes=15, seconds=30),
    }
    for text, expected in cases.items():
        assert cmd_mod.parse_alarm_when(text, now) == expected


def test_parse_alarm_when_iso_and_invalid():
    now = NOW
    iso_aware = cmd_mod.parse_alarm_when("2026-05-01T10:00:00+00:00", now)
    assert iso_aware == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)

    iso_naive = cmd_mod.parse_alarm_when("2026-05-01T10:00:00", now)
    assert iso_naive == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)

    assert cmd_mod.parse_alarm_when("", now) is None
    assert cmd_mod.parse_alarm_when("later", now) is None
    assert cmd_mod.parse_alarm_when("0s", now) is None  # must be strictly positive


# ── WS command handlers ──────────────────────────────────────────────────


async def test_cmd_alarm_set_happy_path():
    registry = AlarmRegistry()
    app = {"alarm_registry": registry}
    session = _fake_session()

    await cmd_mod.cmd_alarm_set(app, session, 'smoke 30s "ring ring"')

    pending = registry.list_pending()
    assert [a.label for a in pending] == ["smoke"]
    # v2: message is a first-class field on PendingAlarm, not stuffed into payload.
    assert pending[0].message == "ring ring"

    envs = [e for e in session.ws.sent if e["type"] == "schedule_state"]
    assert len(envs) == 1
    assert envs[0]["data"]["action"] == "alarm_queued"
    # `name` key kept in the envelope for back-compat with the S4 frontend.
    assert envs[0]["data"]["alarm"]["name"] == "smoke"
    assert envs[0]["data"]["alarm"]["message"] == "ring ring"


async def test_cmd_alarm_set_rejects_past_time():
    registry = AlarmRegistry()
    app = {"alarm_registry": registry}
    session = _fake_session()

    await cmd_mod.cmd_alarm_set(app, session, "past 2020-01-01T00:00:00+00:00 hi")

    assert registry.list_pending() == []
    data = session.ws.sent[-1]["data"]
    assert data["action"] == "alarm_invalid"


async def test_cmd_alarm_set_rejects_duplicate():
    registry = AlarmRegistry()
    app = {"alarm_registry": registry}
    session = _fake_session()
    registry.add("dup", NOW + timedelta(hours=1), cmd_mod.ALARM_HANDLER_DOTPATH)

    await cmd_mod.cmd_alarm_set(app, session, "dup 30s hi")

    actions = [e["data"]["action"] for e in session.ws.sent if e["type"] == "schedule_state"]
    assert "alarm_duplicate" in actions


async def test_cmd_alarm_cancel_paths():
    registry = AlarmRegistry()
    app = {"alarm_registry": registry}
    session = _fake_session()
    registry.add("here", NOW + timedelta(minutes=5), cmd_mod.ALARM_HANDLER_DOTPATH)

    await cmd_mod.cmd_alarm_cancel(app, session, "here")
    await cmd_mod.cmd_alarm_cancel(app, session, "ghost")

    actions = [e["data"]["action"] for e in session.ws.sent if e["type"] == "schedule_state"]
    assert actions == ["alarm_cancelled", "alarm_not_found"]
    assert registry.list_pending() == []
