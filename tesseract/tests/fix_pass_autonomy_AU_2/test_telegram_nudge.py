"""AU-2 S2 — Telegram outbound nudge after recovery.

The nudge path lives in ``mirror.server.app::_send_recovery_nudge`` so
it can reuse ``send_to_operators`` (the rate-cap-exempt fan path used
by the daily-brief push). These tests mock the bridge directly to keep
the suite hermetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tesseract.orchestrator.recovery.summary import (
    RecoverySummary,
    empty_scan_counts,
)


class _RecordingBridge:
    """Stub bridge that captures every send_text call so the test can
    assert on body + recipient count."""

    name = "telegram"

    def __init__(self, *, chat_ids: set[str], tier_map: dict[str, str]) -> None:
        self.calls: list[dict] = []
        self._state = SimpleNamespace(
            allowlist=SimpleNamespace(chat_ids=chat_ids, blocked=set(), pending={}),
            poll_state=SimpleNamespace(user_tier=tier_map),
        )

    async def send_text(self, *, chat_ref: str, text: str, reply_to_message_id=None) -> None:
        self.calls.append({"chat_ref": chat_ref, "text": text})


def _summary_with(*, attn: int = 0, failed_runs: int = 0) -> RecoverySummary:
    s = RecoverySummary(
        boot_id="boot-20260517T220000-deadbeef",
        started_at=datetime(2026, 5, 17, 22, 0, 0, tzinfo=timezone.utc),
        scans=empty_scan_counts(),
    )
    if failed_runs:
        s.scans["schedule"]["failed"] = failed_runs
    for i in range(attn):
        s.flag(kind="worker", id=f"wk-{i}", reason="worker_lost_at_restart")
    return s


@pytest.mark.asyncio
async def test_clean_boot_does_not_send() -> None:
    """No operator_attention → silent boot."""
    from tesseract.mirror.server.app import _send_recovery_nudge

    bridge = _RecordingBridge(chat_ids={"111"}, tier_map={"111": "operator"})
    app = {"telegram_bridge": bridge}
    summary = _summary_with()
    await _send_recovery_nudge(app, summary)
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_attention_items_trigger_send() -> None:
    from tesseract.mirror.server.app import _send_recovery_nudge

    bridge = _RecordingBridge(chat_ids={"111"}, tier_map={"111": "operator"})
    app = {"telegram_bridge": bridge}
    summary = _summary_with(attn=2)
    await _send_recovery_nudge(app, summary)
    assert len(bridge.calls) == 1
    body = bridge.calls[0]["text"]
    assert "Recovery" in body
    assert "2 need" in body


@pytest.mark.asyncio
async def test_no_bridge_no_error() -> None:
    """Missing TELEGRAM_BOT_TOKEN → bridge=None → silent no-op
    (operator may not have wired Telegram at all)."""
    from tesseract.mirror.server.app import _send_recovery_nudge

    app = {"telegram_bridge": None}
    summary = _summary_with(attn=1)
    # Must not raise.
    await _send_recovery_nudge(app, summary)
