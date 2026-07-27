"""F2 (terminal daily-driver, 2026-07-05) — server-side output flow control.

`terminal_pause` / `terminal_resume` toggle a per-pane `asyncio.Event` that
the reader loop awaits before every queue drain. Paused: chunks still land
in the asyncio.Queue via the reader thread, but nothing is forwarded to the
WS until resumed. Reattach must also reset pause state so a client that
disposed its term mid-pause can't leave a pane stuck paused forever.

Review follow-up: the queue has no maxsize, so a chatty child process
during a sustained pause would otherwise grow it without bound. Once
chars queued while paused exceed `pause_buffer_cap_chars`, the reader
loop force-resumes its own drain — see
`test_paused_pane_past_cap_force_resumes_drain`.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def _make_manager(*, pause_buffer_cap_chars: int = 2_000_000) -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="cmd",
        max_tabs=4,
        max_panes_per_tab=4,
        shell_profiles={"cmd": ShellProfile(argv=("cmd.exe",), label="cmd")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=pause_buffer_cap_chars,
    )
    return PTYManager(cfg)


@pytest.mark.asyncio
async def test_pause_stops_draining_then_resume_flushes() -> None:
    block_release = threading.Event()
    read_calls: list[int] = []

    def _read(_n: int) -> str:
        read_calls.append(len(read_calls))
        idx = len(read_calls)
        if idx == 1:
            return "before-pause "
        if idx == 2:
            block_release.wait(timeout=2.0)
            return "after-resume "
        raise EOFError()

    sent: list[dict[str, Any]] = []

    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    proc = SimpleNamespace(
        isalive=lambda: True,
        write=lambda *a, **k: None,
        terminate=lambda *a, **k: None,
        read=_read,
    )
    ws = SimpleNamespace(closed=False, send_json=_send_json)
    manager = _make_manager()
    entry = PTYEntry(pane_id="pty_flow", shell="cmd", proc=proc, ws=ws)  # type: ignore[arg-type]
    manager._ptys["pty_flow"] = entry  # noqa: SLF001

    loop_task = asyncio.create_task(manager._reader_loop(entry))  # noqa: SLF001

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
        "before-pause" in f.get("bytes", "") for f in sent
    ):
        await asyncio.sleep(0.005)
    assert any("before-pause" in f.get("bytes", "") for f in sent), "first chunk never arrived"

    await manager._pause("pty_flow")  # noqa: SLF001
    assert not entry.resume_event.is_set()

    # Release the second (blocking) read now that we're paused — the
    # chunk lands in the asyncio.Queue but must NOT reach the WS.
    block_release.set()
    await asyncio.sleep(0.1)
    assert not any("after-resume" in f.get("bytes", "") for f in sent), (
        "chunk forwarded to WS while paused — pause did not stop the drain"
    )

    await manager._resume("pty_flow")  # noqa: SLF001
    assert entry.resume_event.is_set()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
        "after-resume" in f.get("bytes", "") for f in sent
    ):
        await asyncio.sleep(0.005)
    assert any("after-resume" in f.get("bytes", "") for f in sent), (
        "resume did not flush the queued chunk"
    )

    await loop_task


@pytest.mark.asyncio
async def test_paused_pane_past_cap_force_resumes_drain() -> None:
    """Review follow-up (F2) — the reader thread enqueues via
    `put_nowait` into a queue with no maxsize. While paused, a chatty
    child process would otherwise grow that queue without bound. Once
    chars queued while paused exceed `pause_buffer_cap_chars`, the
    reader loop must force itself back into the draining state so
    memory stays bounded."""
    block_release = threading.Event()
    read_calls: list[int] = []

    def _read(_n: int) -> str:
        read_calls.append(len(read_calls))
        idx = len(read_calls)
        if idx == 1:
            return "priming "
        if idx in (2, 3):
            block_release.wait(timeout=2.0)
            return "x" * 15  # two of these (30 chars) exceed a 20-char cap
        raise EOFError()

    sent: list[dict[str, Any]] = []

    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    proc = SimpleNamespace(
        isalive=lambda: True,
        write=lambda *a, **k: None,
        terminate=lambda *a, **k: None,
        read=_read,
    )
    ws = SimpleNamespace(closed=False, send_json=_send_json)
    manager = _make_manager(pause_buffer_cap_chars=20)
    entry = PTYEntry(pane_id="pty_capped", shell="cmd", proc=proc, ws=ws)  # type: ignore[arg-type]
    manager._ptys["pty_capped"] = entry  # noqa: SLF001

    loop_task = asyncio.create_task(manager._reader_loop(entry))  # noqa: SLF001

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
        "priming" in f.get("bytes", "") for f in sent
    ):
        await asyncio.sleep(0.005)
    assert any("priming" in f.get("bytes", "") for f in sent), "priming chunk never arrived"

    await manager._pause("pty_capped")  # noqa: SLF001
    assert not entry.resume_event.is_set()

    # Let the two 15-char chunks land on the (paused) queue — their
    # combined 30 chars exceed the 20-char cap.
    block_release.set()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not entry.resume_event.is_set():
        await asyncio.sleep(0.005)
    assert entry.resume_event.is_set(), (
        "reader loop did not force-resume once queued chars while paused "
        "exceeded pause_buffer_cap_chars — queue would grow unbounded"
    )

    await loop_task


@pytest.mark.asyncio
async def test_reattach_resets_pause_state() -> None:
    """A pane left paused (e.g. the client disposed its term mid-pause)
    must not stay paused forever after a reattach — the new client has
    no way to know it needs to send `terminal_resume` for a pause it
    never initiated."""
    proc = SimpleNamespace(isalive=lambda: True, write=lambda *a, **k: None,
                           terminate=lambda *a, **k: None, read=lambda *a, **k: "")
    old_ws = SimpleNamespace(closed=True)
    manager = _make_manager()
    entry = PTYEntry(pane_id="pty_stuck", shell="cmd", proc=proc, ws=old_ws)  # type: ignore[arg-type]
    manager._ptys["pty_stuck"] = entry  # noqa: SLF001
    entry.resume_event.clear()
    entry.detached_at = time.monotonic()

    sent: list[dict[str, Any]] = []

    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    new_ws = SimpleNamespace(closed=False, send_json=_send_json)
    await manager._reattach("pty_stuck", None, new_ws)  # noqa: SLF001

    assert entry.resume_event.is_set(), "reattach did not reset pause state"
    assert entry.detached_at is None
    assert entry.ws is new_ws
