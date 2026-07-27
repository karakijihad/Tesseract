"""F3 regression tests — alive-feel observations.

Covers the backend deliverables enumerated in
`Docs/Plan/pre-phase-14-foundation/phase-f3-alive-feel-observations.md` §6:
  - D1: manifest alive-nudge present in assembled prompt
  - D2: stream_start envelope — behavioral check on the emitted payload shape
  - D3: /save with empty history emits a command_result warning (and no write)
  - D13: ollama_boot.ensure_ollama_ready — probe success, probe fail, model missing
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tesseract.brain.prompt import _ALIVE_NUDGE_TEXT, assemble_system_prompt
from tesseract.memory import ollama_boot


# ── D1: manifest alive-nudge ─────────────────────────────────────────


def test_manifest_nudge_present_in_assembled_prompt() -> None:
    prompt = assemble_system_prompt(mode="manifest")
    assert _ALIVE_NUDGE_TEXT in prompt, "F3/D1 — nudge must appear in manifest prompt"


def test_manifest_nudge_mentions_acknowledgment() -> None:
    assert "confirm receipt" in _ALIVE_NUDGE_TEXT.lower()
    assert "acknowledge" in _ALIVE_NUDGE_TEXT.lower()
    assert "vary" in _ALIVE_NUDGE_TEXT.lower()


# ── D2: stream_start envelope — behavioral ──────────────────────────

_UUID4_HEX = re.compile(r"^[0-9a-f]{32}$")


@pytest.mark.asyncio
async def test_run_turn_emits_stream_start_with_uuid4_turn_id() -> None:
    """Runs a stub turn end-to-end and captures the actual envelope payload.

    Replaces the 2026-04-20 source-inspection check (which missed the
    `str(turn_count)` regression). The envelope's turn_id must be a UUID4
    hex per _shared/mirror-envelopes.md.
    """
    from tesseract.mirror.server import ws as ws_module
    from tesseract.mirror.server import turn_runner as turn_runner_module

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    class _FakeChatSession:
        history: list = []
        pending_injected_messages: list = []

        async def send(self, _text: str):
            # Empty async generator — simulates a turn that completes with zero chunks.
            if False:
                yield

    fake_session = SimpleNamespace(
        session_id="test-session",
        event_log=[],
        ws=_FakeWS(),
        chat_session=_FakeChatSession(),
        turn_count=0,
        active_chat_id="test-chat",
        chats={},
        current_turn_tasks={},
        chat_queues={},
        tool_names_by_call={},
        save_name=None,
        started_at="",
        pending_view_snapshot=None,
    )
    fake_app: dict = {"adapter_options": None, "mood": None, "memory_bundle": None}

    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=AsyncMock(return_value=None)),
        patch.object(turn_runner_module, "emit_stats", new=AsyncMock(return_value=None)),
    ):
        await ws_module._run_turn(fake_app, fake_session, "hi")

    stream_starts = [env for env in sent if env.get("type") == "stream_start"]
    assert len(stream_starts) == 1, f"expected exactly one stream_start, got {stream_starts}"
    env = stream_starts[0]
    assert env["category"] == "loop"
    assert env["session_id"] == "test-session"
    turn_id = env["data"]["turn_id"]
    assert isinstance(turn_id, str), f"turn_id must be string, got {type(turn_id).__name__}"
    assert _UUID4_HEX.match(turn_id), f"turn_id not UUID4 hex: {turn_id!r}"


# ── D2b: turn_id uniqueness across consecutive turns ─────────────────


@pytest.mark.asyncio
async def test_consecutive_turns_get_distinct_turn_ids() -> None:
    from tesseract.mirror.server import ws as ws_module
    from tesseract.mirror.server import turn_runner as turn_runner_module

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    class _FakeChatSession:
        history: list = []
        pending_injected_messages: list = []

        async def send(self, _text: str):
            if False:
                yield

    fake_session = SimpleNamespace(
        session_id="s",
        event_log=[],
        ws=_FakeWS(),
        chat_session=_FakeChatSession(),
        turn_count=0,
        active_chat_id="test-chat",
        chats={},
        current_turn_tasks={},
        chat_queues={},
        tool_names_by_call={},
        save_name=None,
        started_at="",
        pending_view_snapshot=None,
    )
    fake_app: dict = {"adapter_options": None, "mood": None, "memory_bundle": None}

    with (
        patch.object(turn_runner_module, "_maybe_auto_compact", new=AsyncMock(return_value=None)),
        patch.object(turn_runner_module, "emit_stats", new=AsyncMock(return_value=None)),
    ):
        await ws_module._run_turn(fake_app, fake_session, "hi 1")
        await ws_module._run_turn(fake_app, fake_session, "hi 2")

    turn_ids = [env["data"]["turn_id"] for env in sent if env.get("type") == "stream_start"]
    assert len(turn_ids) == 2
    assert turn_ids[0] != turn_ids[1], "each turn must emit a distinct turn_id"


# ── D3: /save with empty history ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cmd_save_empty_history_emits_warning(tmp_path: Path) -> None:
    captured = await _run_cmd_save(tmp_path, arg="Testing", history_empty=True)
    warnings = [env for env in captured if env["type"] == "command_result"]
    assert warnings, f"expected command_result, got {[e['type'] for e in captured]}"
    w = warnings[0]
    assert w["data"]["command"] == "save"
    assert w["data"]["ok"] is False
    assert w["data"]["severity"] == "warning"
    assert w["data"]["reason_code"] == "empty_history"
    assert not list(tmp_path.glob("*.json")), "F3/D3 — empty /save must not write"


@pytest.mark.asyncio
async def test_cmd_save_with_history_writes(tmp_path: Path) -> None:
    captured = await _run_cmd_save(tmp_path, arg="Testing", history_empty=False)
    types = [env["type"] for env in captured]
    assert "session_saved" in types
    assert "command_result" not in types
    assert (tmp_path / "Testing.json").exists()


# ── D13: ensure_ollama_ready ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ollama_boot_probe_success_model_present() -> None:
    with (
        patch.object(ollama_boot, "_probe", new=AsyncMock(return_value=True)),
        patch.object(
            ollama_boot, "_fetch_tags",
            new=AsyncMock(return_value=["nomic-embed-text:latest"]),
        ),
    ):
        ready = await ollama_boot.ensure_ollama_ready(
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            auto_start=False,
        )
    assert ready is True


@pytest.mark.asyncio
async def test_ollama_boot_probe_fail_fails_open(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(ollama_boot, "_probe", new=AsyncMock(return_value=False)),
        patch.object(ollama_boot, "_spawn_ollama_serve", return_value=False),
    ):
        ready = await ollama_boot.ensure_ollama_ready(
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            auto_start=False,
        )
    assert ready is False
    assert any("not reachable" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_ollama_boot_model_missing_warns_no_pull(caplog: pytest.LogCaptureFixture) -> None:
    with (
        patch.object(ollama_boot, "_probe", new=AsyncMock(return_value=True)),
        patch.object(ollama_boot, "_fetch_tags", new=AsyncMock(return_value=["other-model:latest"])),
    ):
        ready = await ollama_boot.ensure_ollama_ready(
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            auto_start=False,
        )
    assert ready is False
    assert any("ollama pull" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_ollama_boot_auto_start_skipped_on_remote_host() -> None:
    # auto_start True + remote base_url — spawn must not fire
    with (
        patch.object(ollama_boot, "_probe", new=AsyncMock(return_value=False)),
        patch.object(ollama_boot, "_spawn_ollama_serve", return_value=False) as spawn,
    ):
        ready = await ollama_boot.ensure_ollama_ready(
            base_url="http://remote.example:11434",
            model="nomic-embed-text",
            auto_start=True,
        )
    assert ready is False
    spawn.assert_not_called()


# ── helpers ──────────────────────────────────────────────────────────


async def _run_cmd_save(sessions_dir: Path, arg: str, history_empty: bool) -> list[dict]:
    from tesseract.mirror.server import commands as commands_module

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    class _FakeChatSession:
        def __init__(self, empty: bool) -> None:
            self.history: list = [] if empty else [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey"},
            ]

    class _FakeSession:
        def __init__(self) -> None:
            self.session_id = "test-session"
            self.event_log: list[dict] = []
            self.ws = _FakeWS()
            self.save_name: str | None = None
            self.started_at = "2026-04-20T00:00:00Z"
            self.chat_session = _FakeChatSession(history_empty)

    class _FakeOpts:
        model = "test-model"

    fake_app: dict = {"adapter_options": _FakeOpts()}
    fake_session = _FakeSession()
    with patch.object(commands_module, "SESSIONS_DIR", sessions_dir):
        await commands_module.cmd_save(fake_app, fake_session, arg)
    return sent
