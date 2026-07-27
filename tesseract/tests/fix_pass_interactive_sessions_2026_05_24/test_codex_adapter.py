from __future__ import annotations

import json
import pytest
from tesseract.orchestrator.tars_controller.interactive.cli_adapter import (
    CodexStreamAdapter,
)
from tesseract.orchestrator.tars_controller.interactive.stream_parser import (
    CodexTurnAccumulator,
)


# ── argv tests ──────────────────────────────────────────────────────────────

def test_open_argv_no_resume():
    a = CodexStreamAdapter()
    argv = a.build_argv(task="do X", session_id=None)
    # argv[0] is the machine-resolved binary (native codex.exe preferred
    # over the npm .cmd wrapper since trio W1 — cmd.exe %* argv mangling).
    assert argv[0] == a.binary
    assert "exec" in argv
    assert "do X" in argv
    assert "resume" not in argv
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv


def test_open_argv_shape():
    a = CodexStreamAdapter()
    argv = a.build_argv(task="do X", session_id=None)
    # exact open form: <resolved codex> exec <task> --json ...
    assert argv == [
        a.binary, "exec", "do X",
        "--json", "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_resume_argv_contains_resume_and_session_id():
    a = CodexStreamAdapter()
    argv = a.build_argv(task="next step", session_id="th-99")
    assert "resume" in argv
    i = argv.index("resume")
    assert argv[i + 1] == "th-99"
    assert argv[i + 2] == "next step"


def test_resume_argv_shape():
    a = CodexStreamAdapter()
    argv = a.build_argv(task="next step", session_id="th-99")
    assert argv == [
        a.binary, "exec", "resume", "th-99", "next step",
        "--json", "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
    ]


def test_full_access_flag_present():
    a = CodexStreamAdapter()
    argv = a.build_argv(task="x", session_id=None)
    assert "--dangerously-bypass-approvals-and-sandbox" in argv


def test_model_flag_present_when_model_set():
    a = CodexStreamAdapter(model="codex-test-model")
    argv = a.build_argv(task="x", session_id=None)
    i = argv.index("--model")
    assert argv[i + 1] == "codex-test-model"


def test_no_model_flag_when_model_unset():
    a = CodexStreamAdapter()
    argv = a.build_argv(task="x", session_id=None)
    assert "--model" not in argv


# ── run_turn integration ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_turn_parses_fake_codex_stream():
    events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
        {"type": "turn.completed", "usage": {}},
    ]
    lines = [(json.dumps(e) + "\n").encode() for e in events]

    async def fake_spawn(argv, cwd):
        return _FakeProc(lines)

    a = CodexStreamAdapter(spawn=fake_spawn)
    acc = await a.run_turn(task="hi", session_id=None, cwd=".", on_event=lambda e: None)
    assert acc.session_id == "th-1"
    assert acc.result_text == "ok"
    assert acc.done is True


@pytest.mark.asyncio
async def test_run_turn_non_agent_item_not_accumulated():
    """reasoning / command_execution items must not appear in result_text."""
    events = [
        {"type": "thread.started", "thread_id": "th-2"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking..."}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
        {"type": "turn.completed", "usage": {}},
    ]
    lines = [(json.dumps(e) + "\n").encode() for e in events]

    async def fake_spawn(argv, cwd):
        return _FakeProc(lines)

    a = CodexStreamAdapter(spawn=fake_spawn)
    acc = await a.run_turn(task="hi", session_id=None, cwd=".", on_event=lambda e: None)
    assert acc.result_text == "answer"


@pytest.mark.asyncio
async def test_run_turn_cancel_kills_and_drains():
    import asyncio
    cancel = asyncio.Event()
    cancel.set()
    killed = {"v": False}

    class _P:
        def __init__(self):
            self.stdout = _S()
            self.returncode = None

        async def wait(self):
            self.returncode = -9
            return -9

        def kill(self):
            killed["v"] = True

    class _S:
        async def readline(self):
            return b""

        async def read(self, n):
            return b""

    async def fake_spawn(argv, cwd):
        return _P()

    a = CodexStreamAdapter(spawn=fake_spawn)
    await a.run_turn(task="x", session_id=None, cwd=".", on_event=lambda e: None, cancel_event=cancel)
    assert killed["v"] is True


# ── CodexTurnAccumulator unit tests ─────────────────────────────────────────

def test_codex_accumulator_thread_started():
    acc = CodexTurnAccumulator()
    acc.feed({"type": "thread.started", "thread_id": "th-abc"})
    assert acc.session_id == "th-abc"
    assert acc.done is False


def test_codex_accumulator_agent_message_text():
    acc = CodexTurnAccumulator()
    acc.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello "}})
    acc.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "world"}})
    assert acc.result_text == "Hello world"
    assert acc.done is False


def test_codex_accumulator_non_agent_item_ignored():
    acc = CodexTurnAccumulator()
    acc.feed({"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}})
    acc.feed({"type": "item.completed", "item": {"type": "command_execution", "text": "ls"}})
    assert acc.result_text == ""
    assert acc.done is False


def test_codex_accumulator_turn_completed():
    acc = CodexTurnAccumulator()
    acc.feed({"type": "thread.started", "thread_id": "th-x"})
    acc.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
    acc.feed({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}})
    assert acc.done is True
    assert acc.is_error is False
    assert acc.usage == {"input_tokens": 5, "output_tokens": 3}
    assert acc.result_text == "done"


def test_codex_accumulator_error_event():
    acc = CodexTurnAccumulator()
    acc.feed({"type": "error", "message": "something broke"})
    assert acc.done is True
    assert acc.is_error is True
    assert acc.result_text == "something broke"


def test_codex_accumulator_turn_failed_event():
    acc = CodexTurnAccumulator()
    acc.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}})
    acc.feed({"type": "turn.failed", "error": "network timeout"})
    assert acc.done is True
    assert acc.is_error is True
    # error string used as result_text when no agent_message fallback override
    assert "network timeout" in acc.result_text


def test_codex_accumulator_turn_failed_falls_back_to_agent_text():
    """When turn.failed carries no message/error key, fall back to accumulated text."""
    acc = CodexTurnAccumulator()
    acc.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "partial answer"}})
    acc.feed({"type": "turn.failed"})
    assert acc.done is True
    assert acc.is_error is True
    assert acc.result_text == "partial answer"


# ── shared helpers ────────────────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.returncode = 0

    async def wait(self):
        return 0

    def kill(self):
        pass


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""

    async def read(self, n):
        return b""
