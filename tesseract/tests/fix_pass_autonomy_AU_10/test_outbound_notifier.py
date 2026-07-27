"""AU-10 OutboundNotifier — substrate tests.

Covers:

* sliding-window rate cap correctness (cap, prune, persistence),
* exempt categories bypass the cap regardless of recent history,
* YAML + runtime mute paths each independently silence,
* no-bridge / muted / cap-zero paths return distinct ``reason`` values,
* template formatter renders every category under 512 chars,
* the ledger file lands under the isolated TESSERACT_HOME (no leak).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.autonomy.outbound import (
    CATEGORIES,
    DEFAULT_RATE_PER_HOUR,
    EXEMPT_CATEGORIES,
    MAX_MESSAGE_CHARS,
    OutboundNotifier,
    RateLedger,
    format_message,
    outbound_mutes_path,
    outbound_rates_path,
    read_runtime_mutes,
    write_runtime_mutes,
)
from tesseract.tests.fix_pass_autonomy_AU_10.conftest import FakeChannelsConfig


@pytest.mark.asyncio
async def test_notify_sends_when_under_cap(
    isolated_home, bridge, channels_config, fixed_clock,
):
    clock, _advance = fixed_clock
    captured: list[dict] = []

    async def sender(text, *, bridge, allowlist, user_tier):
        captured.append({"text": text})
        return {"sent": 2, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: channels_config,
        sender=sender,
        clock=clock,
    )
    result = await notifier.notify(
        "agenda_started",
        {"item_id": "ag-2026-05-18-1200-foo", "goal": "do the thing"},
    )
    assert result.sent == 2
    assert result.skipped is False
    assert captured and "do the thing" in captured[0]["text"]


@pytest.mark.asyncio
async def test_rate_cap_blocks_after_n_sends(
    isolated_home, bridge, channels_config, fixed_clock,
):
    clock, _advance = fixed_clock
    cfg = type(channels_config)(default_per_hour=3)

    async def sender(*a, **kw):
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: cfg,
        sender=sender,
        clock=clock,
    )
    for _ in range(3):
        result = await notifier.notify("agenda_started", {"item_id": "ag-x"})
        assert result.sent == 1
    blocked = await notifier.notify("agenda_started", {"item_id": "ag-x"})
    assert blocked.skipped is True
    assert blocked.reason == "rate_capped"
    assert blocked.sent == 0


@pytest.mark.asyncio
async def test_sliding_window_releases_after_seconds(
    isolated_home, bridge, channels_config, fixed_clock,
):
    clock, advance = fixed_clock
    cfg = type(channels_config)(default_per_hour=1)

    async def sender(*a, **kw):
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: cfg,
        sender=sender,
        clock=clock,
    )
    first = await notifier.notify("agenda_started", {"item_id": "ag-a"})
    assert first.sent == 1
    second = await notifier.notify("agenda_started", {"item_id": "ag-a"})
    assert second.reason == "rate_capped"
    # Advance just past the 1h window.
    advance(3601)
    third = await notifier.notify("agenda_started", {"item_id": "ag-a"})
    assert third.sent == 1


@pytest.mark.asyncio
async def test_exempt_category_bypasses_cap(
    isolated_home, bridge, channels_config, fixed_clock,
):
    clock, _advance = fixed_clock
    cfg = type(channels_config)(default_per_hour=0)

    async def sender(*a, **kw):
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: cfg,
        sender=sender,
        clock=clock,
    )
    # cap=0 would block every non-exempt category, but recovery_summary
    # is in EXEMPT_CATEGORIES so it MUST go through.
    result = await notifier.notify("recovery_summary", {"text": "boot ok"})
    assert result.sent == 1
    # Send 20 in a row — exempt path never throttles.
    for _ in range(20):
        r = await notifier.notify("recovery_summary", {"text": "boot ok"})
        assert r.sent == 1


@pytest.mark.asyncio
async def test_yaml_mute_silences_category(
    isolated_home, bridge, fixed_clock,
):
    clock, _advance = fixed_clock
    cfg_class = FakeChannelsConfig
    cfg = cfg_class(muted_categories=["agenda_blocked"])

    async def sender(*a, **kw):
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: cfg,
        sender=sender,
        clock=clock,
    )
    result = await notifier.notify("agenda_blocked", {"item_id": "ag-x"})
    assert result.skipped is True
    assert result.reason == "muted"


@pytest.mark.asyncio
async def test_runtime_mute_silences_category(
    isolated_home, bridge, channels_config, fixed_clock,
):
    clock, _advance = fixed_clock
    write_runtime_mutes({"telegram": ["upgrade_applied"]})

    async def sender(*a, **kw):
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: channels_config,
        sender=sender,
        clock=clock,
    )
    result = await notifier.notify(
        "upgrade_applied", {"upgrade_id": "up-1", "class": "hot_tool"},
    )
    assert result.skipped is True
    assert result.reason == "muted"
    # Non-muted category still flows.
    other = await notifier.notify("agenda_started", {"item_id": "ag-x"})
    assert other.sent == 1


@pytest.mark.asyncio
async def test_no_bridge_short_circuits(
    isolated_home, channels_config, fixed_clock,
):
    clock, _advance = fixed_clock
    notifier = OutboundNotifier(
        bridge_getter=lambda: None,
        channels_config_getter=lambda: channels_config,
        sender=None,
        clock=clock,
    )
    result = await notifier.notify("agenda_started", {"item_id": "ag-x"})
    assert result.skipped is True
    assert result.reason == "no_bridge"


@pytest.mark.asyncio
async def test_cap_zero_blocks_non_exempt(
    isolated_home, bridge, fixed_clock,
):
    clock, _advance = fixed_clock
    cfg_class = FakeChannelsConfig
    cfg = cfg_class(default_per_hour=0)

    async def sender(*a, **kw):
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: cfg,
        sender=sender,
        clock=clock,
    )
    r = await notifier.notify("agenda_started", {"item_id": "ag-x"})
    assert r.skipped is True
    assert r.reason == "cap_zero"


def test_format_message_under_limit_all_categories():
    """Every template renders something under MAX_MESSAGE_CHARS even
    with maximal context that exceeds the limit."""
    long_goal = "x" * 2000
    ctx = {
        "item_id": "ag-2026-05-18-1200-very-long-slug-here",
        "goal": long_goal,
        "rationale": long_goal,
        "reason": long_goal,
        "text": long_goal,
        "source": "operator",
        "detector": "loop",
        "upgrade_id": "up-1",
        "class": "hot_tool",
        "gates": ["dependency_install", "config_apply", "kernel_patch"],
    }
    for cat in CATEGORIES:
        body = format_message(cat, ctx)
        assert body, f"empty body for {cat}"
        assert len(body) <= MAX_MESSAGE_CHARS, f"{cat}: {len(body)} > {MAX_MESSAGE_CHARS}"


def test_ledger_persists_under_tesseract_home(isolated_home):
    ledger = RateLedger(window_seconds=3600)
    ledger.register("telegram", "agenda_started")
    ledger.register("telegram", "agenda_started")
    # Re-instantiate — should pick up the persisted timestamps.
    revived = RateLedger(window_seconds=3600)
    assert revived.count("telegram", "agenda_started") == 2
    # File landed under TESSERACT_HOME (not under the live tree).
    expected = Path(outbound_rates_path())
    assert expected.exists()
    raw = json.loads(expected.read_text(encoding="utf-8"))
    assert raw["schema"] == 1
    assert "telegram::agenda_started" in raw["windows"]


def test_runtime_mute_roundtrip(isolated_home):
    write_runtime_mutes({"telegram": ["agenda_started", "agenda_started"]})
    revived = read_runtime_mutes()
    # Dedupes via sorted-set in writer.
    assert revived == {"telegram": ["agenda_started"]}
    assert outbound_mutes_path().exists()


def test_exempt_categories_constant_matches_spec():
    assert EXEMPT_CATEGORIES == frozenset(
        {"recovery_summary", "crash_storm_latched", "awaiting_operator"},
    )


def test_default_rate_per_hour():
    assert DEFAULT_RATE_PER_HOUR == 6


@pytest.mark.asyncio
async def test_exempt_category_bypasses_mute(
    isolated_home, bridge, fixed_clock,
):
    """GOVERNANCE §9 — exempt categories MUST reach the operator even
    when a mute toggle was flipped (by accident or otherwise)."""
    clock, _advance = fixed_clock
    cfg_class = FakeChannelsConfig
    # Mute every exempt category via YAML AND via runtime overrides.
    cfg = cfg_class(muted_categories=list(EXEMPT_CATEGORIES))
    write_runtime_mutes({"telegram": list(EXEMPT_CATEGORIES)})

    sent_categories: list[str] = []

    async def sender(text, *, bridge, allowlist, user_tier):
        sent_categories.append(text[:64])
        return {"sent": 1, "skipped": 0, "errors": 0}

    notifier = OutboundNotifier(
        bridge_getter=lambda: bridge,
        channels_config_getter=lambda: cfg,
        sender=sender,
        clock=clock,
    )
    for cat in EXEMPT_CATEGORIES:
        result = await notifier.notify(cat, {"text": f"exempt: {cat}"})
        assert result.sent == 1, f"{cat} should bypass mute"
        assert result.skipped is False
    assert len(sent_categories) == len(EXEMPT_CATEGORIES)
