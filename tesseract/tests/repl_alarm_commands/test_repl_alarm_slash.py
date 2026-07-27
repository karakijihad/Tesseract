"""Coverage for the alarm slash commands now driven by ``run_slash``.

Phase-1 cleanup (2026-05-05): ``/alarm-set`` etc. were hand-coded branches in
``tars_repl.py``. They moved to the universal slash dispatcher; the same
:class:`AlarmRegistry` mutations are now reached through ``run_slash`` →
:class:`AlarmSetTool` (and friends). These tests preserve the original
coverage shape against the new surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.alarm_cancel import AlarmCancelTool
from tesseract.kernel.tools.alarm_list import AlarmListTool
from tesseract.kernel.tools.alarm_set import AlarmSetTool
from tesseract.kernel.tools.alarm_snooze import AlarmSnoozeTool
from tesseract.kernel.tools.base import ToolContext
from tesseract.scheduler.alarm_parser import ALARM_HANDLER_DOTPATH
from tesseract.scheduler.alarms import AlarmRegistry
from tesseract.scripts.slash_dispatch import _OPERATOR_TOKEN, run_slash


async def _slash(*args, **kwargs):
    """Test-side wrapper that injects the operator caller token."""
    return await run_slash(*args, caller_token=_OPERATOR_TOKEN, **kwargs)


@pytest.fixture
def alarm_registry() -> AlarmRegistry:
    return AlarmRegistry(state_file=None)


@pytest.fixture
def registry(alarm_registry: AlarmRegistry) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(AlarmSetTool(alarm_registry))
    reg.register(AlarmListTool(alarm_registry))
    reg.register(AlarmCancelTool(alarm_registry))
    reg.register(AlarmSnoozeTool(alarm_registry))
    return reg


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(workspace_root=".", session_id="test")


@pytest.mark.asyncio
async def test_alarm_set_queues_one_shot(registry, alarm_registry, ctx):
    out = await _slash(
        registry, "alarm_set",
        {"label": "standup", "when": "10m", "message": "daily standup"}, [],
        ctx,
    )
    assert "standup" in out
    pending = alarm_registry.list_pending()
    assert len(pending) == 1 and pending[0].label == "standup"
    assert pending[0].run_at > datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_alarm_set_with_recurrence(registry, alarm_registry, ctx):
    out = await _slash(
        registry, "alarm_set",
        {"label": "stretch", "when": "every 30m", "message": "get up"}, [],
        ctx,
    )
    assert "every" in out
    pending = alarm_registry.list_pending()
    assert pending[0].recurrence is not None
    assert pending[0].recurrence.kind == "every"


@pytest.mark.asyncio
async def test_alarm_set_missing_required(registry, ctx):
    out = await _slash(registry, "alarm_set", {}, [], ctx)
    assert "invalid args" in out


@pytest.mark.asyncio
async def test_alarm_set_unparseable_when(registry, alarm_registry, ctx):
    out = await _slash(
        registry, "alarm_set",
        {"label": "broken", "when": "nonsensetime"}, [],
        ctx,
    )
    assert "cannot parse" in out
    assert alarm_registry.list_pending() == []


@pytest.mark.asyncio
async def test_alarm_set_duplicate_label(registry, alarm_registry, ctx):
    await _slash(registry, "alarm_set", {"label": "dup", "when": "1h"}, [], ctx)
    out = await _slash(registry, "alarm_set", {"label": "dup", "when": "2h"}, [], ctx)
    assert "already pending" in out
    assert len(alarm_registry.list_pending()) == 1


@pytest.mark.asyncio
async def test_alarm_list_empty(registry, ctx):
    out = await _slash(registry, "alarm_list", {}, [], ctx)
    assert "no pending" in out.lower() or "0" in out


@pytest.mark.asyncio
async def test_alarm_list_populated(registry, alarm_registry, ctx):
    alarm_registry.add(
        label="lunch",
        run_at=datetime.now(timezone.utc) + timedelta(minutes=45),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
        message="eat",
    )
    alarm_registry.add(
        label="standup",
        run_at=datetime.now(timezone.utc) + timedelta(seconds=90),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    out = await _slash(registry, "alarm_list", {}, [], ctx)
    assert "lunch" in out
    assert "standup" in out


@pytest.mark.asyncio
async def test_alarm_cancel_by_label(registry, alarm_registry, ctx):
    alarm_registry.add(
        label="cancelme",
        run_at=datetime.now(timezone.utc) + timedelta(hours=1),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    out = await _slash(registry, "alarm_cancel", {"handle": "cancelme"}, [], ctx)
    assert "cancel" in out.lower()
    assert alarm_registry.list_pending() == []


@pytest.mark.asyncio
async def test_alarm_cancel_missing(registry, ctx):
    out = await _slash(registry, "alarm_cancel", {"handle": "ghost"}, [], ctx)
    assert "no alarm" in out.lower() or "not found" in out.lower()


@pytest.mark.asyncio
async def test_alarm_cancel_no_arg(registry, ctx):
    # handle is required; coerce_args raises before the tool runs.
    out = await _slash(registry, "alarm_cancel", {}, [], ctx)
    assert "invalid args" in out


@pytest.mark.asyncio
async def test_alarm_snooze_pushes_run_at(registry, alarm_registry, ctx):
    original = datetime.now(timezone.utc) + timedelta(minutes=2)
    alarm_registry.add(
        label="snz", run_at=original, handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    out = await _slash(
        registry, "alarm_snooze", {"handle": "snz", "duration": "15m"}, [], ctx,
    )
    assert "snooz" in out.lower()
    new_run_at = alarm_registry.list_pending()[0].run_at
    assert new_run_at > original


@pytest.mark.asyncio
async def test_alarm_snooze_default_duration(registry, alarm_registry, ctx):
    alarm_registry.add(
        label="snz",
        run_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    # Schema default is 10m — no `duration` arg → tool uses its default.
    out = await _slash(registry, "alarm_snooze", {"handle": "snz"}, [], ctx)
    assert "snooz" in out.lower()
    new_run_at = alarm_registry.list_pending()[0].run_at
    assert (new_run_at - datetime.now(timezone.utc)).total_seconds() > 9 * 60


@pytest.mark.asyncio
async def test_alarm_snooze_unknown_handle(registry, ctx):
    out = await _slash(
        registry, "alarm_snooze", {"handle": "ghost", "duration": "5m"}, [], ctx,
    )
    assert "no alarm" in out.lower() or "not found" in out.lower()


@pytest.mark.asyncio
async def test_alarm_snooze_bad_duration(registry, alarm_registry, ctx):
    alarm_registry.add(
        label="snz",
        run_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        handler_dotpath=ALARM_HANDLER_DOTPATH,
    )
    out = await _slash(
        registry, "alarm_snooze", {"handle": "snz", "duration": "nonsense"}, [], ctx,
    )
    assert "cannot parse" in out.lower() or "could not" in out.lower()
