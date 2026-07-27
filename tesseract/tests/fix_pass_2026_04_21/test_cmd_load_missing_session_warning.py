"""`cmd_load` of a missing session emits `stream_error` with severity=warning.

Auto-resume on reload fires `/resume <saveName>` unconditionally. When the
operator cleared+deleted the session pre-reload, the backend must emit an
envelope that keeps the orb calm and does not add a chat error bubble —
mirroring the `command_result.severity='warning'` pattern already used by
`cmd_delete` for not-found.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tesseract.mirror.server import commands as commands_mod


@dataclass
class _FakeServerSession:
    session_id: str = "test-sess"


async def test_cmd_load_missing_session_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def _capture(_session, envelope):
        if envelope is not None:
            captured.append(envelope)

    monkeypatch.setattr(commands_mod, "send_envelope", _capture)

    session = _FakeServerSession()
    # Name guaranteed not to exist on disk.
    await commands_mod.cmd_load(None, session, "definitely-not-a-real-session-xyz-2026-04-21")

    assert len(captured) == 1, f"expected 1 envelope, got {len(captured)}"
    env = captured[0]
    assert env["type"] == "stream_error"
    assert env["data"]["message"].startswith("session not found:")
    assert env["data"].get("severity") == "warning", (
        f"BUG: cmd_load missing-session emission lost the warning classification "
        f"— orb will fire red for a benign operator-input miss. data={env['data']!r}"
    )


async def test_cmd_load_usage_emits_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def _capture(_session, envelope):
        if envelope is not None:
            captured.append(envelope)

    monkeypatch.setattr(commands_mod, "send_envelope", _capture)

    session = _FakeServerSession()
    await commands_mod.cmd_load(None, session, None)

    assert len(captured) == 1
    env = captured[0]
    assert env["data"]["message"] == "usage: /load <name>"
    assert env["data"].get("severity") == "warning"
