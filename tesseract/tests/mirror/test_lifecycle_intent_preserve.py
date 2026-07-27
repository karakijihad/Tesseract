"""Regression — operator-reported 2026-05-21.

Clicking the code-drift toast's Restart button killed the backend but
the supervisor never respawned it: aiohttp's ``on_shutdown`` hook fired
``write_shutdown_intent(intent='operator_quit')`` *after* the route had
already written ``{restart_upgrade}``, so the supervisor saw an
``operator_quit`` and refused to bring the backend back.

Fix: ``on_aiohttp_shutdown`` now preserves any intent already on disk
for the current backend PID — both runtime routes write BEFORE
triggering ``loop.stop``, so a same-PID intent is always the route's.
The signal path (SIGTERM/SIGINT) leaves no prior intent, so the default
``operator_quit + backend_signal`` write still fires there.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``lifecycle`` resolves TESSERACT_HOME at call time, so just setting
    the env var is enough — no module reloads required."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _intent_payload(_home: Path) -> dict:
    return json.loads((_home / "runtime" / "intent.json").read_text("utf-8"))


def test_preserves_restart_upgrade_intent_for_same_pid(_home: Path) -> None:
    """The restart-for-code-drift route writes ``{restart_upgrade}``
    with our PID, then triggers loop.stop. on_aiohttp_shutdown must
    leave the file alone — otherwise it degrades to operator_quit."""
    from tesseract.mirror.server.lifecycle import (
        on_aiohttp_shutdown,
        write_shutdown_intent,
    )

    write_shutdown_intent(
        intent="restart_upgrade",
        source="ui_button",
        continuation_id="code-drift-abc123",
        reason="operator clicked restart on code drift",
    )

    on_aiohttp_shutdown(app=object())

    payload = _intent_payload(_home)
    assert payload["intent"] == "restart_upgrade"
    assert payload["continuation_id"] == "code-drift-abc123"
    assert payload["source"] == "ui_button"


def test_preserves_operator_quit_intent_for_same_pid(_home: Path) -> None:
    """``POST /api/runtime/shutdown`` writes operator_quit + ui_button.
    The hook must not rewrite it as backend_signal — the route's source
    label is the operator-facing audit trail."""
    from tesseract.mirror.server.lifecycle import (
        on_aiohttp_shutdown,
        write_shutdown_intent,
    )

    write_shutdown_intent(
        intent="operator_quit",
        source="ui_button",
        reason="operator clicked shutdown",
    )

    on_aiohttp_shutdown(app=object())

    payload = _intent_payload(_home)
    assert payload["intent"] == "operator_quit"
    assert payload["source"] == "ui_button"


def test_writes_default_operator_quit_when_no_prior_intent(
    _home: Path,
) -> None:
    """Signal path — supervisor sends SIGTERM, no route ran first.
    intent.json doesn't exist; the hook writes operator_quit so the
    supervisor honors the orderly exit instead of treating it as a
    crash."""
    from tesseract.mirror.server.lifecycle import on_aiohttp_shutdown

    assert not (_home / "runtime" / "intent.json").exists()

    on_aiohttp_shutdown(app=object())

    payload = _intent_payload(_home)
    assert payload["intent"] == "operator_quit"
    assert payload["source"] == "backend_signal"


def test_overwrites_stale_intent_from_prior_backend_pid(
    _home: Path,
) -> None:
    """A stale intent from a previous backend run (different PID) must
    not protect itself from the hook — otherwise a crashed prior run's
    intent could route the next clean shutdown wrong."""
    from tesseract.mirror.server.lifecycle import on_aiohttp_shutdown
    from tesseract.supervisor.intent import (
        IntentFile,
        intent_path,
        write_atomic,
    )

    stale = IntentFile(
        intent="restart_upgrade",
        timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
        source="ui_button",
        continuation_id="code-drift-stale",
        backend_pid=os.getpid() + 12345,
    )
    write_atomic(intent_path(_home), stale)

    on_aiohttp_shutdown(app=object())

    payload = _intent_payload(_home)
    assert payload["intent"] == "operator_quit"
    assert payload["source"] == "backend_signal"
    assert payload["backend_pid"] == os.getpid()
