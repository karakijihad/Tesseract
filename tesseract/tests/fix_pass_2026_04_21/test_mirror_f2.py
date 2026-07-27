"""F2 regression tests — mirror fixes.

Covers the unit + integration deliverables enumerated in
`Docs/Plan/pre-phase-14-foundation/phase-f2-mirror-fixes.md` §6:
  - delete_session() → tuple[bool, str] (3 cases)
  - tokens.css carries --hint-bg + --hint-border
  - command_result envelope shape on /delete failure paths
  - command_result NOT emitted on success
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tesseract.brain.session_store import delete_session, save_session


REPO_ROOT = Path(__file__).resolve().parents[3]
TOKENS_CSS = REPO_ROOT / "tesseract" / "mirror" / "src" / "styles" / "tokens.css"


# ── delete_session() tuple return type ───────────────────────────────


def test_delete_session_not_found(tmp_path: Path) -> None:
    ok, reason = delete_session(tmp_path, "ghost")
    assert ok is False
    assert reason == "not_found"


def test_delete_session_io_error(tmp_path: Path) -> None:
    save_session(tmp_path, "doomed", "test-model", "2026-04-20T00:00:00Z", [])
    with patch("tesseract.brain.session_store.Path.unlink", side_effect=OSError("permission denied")):
        ok, reason = delete_session(tmp_path, "doomed")
    assert ok is False
    assert reason == "io_error"


def test_delete_session_success(tmp_path: Path) -> None:
    save_session(tmp_path, "live", "test-model", "2026-04-20T00:00:00Z", [])
    ok, reason = delete_session(tmp_path, "live")
    assert ok is True
    assert reason == ""
    assert not (tmp_path / "live.json").exists()


# ── tokens.css hint tokens ───────────────────────────────────────────


def test_tokens_css_has_hint_bg() -> None:
    text = TOKENS_CSS.read_text(encoding="utf-8")
    assert "--hint-bg:" in text, "F2 §5d — --hint-bg must be declared in tokens.css"
    assert "rgba(28,28,42,0.72)" in text, "F2 §5d — --hint-bg value must match phase spec"


def test_tokens_css_has_hint_border() -> None:
    text = TOKENS_CSS.read_text(encoding="utf-8")
    assert "--hint-border:" in text, "F2 §5d — --hint-border must be declared in tokens.css"
    assert "rgba(255,255,255,0.10)" in text, "F2 §5d — --hint-border value must match phase spec"


# ── /delete command_result envelope (integration via _cmd_delete) ────


@pytest.mark.asyncio
async def test_delete_command_not_found_emits_warning_envelope(tmp_path: Path) -> None:
    captured = await _run_cmd_delete(tmp_path, "ghost")
    assert captured["type"] == "command_result"
    assert captured["category"] == "command_result"
    assert captured["data"]["command"] == "delete"
    assert captured["data"]["ok"] is False
    assert captured["data"]["severity"] == "warning"
    assert captured["data"]["reason_code"] == "not_found"


@pytest.mark.asyncio
async def test_delete_command_io_error_emits_error_envelope(tmp_path: Path) -> None:
    save_session(tmp_path, "doomed", "test-model", "2026-04-20T00:00:00Z", [])
    with patch("tesseract.brain.session_store.Path.unlink", side_effect=OSError("permission denied")):
        captured = await _run_cmd_delete(tmp_path, "doomed")
    assert captured["type"] == "command_result"
    assert captured["data"]["severity"] == "error"
    assert captured["data"]["reason_code"] == "io_error"


@pytest.mark.asyncio
async def test_delete_command_success_no_command_result(tmp_path: Path) -> None:
    save_session(tmp_path, "live", "test-model", "2026-04-20T00:00:00Z", [])
    sent = await _run_cmd_delete_capture_all(tmp_path, "live")
    types = [env["type"] for env in sent]
    # Success path emits session_deleted + the refreshed session_list, but
    # never the command_result envelope (only failure paths emit it).
    assert "command_result" not in types
    assert "session_deleted" in types


# ── helpers ──────────────────────────────────────────────────────────


async def _run_cmd_delete(sessions_dir: Path, name: str) -> dict:
    """Invoke `_cmd_delete` against a fake ServerSession; return the first
    `command_result` envelope sent. Patches SESSIONS_DIR so the test owns
    the directory."""
    sent = await _run_cmd_delete_capture_all(sessions_dir, name)
    matches = [env for env in sent if env["type"] == "command_result"]
    assert matches, f"expected command_result envelope, got {[e['type'] for e in sent]}"
    return matches[0]


async def _run_cmd_delete_capture_all(sessions_dir: Path, name: str) -> list[dict]:
    from tesseract.mirror.server import commands as commands_module

    sent: list[dict] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    class _FakeSession:
        def __init__(self) -> None:
            self.session_id = "test-session"
            self.event_log: list[dict] = []
            self.ws = _FakeWS()
            self.save_name: str | None = None

    fake_session = _FakeSession()
    with patch.object(commands_module, "SESSIONS_DIR", sessions_dir):
        await commands_module.cmd_delete(fake_session, name)
    return sent
