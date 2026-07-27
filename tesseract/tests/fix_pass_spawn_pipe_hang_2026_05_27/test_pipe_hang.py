"""Background-spawn hang — process exit must drive completion, not stdout EOF.

Repro: a delegate_codex task that starts a longer-lived grandchild (a dev
server on localhost:8787) leaves the inherited stdout write-end open. Reading
to EOF then blocks until `timeout` even though `codex exec` itself already
exited, so the background SpawnHandle stays `running` for the full timeout
(operator-observed 2026-05-27, session 2026-05-27-2129).

Fix: `run_subprocess_with_sink` / `race_communicate` complete on
`process.wait()` and only drain stdout for a short grace afterward.
"""

from __future__ import annotations

import asyncio

import pytest

from tesseract.kernel.tools import cli_stream
from tesseract.kernel.tools.cli_stream import (
    race_communicate,
    run_subprocess_with_sink,
)


class _Stdout:
    """Yields scripted chunks, then either EOF (`b""`) or blocks forever.

    `hang=True` simulates a surviving grandchild holding the pipe open —
    the read after the last chunk never returns.
    """

    def __init__(self, chunks: list[bytes], *, hang: bool) -> None:
        self._chunks = list(chunks)
        self._hang = hang

    async def read(self, _n: int = -1) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        if self._hang:
            await asyncio.sleep(3600)
        return b""


class _Proc:
    """Fake asyncio subprocess whose process exits immediately even while a
    grandchild keeps the stdout pipe open."""

    def __init__(self, stdout: _Stdout, stderr: _Stdout | None = None, rc: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = None
        self._rc = rc
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc

    def kill(self) -> None:
        self.killed = True


def _patch_exec(monkeypatch, proc: _Proc) -> None:
    async def _fake_exec(*_a, **_k):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


@pytest.mark.asyncio
async def test_sink_returns_after_exit_when_pipe_stays_open(monkeypatch):
    monkeypatch.setattr(cli_stream, "DRAIN_GRACE_SECONDS", 0.05)
    _patch_exec(monkeypatch, _Proc(_Stdout([b"hello from codex\n"], hang=True)))

    result = await run_subprocess_with_sink(
        tool_name="delegate_codex",
        argv=("codex", "exec", "task"),
        cwd=".",
        timeout=2.0,  # old code would block ~this long; the fix returns in ~grace
        sink=None,
        call_id="c1",
        empty_message="codex returned empty output",
        missing_message="codex CLI not found",
    )

    assert result.is_error is False
    assert "hello from codex" in result.output
    assert result.metadata["exit_code"] == 0


@pytest.mark.asyncio
async def test_sink_normal_eof_still_returns_full_output(monkeypatch):
    monkeypatch.setattr(cli_stream, "DRAIN_GRACE_SECONDS", 0.05)
    _patch_exec(monkeypatch, _Proc(_Stdout([b"line one\n", b"line two\n"], hang=False)))

    result = await run_subprocess_with_sink(
        tool_name="delegate_codex",
        argv=("codex", "exec", "task"),
        cwd=".",
        timeout=5.0,
        sink=None,
        call_id="c1",
        empty_message="codex returned empty output",
        missing_message="codex CLI not found",
    )

    assert result.is_error is False
    assert "line one" in result.output and "line two" in result.output


@pytest.mark.asyncio
async def test_race_communicate_returns_after_exit_when_pipe_stays_open(monkeypatch):
    monkeypatch.setattr(cli_stream, "DRAIN_GRACE_SECONDS", 0.05)
    proc = _Proc(
        _Stdout([b"audit done\n"], hang=True),
        stderr=_Stdout([], hang=True),
    )

    result = await race_communicate(proc, None, timeout=2.0, tool_name="delegate_codex")

    assert result is not None
    stdout, _stderr = result
    assert b"audit done" in stdout
