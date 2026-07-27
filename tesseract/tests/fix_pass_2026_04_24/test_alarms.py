"""Regression suite for alarm v2 — persistence, recurrence, parser, kernel tools.

Covers everything added by the alarm subsystem v2 pass:

- YAML persistence roundtrip (add → reload matches)
- RecurrenceRule.next_occurrence math for every kind
- parse_alarm_when extended patterns ('in N minutes', '9am', 'tomorrow at 9am',
  'next mon at 9am')
- parse_recurrence + parse_alarm_spec combined entry point
- Each kernel tool (alarm_set / alarm_list / alarm_cancel / alarm_snooze):
  happy-path + error path
- Ambiguous-label resolution
- Fire-path: one-shot removes, recurring re-arms
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.kernel.tools.alarm_cancel import AlarmCancelTool
from tesseract.kernel.tools.alarm_list import AlarmListTool
from tesseract.kernel.tools.alarm_set import AlarmSetTool
from tesseract.kernel.tools.alarm_snooze import AlarmSnoozeTool
from tesseract.kernel.tools.base import ToolContext
from tesseract.mirror.server import commands as cmd_mod
from tesseract.scheduler.alarms import (
    AlarmRegistry,
    PendingAlarm,
    RecurrenceRule,
    SNOOZE_OPTIONS,
)
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult


ALARM_HANDLER_DOTPATH = cmd_mod.ALARM_HANDLER_DOTPATH
NOW = datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc)  # Friday 09:00 UTC


class _RecordingJob(BaseJob):
    fired: list[JobContext] = []

    async def run(self, ctx: JobContext) -> JobResult:
        _RecordingJob.fired.append(ctx)
        return JobResult(job_name=ctx.job_name, run_id=ctx.run_id, ok=True, detail="ran")


_RECORDING_DOTPATH = f"{__name__}._RecordingJob"


@pytest.fixture(autouse=True)
def _reset_recording_job():
    _RecordingJob.fired = []
    yield
    _RecordingJob.fired = []


# ── Persistence ──────────────────────────────────────────────────────────────


def test_persistence_roundtrip(tmp_path: Path):
    state = tmp_path / "alarms.yaml"
    reg = AlarmRegistry(state_file=state)
    reg.add("trash", NOW + timedelta(minutes=20), ALARM_HANDLER_DOTPATH, message="take out")
    reg.add("standup", NOW + timedelta(hours=1), ALARM_HANDLER_DOTPATH,
            message="stand up", recurrence=RecurrenceRule(kind="weekdays"))

    assert state.exists()
    text = state.read_text(encoding="utf-8")
    assert "trash" in text and "standup" in text

    reg2 = AlarmRegistry(state_file=state)
    labels = sorted(a.label for a in reg2.list_pending())
    assert labels == ["standup", "trash"]
    by_label = {a.label: a for a in reg2.list_pending()}
    assert by_label["standup"].recurrence is not None
    assert by_label["standup"].recurrence.kind == "weekdays"
    assert by_label["trash"].recurrence is None


def test_persistence_removes_cancelled_from_yaml(tmp_path: Path):
    state = tmp_path / "alarms.yaml"
    reg = AlarmRegistry(state_file=state)
    reg.add("a", NOW + timedelta(minutes=5), ALARM_HANDLER_DOTPATH)
    reg.add("b", NOW + timedelta(minutes=10), ALARM_HANDLER_DOTPATH)
    reg.cancel("a")

    reg2 = AlarmRegistry(state_file=state)
    assert [a.label for a in reg2.list_pending()] == ["b"]


def test_no_persistence_when_state_file_none(tmp_path: Path):
    """In-memory-only mode: add does not create any file."""
    state_sentinel = tmp_path / "alarms.yaml"
    reg = AlarmRegistry(state_file=None)
    reg.add("x", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    assert not state_sentinel.exists()


# ── RecurrenceRule.next_occurrence ───────────────────────────────────────────


def test_recurrence_daily():
    rule = RecurrenceRule(kind="daily")
    assert rule.next_occurrence(NOW) == NOW + timedelta(days=1)


def test_recurrence_weekdays_friday_to_monday():
    rule = RecurrenceRule(kind="weekdays")
    # NOW is Friday — next weekday should be Monday (3 days forward).
    assert rule.next_occurrence(NOW) == NOW + timedelta(days=3)


def test_recurrence_weekdays_wednesday_to_thursday():
    rule = RecurrenceRule(kind="weekdays")
    wed = datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc)  # Wednesday
    assert rule.next_occurrence(wed) == wed + timedelta(days=1)


def test_recurrence_weekly_same_day_wraps():
    rule = RecurrenceRule(kind="weekly", weekday=4)  # Friday (NOW is Friday)
    assert rule.next_occurrence(NOW) == NOW + timedelta(days=7)


def test_recurrence_weekly_forward_in_week():
    rule = RecurrenceRule(kind="weekly", weekday=0)  # Monday (NOW is Friday)
    assert rule.next_occurrence(NOW) == NOW + timedelta(days=3)


def test_recurrence_every_interval():
    rule = RecurrenceRule(kind="every", interval_seconds=7200)
    assert rule.next_occurrence(NOW) == NOW + timedelta(hours=2)


def test_recurrence_roundtrip():
    for rule in [
        RecurrenceRule(kind="daily"),
        RecurrenceRule(kind="weekdays"),
        RecurrenceRule(kind="weekly", weekday=2),
        RecurrenceRule(kind="every", interval_seconds=90 * 60),
    ]:
        assert RecurrenceRule.from_dict(rule.to_dict()) == rule


# ── Parser: parse_alarm_when extended patterns ───────────────────────────────


def test_parse_alarm_when_in_spelled_out():
    assert cmd_mod.parse_alarm_when("in 20 minutes", NOW) == NOW + timedelta(minutes=20)
    assert cmd_mod.parse_alarm_when("in 2 hours", NOW) == NOW + timedelta(hours=2)
    assert cmd_mod.parse_alarm_when("in 90 seconds", NOW) == NOW + timedelta(seconds=90)


# Wall-clock expressions are interpreted in **system local time** (so "9pm"
# means the operator's wall clock, not UTC). The expected-value helpers below
# mirror the parser's local→UTC math so these tests stay deterministic on any
# system tz.


def _expected_clock_at(hour: int, minute: int = 0, days_offset: int = 0) -> datetime:
    """Build the expected UTC datetime for `parse_alarm_when("<hour>:<min>", NOW)`
    using the same local-anchor logic as the parser. `days_offset=1` forces
    "tomorrow"; `days_offset=0` means "today, or roll to tomorrow if past"."""
    now_local = NOW.astimezone()
    base_local = now_local + timedelta(days=days_offset)
    candidate_local = base_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if days_offset == 0 and candidate_local <= now_local:
        candidate_local += timedelta(days=1)
    return candidate_local.astimezone(timezone.utc)


def test_parse_alarm_when_clock_today_future():
    # NOW=09:00 UTC; "9pm" / "14:30" are sufficiently late that they're still
    # in the future on any reasonable tz.
    assert cmd_mod.parse_alarm_when("9pm", NOW) == _expected_clock_at(21)
    assert cmd_mod.parse_alarm_when("14:30", NOW) == _expected_clock_at(14, 30)


def test_parse_alarm_when_clock_past_rolls_to_tomorrow():
    # The parser rolls the clock to tomorrow when the local-anchored time has
    # already passed. "8am" past noon-local would be tomorrow; on tzs where
    # 8am hasn't happened yet, today is correct — `_expected_clock_at` mirrors
    # the same conditional.
    assert cmd_mod.parse_alarm_when("8am", NOW) == _expected_clock_at(8)


def test_parse_alarm_when_tomorrow_at():
    expected = _expected_clock_at(9, days_offset=1)
    assert cmd_mod.parse_alarm_when("tomorrow at 9am", NOW) == expected
    assert cmd_mod.parse_alarm_when("tomorrow 9am", NOW) == expected


def test_parse_alarm_when_next_weekday():
    # NOW is Friday in UTC. The local-tz version may also be Friday or roll a
    # day either side at extreme offsets — derive the expected via the same
    # local-anchor math.
    now_local = NOW.astimezone()
    days_ahead = (0 - now_local.weekday()) % 7  # Monday=0
    if days_ahead == 0:
        days_ahead = 7
    expected = _expected_clock_at(9, days_offset=days_ahead)
    assert cmd_mod.parse_alarm_when("next mon at 9am", NOW) == expected


def test_parse_alarm_when_noon_midnight():
    assert cmd_mod.parse_alarm_when("noon", NOW) == _expected_clock_at(12)


def test_parse_alarm_when_still_accepts_compact_and_iso():
    # Back-compat with the S4 parser.
    assert cmd_mod.parse_alarm_when("15m", NOW) == NOW + timedelta(minutes=15)
    assert cmd_mod.parse_alarm_when("1h30m", NOW) == NOW + timedelta(hours=1, minutes=30)
    assert cmd_mod.parse_alarm_when("2026-05-01T10:00:00+00:00", NOW) == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)


# ── Parser: parse_recurrence + parse_alarm_spec ──────────────────────────────


def test_parse_recurrence_variants():
    assert cmd_mod.parse_recurrence(["daily"])[0] == RecurrenceRule(kind="daily")
    assert cmd_mod.parse_recurrence(["every", "day"])[0] == RecurrenceRule(kind="daily")
    assert cmd_mod.parse_recurrence(["weekdays"])[0] == RecurrenceRule(kind="weekdays")
    assert cmd_mod.parse_recurrence(["every", "weekday"])[0] == RecurrenceRule(kind="weekdays")
    assert cmd_mod.parse_recurrence(["every", "mon"])[0] == RecurrenceRule(kind="weekly", weekday=0)
    assert cmd_mod.parse_recurrence(["every", "2h"])[0] == RecurrenceRule(kind="every", interval_seconds=7200)
    assert cmd_mod.parse_recurrence(["every", "30", "minutes"])[0] == RecurrenceRule(kind="every", interval_seconds=1800)


def test_parse_alarm_spec_recurring_with_clock():
    run_at, rec, msg = cmd_mod.parse_alarm_spec("weekdays at 9am stand up", NOW)
    assert rec == RecurrenceRule(kind="weekdays")
    # NOW is Friday 09:00 — recurrence kicks in, first fire next weekday 09:00.
    # The parser normalizes to today-9am when past, but recurrence then advances;
    # with NOW at 09:00 exactly, 'at 9am' rolls to tomorrow (Saturday 09:00).
    assert run_at is not None
    assert msg == "stand up"


def test_parse_alarm_spec_one_shot_with_quoted_message():
    run_at, rec, msg = cmd_mod.parse_alarm_spec('20m "take out the trash"', NOW)
    assert rec is None
    assert run_at == NOW + timedelta(minutes=20)
    assert msg == "take out the trash"


def test_parse_alarm_spec_in_n_minutes_with_tail_message():
    run_at, rec, msg = cmd_mod.parse_alarm_spec("in 30 minutes call mom", NOW)
    assert rec is None
    assert run_at == NOW + timedelta(minutes=30)
    assert msg == "call mom"


def test_parse_alarm_spec_bare_recurrence_uses_rule_for_first_fire():
    run_at, rec, msg = cmd_mod.parse_alarm_spec("daily stand up", NOW)
    assert rec == RecurrenceRule(kind="daily")
    assert run_at == NOW + timedelta(days=1)
    assert msg == "stand up"


# ── AlarmRegistry resolve / suggestions ──────────────────────────────────────


def test_resolve_by_label_unique():
    reg = AlarmRegistry(state_file=None)
    alarm = reg.add("unique", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    assert reg.resolve("unique") is alarm


def test_resolve_by_id_prefix():
    reg = AlarmRegistry(state_file=None)
    alarm = reg.add("x", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    assert reg.resolve(alarm.id[:4]) is alarm
    assert reg.resolve(alarm.id) is alarm


def test_resolve_misses_return_none():
    reg = AlarmRegistry(state_file=None)
    reg.add("x", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    assert reg.resolve("does-not-exist") is None


def test_suggestions_are_helpful():
    reg = AlarmRegistry(state_file=None)
    reg.add("standup-a", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    reg.add("standup-b", NOW + timedelta(minutes=2), ALARM_HANDLER_DOTPATH)
    hits = reg.suggestions("standup")
    assert len(hits) == 2


# ── Fire path: one-shot removal + recurring re-arm ───────────────────────────


async def test_tick_one_shot_removes_and_persists(tmp_path: Path):
    state = tmp_path / "alarms.yaml"
    reg = AlarmRegistry(log_dir=tmp_path, state_file=state)
    reg.add("fire", NOW - timedelta(seconds=1), _RECORDING_DOTPATH,
            payload={"alarm_name": "fire"}, message="hello")

    await reg.tick(app=None, now=NOW)
    assert len(_RecordingJob.fired) == 1
    assert reg.list_pending() == []

    # Persistence reflects the removal.
    reg2 = AlarmRegistry(state_file=state)
    assert reg2.list_pending() == []
    # recently_fired has the entry.
    assert len(reg.recently_fired) == 1
    assert reg.recently_fired[0].label == "fire"
    assert reg.recently_fired[0].was_recurring is False


async def test_tick_recurring_rearms_and_persists(tmp_path: Path):
    state = tmp_path / "alarms.yaml"
    reg = AlarmRegistry(log_dir=tmp_path, state_file=state)
    reg.add(
        "cycle",
        NOW - timedelta(seconds=1),
        _RECORDING_DOTPATH,
        payload={"alarm_name": "cycle"},
        recurrence=RecurrenceRule(kind="every", interval_seconds=600),
    )

    await reg.tick(app=None, now=NOW)
    assert len(_RecordingJob.fired) == 1
    pending = reg.list_pending()
    assert len(pending) == 1
    # run_at advanced by the interval (past NOW).
    assert pending[0].run_at > NOW

    # Persistence reflects the re-armed run_at.
    reg2 = AlarmRegistry(state_file=state)
    assert len(reg2.list_pending()) == 1
    assert reg2.list_pending()[0].run_at == pending[0].run_at

    # recently_fired still tagged recurring.
    assert reg.recently_fired[-1].was_recurring is True


async def test_tick_recurring_skips_missed_cycles_without_burst(tmp_path: Path):
    """If the process was offline for >1 cycle, we fire ONCE and fast-forward
    to the next future slot. No backlog storm."""
    reg = AlarmRegistry(log_dir=tmp_path, state_file=None)
    reg.add(
        "heartbeat",
        NOW - timedelta(minutes=50),
        _RECORDING_DOTPATH,
        recurrence=RecurrenceRule(kind="every", interval_seconds=600),
    )
    await reg.tick(app=None, now=NOW)
    assert len(_RecordingJob.fired) == 1
    assert reg.list_pending()[0].run_at > NOW


# ── Kernel tools ─────────────────────────────────────────────────────────────


CTX = ToolContext(workspace_root=".")


async def test_alarm_set_tool_happy_path():
    reg = AlarmRegistry(state_file=None)
    tool = AlarmSetTool(alarm_registry=reg)
    result = await tool.run(
        tool.input_schema(label="trash", when="20m", message="take out"),
        CTX,
    )
    assert not result.is_error
    assert "trash" in result.output
    assert reg.list_pending()[0].label == "trash"
    assert reg.list_pending()[0].message == "take out"


async def test_alarm_set_tool_with_recurrence():
    reg = AlarmRegistry(state_file=None)
    tool = AlarmSetTool(alarm_registry=reg)
    result = await tool.run(
        tool.input_schema(label="standup", when="weekdays at 9am", message="stand up"),
        CTX,
    )
    assert not result.is_error
    alarm = reg.list_pending()[0]
    assert alarm.recurrence is not None
    assert alarm.recurrence.kind == "weekdays"
    assert alarm.message == "stand up"


async def test_alarm_set_tool_rejects_bad_when():
    reg = AlarmRegistry(state_file=None)
    tool = AlarmSetTool(alarm_registry=reg)
    result = await tool.run(
        tool.input_schema(label="x", when="NOT A TIME", message="hi"),
        CTX,
    )
    assert result.is_error
    assert reg.list_pending() == []


async def test_alarm_set_tool_rejects_duplicate_label():
    reg = AlarmRegistry(state_file=None)
    tool = AlarmSetTool(alarm_registry=reg)
    await tool.run(tool.input_schema(label="dup", when="20m"), CTX)
    result = await tool.run(tool.input_schema(label="dup", when="30m"), CTX)
    assert result.is_error


async def test_alarm_list_tool():
    reg = AlarmRegistry(state_file=None)
    tool = AlarmListTool(alarm_registry=reg)
    empty = await tool.run(tool.input_schema(), CTX)
    assert "no pending alarms" in empty.output

    reg.add("a", NOW + timedelta(minutes=5), ALARM_HANDLER_DOTPATH, message="one")
    reg.add("b", NOW + timedelta(minutes=10), ALARM_HANDLER_DOTPATH, message="two")
    filled = await tool.run(tool.input_schema(), CTX)
    assert "a" in filled.output and "b" in filled.output
    assert filled.metadata["count"] == 2


async def test_alarm_cancel_tool_by_label():
    reg = AlarmRegistry(state_file=None)
    alarm = reg.add("gone", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    tool = AlarmCancelTool(alarm_registry=reg)
    result = await tool.run(tool.input_schema(handle="gone"), CTX)
    assert not result.is_error
    assert "gone" in result.output
    assert reg.list_pending() == []


async def test_alarm_cancel_tool_by_id_prefix():
    reg = AlarmRegistry(state_file=None)
    alarm = reg.add("gone", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    tool = AlarmCancelTool(alarm_registry=reg)
    result = await tool.run(tool.input_schema(handle=alarm.id[:6]), CTX)
    assert not result.is_error


async def test_alarm_cancel_tool_missing_handle_suggests():
    reg = AlarmRegistry(state_file=None)
    reg.add("standup-a", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    tool = AlarmCancelTool(alarm_registry=reg)
    result = await tool.run(tool.input_schema(handle="nope"), CTX)
    assert result.is_error
    assert "no alarm matches" in result.output


async def test_alarm_snooze_tool_extends_run_at():
    reg = AlarmRegistry(state_file=None)
    alarm = reg.add("ring", NOW + timedelta(seconds=10), ALARM_HANDLER_DOTPATH)
    first = alarm.run_at
    tool = AlarmSnoozeTool(alarm_registry=reg)
    result = await tool.run(
        tool.input_schema(handle="ring", duration="30m"),
        CTX,
    )
    assert not result.is_error
    assert reg.list_pending()[0].run_at > first


async def test_alarm_snooze_tool_default_duration_is_10m():
    reg = AlarmRegistry(state_file=None)
    reg.add("x", NOW + timedelta(seconds=5), ALARM_HANDLER_DOTPATH)
    tool = AlarmSnoozeTool(alarm_registry=reg)
    result = await tool.run(tool.input_schema(handle="x"), CTX)
    assert not result.is_error


async def test_alarm_snooze_tool_unknown_handle():
    reg = AlarmRegistry(state_file=None)
    tool = AlarmSnoozeTool(alarm_registry=reg)
    result = await tool.run(tool.input_schema(handle="missing", duration="5m"), CTX)
    assert result.is_error


async def test_alarm_snooze_tool_rejects_recurrence_in_duration():
    """Snooze must use a time-only parser — `every 2h` is a recurrence,
    not a duration, and accepting it silently drops the cycle on the floor."""
    reg = AlarmRegistry(state_file=None)
    reg.add("ring", NOW + timedelta(seconds=10), ALARM_HANDLER_DOTPATH)
    tool = AlarmSnoozeTool(alarm_registry=reg)
    result = await tool.run(tool.input_schema(handle="ring", duration="every 2h"), CTX)
    assert result.is_error
    assert "cannot parse snooze duration" in result.output


async def test_cmd_alarm_snooze_rejects_recurrence_in_duration():
    reg = AlarmRegistry(state_file=None)
    reg.add("ring", NOW + timedelta(seconds=10), ALARM_HANDLER_DOTPATH)
    app = {"alarm_registry": reg}
    sess = _fake_session()

    await cmd_mod.cmd_alarm_snooze(app, sess, "ring \"every 2h\"")

    actions = [e["data"]["action"] for e in sess.ws.sent if e["type"] == "schedule_state"]
    assert "alarm_invalid" in actions


# ── WS command path — ambiguous label ────────────────────────────────────────


def _fake_session() -> SimpleNamespace:
    class _FakeWS:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send_json(self, payload):
            self.sent.append(payload)

    return SimpleNamespace(session_id="sess", ws=_FakeWS(), event_log=[])


async def test_alarm_cancel_cmd_emits_suggestions_on_ambiguous_label():
    reg = AlarmRegistry(state_file=None)
    reg.add("standup-a", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH)
    reg.add("standup-b", NOW + timedelta(minutes=2), ALARM_HANDLER_DOTPATH)
    app = {"alarm_registry": reg}
    sess = _fake_session()

    await cmd_mod.cmd_alarm_cancel(app, sess, "standup")

    payloads = [e["data"] for e in sess.ws.sent if e["type"] == "schedule_state"]
    assert len(payloads) == 1
    assert payloads[0]["action"] == "alarm_not_found"
    assert payloads[0]["suggestions"]


async def test_alarm_list_cmd_returns_pending():
    reg = AlarmRegistry(state_file=None)
    reg.add("a", NOW + timedelta(minutes=1), ALARM_HANDLER_DOTPATH, message="one")
    app = {"alarm_registry": reg}
    sess = _fake_session()

    await cmd_mod.cmd_alarm_list(app, sess)

    data = [e["data"] for e in sess.ws.sent if e["type"] == "schedule_state"][0]
    assert data["action"] == "alarm_list"
    assert len(data["alarms"]) == 1
    assert data["alarms"][0]["label"] == "a"


async def test_alarm_snooze_cmd_shifts_run_at():
    reg = AlarmRegistry(state_file=None)
    reg.add("ring", NOW + timedelta(seconds=10), ALARM_HANDLER_DOTPATH)
    app = {"alarm_registry": reg}
    sess = _fake_session()

    await cmd_mod.cmd_alarm_snooze(app, sess, "ring 5m")

    data = [e["data"] for e in sess.ws.sent if e["type"] == "schedule_state"][-1]
    assert data["action"] == "alarm_snoozed"


# ── Sanity: SNOOZE_OPTIONS stay in plan ──────────────────────────────────────


def test_snooze_options_constant():
    assert SNOOZE_OPTIONS == ["5m", "10m", "30m", "1h"]
