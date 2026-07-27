import json
import pytest
from tesseract.orchestrator.tars_controller.interactive.cli_adapter import (
    ClaudeStreamAdapter,
)

def test_open_argv_has_stream_json_and_no_resume():
    a = ClaudeStreamAdapter()
    argv = a.build_argv(task="do X", session_id=None)
    assert "claude" in argv[0]
    assert "-p" in argv
    assert "do X" in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv
    assert "--resume" not in argv

def test_send_argv_has_resume():
    a = ClaudeStreamAdapter()
    argv = a.build_argv(task="next", session_id="sess-1")
    assert "--resume" in argv
    i = argv.index("--resume")
    assert argv[i + 1] == "sess-1"

def test_full_access_flag_present():
    a = ClaudeStreamAdapter()
    argv = a.build_argv(task="x", session_id=None)
    assert "--dangerously-skip-permissions" in argv

def test_model_flag_present_when_model_set():
    a = ClaudeStreamAdapter(model="claude-test-model")
    argv = a.build_argv(task="x", session_id=None)
    i = argv.index("--model")
    assert argv[i + 1] == "claude-test-model"

def test_no_model_flag_when_model_unset():
    a = ClaudeStreamAdapter()
    argv = a.build_argv(task="x", session_id=None)
    assert "--model" not in argv

@pytest.mark.asyncio
async def test_run_turn_parses_fake_stream():
    events = [
        {"type": "system", "subtype": "init", "session_id": "s9"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "result", "subtype": "success", "result": "ok", "usage": {}},
    ]
    lines = [(json.dumps(e) + "\n").encode() for e in events]

    async def fake_spawn(argv, cwd):
        return _FakeProc(lines)

    a = ClaudeStreamAdapter(spawn=fake_spawn)
    acc = await a.run_turn(task="hi", session_id=None, cwd=".", on_event=lambda e: None)
    assert acc.session_id == "s9"
    assert acc.result_text == "ok"
    assert acc.done is True


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


@pytest.mark.asyncio
async def test_run_turn_cancel_kills_and_drains():
    import asyncio
    cancel = asyncio.Event(); cancel.set()
    killed = {"v": False}
    class _P:
        def __init__(self): self.stdout = _S(); self.returncode = None
        async def wait(self): self.returncode = -9; return -9
        def kill(self): killed["v"] = True
    class _S:
        async def readline(self): return b""
        async def read(self, n): return b""
    async def fake_spawn(argv, cwd): return _P()
    a = ClaudeStreamAdapter(spawn=fake_spawn)
    acc = await a.run_turn(task="x", session_id=None, cwd=".", on_event=lambda e: None, cancel_event=cancel)
    assert killed["v"] is True


# ─── Fix 3: turn_timeout bounds a hung subprocess ────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_cancel_aborts_mid_readline():
    """M2: cancel_event fired WHILE readline is pending (silent stdout, no
    turn_timeout) must abort promptly — checking cancel only between lines
    would hang a mid-tool-call turn until the CLI next prints."""
    import asyncio

    killed = {"v": False}

    class _HangingStdout:
        async def readline(self):
            await asyncio.sleep(3600)
            return b""

        async def read(self, n):
            return b""

    class _HangingProc:
        def __init__(self):
            self.stdout = _HangingStdout()
            self.returncode = None

        def kill(self):
            killed["v"] = True
            self.returncode = -9

        async def wait(self):
            return self.returncode or -9

    async def fake_spawn(argv, cwd):
        return _HangingProc()

    cancel = asyncio.Event()

    async def _fire():
        await asyncio.sleep(0.05)
        cancel.set()

    fire_task = asyncio.create_task(_fire())
    a = ClaudeStreamAdapter(spawn=fake_spawn)
    # wait_for guards against a regression: without the readline/cancel race
    # this would hang forever (no turn_timeout set).
    await asyncio.wait_for(
        a.run_turn(
            task="steer-away", session_id=None, cwd=".",
            on_event=lambda e: None, cancel_event=cancel,
        ),
        timeout=2.0,
    )
    await fire_task
    assert killed["v"] is True


@pytest.mark.asyncio
async def test_run_turn_times_out_on_hanging_stdout():
    """A subprocess whose stdout never yields must be killed and return
    is_error=True within the turn_timeout deadline.

    Uses a fake stdout whose readline blocks indefinitely (asyncio.Future
    that never resolves), and a tiny turn_timeout of 0.05s so the test
    completes quickly and deterministically.
    """
    import asyncio

    killed = {"v": False}

    class _HangingStdout:
        async def readline(self):
            await asyncio.sleep(3600)  # effectively infinite
            return b""

        async def read(self, n):
            return b""

    class _HangingProc:
        def __init__(self):
            self.stdout = _HangingStdout()
            self.returncode = None

        def kill(self):
            killed["v"] = True
            self.returncode = -9

        async def wait(self):
            return self.returncode or -9

    async def fake_spawn(argv, cwd):
        return _HangingProc()

    a = ClaudeStreamAdapter(spawn=fake_spawn)
    acc = await a.run_turn(
        task="hang-me",
        session_id=None,
        cwd=".",
        on_event=lambda e: None,
        turn_timeout=0.05,
    )

    assert acc.is_error, "timed-out turn must be marked is_error"
    assert "timed out" in acc.result_text, f"unexpected result_text: {acc.result_text!r}"
    assert killed["v"] is True, "proc.kill() must be called on timeout"
