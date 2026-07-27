"""F6 (terminal daily-driver, 2026-07-05) — WS-drop grace period + reattach.

A dropped WS no longer kills the PTY outright (that turned every page
reload into a dead shell). `cleanup_for_ws` now marks owned panes
detached and starts a grace timer; a client that reattaches within the
grace window gets the pane repointed at its new WS and replays only the
gap since its last-seen cursor (reusing `output_total_chars` /
`read_buffer_for_pane` — no second buffer). Grace expiry falls back to
the existing kill path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def _make_manager(reattach_grace_s: float = 30.0) -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="cmd",
        max_tabs=4,
        max_panes_per_tab=4,
        shell_profiles={"cmd": ShellProfile(argv=("cmd.exe",), label="cmd")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=reattach_grace_s,
        pause_buffer_cap_chars=2_000_000,
    )
    return PTYManager(cfg)


def _make_entry(manager: PTYManager, pane_id: str, ws: Any) -> PTYEntry:
    proc = SimpleNamespace(
        isalive=lambda: True,
        write=lambda *a, **k: None,
        terminate=lambda *a, **k: None,
        read=lambda *a, **k: "",
    )
    entry = PTYEntry(pane_id=pane_id, shell="cmd", proc=proc, ws=ws)  # type: ignore[arg-type]
    manager._ptys[pane_id] = entry  # noqa: SLF001
    return entry


def _fake_ws(sent: list[dict[str, Any]], *, closed: bool = False) -> SimpleNamespace:
    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)
    return SimpleNamespace(closed=closed, send_json=_send_json)


@pytest.mark.asyncio
async def test_ws_drop_detaches_instead_of_killing() -> None:
    manager = _make_manager()
    old_sent: list[dict[str, Any]] = []
    old_ws = _fake_ws(old_sent)
    entry = _make_entry(manager, "pty_drop", old_ws)

    await manager.cleanup_for_ws(old_ws)

    assert "pty_drop" in manager._ptys, "pane was killed on WS drop instead of detached"
    assert entry.detached_at is not None


@pytest.mark.asyncio
async def test_grace_period_reattach_replays_only_gap() -> None:
    manager = _make_manager()
    old_sent: list[dict[str, Any]] = []
    old_ws = _fake_ws(old_sent)
    entry = _make_entry(manager, "pty_gap", old_ws)
    manager._append_to_buffer(entry, "hello world")  # noqa: SLF001 — total_chars=11

    await manager.cleanup_for_ws(old_ws)
    assert entry.detached_at is not None

    new_sent: list[dict[str, Any]] = []
    new_ws = _fake_ws(new_sent)
    await manager._reattach("pty_gap", "5", new_ws)  # noqa: SLF001 — client last saw 5 chars

    assert entry.detached_at is None
    assert entry.ws is new_ws
    acks = [f for f in new_sent if f.get("type") == "terminal_reattached"]
    assert len(acks) == 1 and acks[0]["pane_id"] == "pty_gap"
    replays = [f for f in new_sent if f.get("type") == "terminal_output_chunk"]
    assert len(replays) == 1
    assert replays[0]["bytes"] == " world", (
        f"expected only the gap after cursor 5, got {replays[0]['bytes']!r}"
    )
    # Live re-verification finding — the replay chunk must be flagged so
    # the client knows to suppress sendKeystroke for any terminal-query
    # response xterm.js auto-emits while re-parsing it (see terminal.ts).
    assert replays[0]["replay"] is True
    # No double-echo: nothing was sent to the OLD (dropped) ws on reattach.
    assert old_sent == []


@pytest.mark.asyncio
async def test_dispatch_fresh_reattach_forces_full_replay_ignoring_since_token() -> None:
    """Live-gate fix pass, Finding 1 — `bootstrapPanes()` (page reload) now
    sends `fresh: true` on every reattach. Even if a stale since_token also
    rides along (e.g. a client bug, or the pre-fix persisted-cursor path),
    `dispatch()` must force a full replay — the fresh xterm this pane is
    about to attach to has zero content, so a gap-only replay would leave
    it blank."""
    manager = _make_manager()
    entry = _make_entry(manager, "pty_fresh", _fake_ws([]))
    manager._append_to_buffer(entry, "hello world")  # noqa: SLF001 — total=11

    new_sent: list[dict[str, Any]] = []
    new_ws = _fake_ws(new_sent)
    await manager.dispatch(
        {"type": "terminal_reattach", "pane_id": "pty_fresh", "since_token": "5", "fresh": True},
        new_ws,
    )

    replays = [f for f in new_sent if f.get("type") == "terminal_output_chunk"]
    assert len(replays) == 1
    assert replays[0]["bytes"] == "hello world", (
        f"fresh:true must replay the FULL buffer, not the gap after cursor 5, got {replays[0]['bytes']!r}"
    )


@pytest.mark.asyncio
async def test_reattach_snapshot_taken_before_first_send_await() -> None:
    """Review finding (Critical) — the replay snapshot must be computed
    synchronously BEFORE the first `await self._send(...)` in
    `_reattach`. `entry.ws`/`entry.resume_event` are repointed/reset
    before that snapshot, so if a live flush (the reader loop pushing
    fresh output to the now-repointed ws) lands during the await, it
    must NOT also be folded into the replay — otherwise the client gets
    those bytes twice (once live, once replayed).

    `_send` is patched so its FIRST call (the `terminal_reattached` ack)
    appends new output to the buffer before delegating to the real
    implementation — simulating the reader_task's live flush racing in
    during that await. The replay actually sent must equal only the
    gap that existed at reattach time, not the raced-in bytes.
    """
    manager = _make_manager()
    old_sent: list[dict[str, Any]] = []
    old_ws = _fake_ws(old_sent)
    entry = _make_entry(manager, "pty_race", old_ws)
    manager._append_to_buffer(entry, "hello world")  # noqa: SLF001 — total=11

    await manager.cleanup_for_ws(old_ws)
    assert entry.detached_at is not None

    new_sent: list[dict[str, Any]] = []
    new_ws = _fake_ws(new_sent)

    real_send = PTYManager._send
    send_calls = 0

    async def _racy_send(ws: Any, payload: dict[str, Any]) -> None:
        nonlocal send_calls
        send_calls += 1
        if send_calls == 1:
            # Simulate the reader_task flushing NEW output to the
            # already-repointed entry.ws during this await.
            manager._append_to_buffer(entry, "!LIVE!")  # noqa: SLF001
        await real_send(ws, payload)

    with patch.object(PTYManager, "_send", staticmethod(_racy_send)):
        await manager._reattach("pty_race", "5", new_ws)  # noqa: SLF001

    assert send_calls == 2, "expected exactly one ack send + one replay send"
    acks = [f for f in new_sent if f.get("type") == "terminal_reattached"]
    assert len(acks) == 1
    replays = [f for f in new_sent if f.get("type") == "terminal_output_chunk"]
    assert len(replays) == 1
    assert replays[0]["bytes"] == " world", (
        "replay included bytes that raced in during the ack await — "
        f"double-echo, got {replays[0]['bytes']!r}"
    )


@pytest.mark.asyncio
async def test_grace_expiry_kills_pane() -> None:
    manager = _make_manager(reattach_grace_s=0.02)
    old_sent: list[dict[str, Any]] = []
    old_ws = _fake_ws(old_sent)
    _make_entry(manager, "pty_expire", old_ws)

    await manager.cleanup_for_ws(old_ws)
    assert "pty_expire" in manager._ptys

    await asyncio.sleep(0.15)

    assert "pty_expire" not in manager._ptys, "grace expiry did not kill the pane"


@pytest.mark.asyncio
async def test_second_ws_claiming_attached_pane_repoints_last_claimer_wins() -> None:
    """A pane that's still attached (never detached) can be claimed by a
    new WS too — `_reattach` follows the same last-claimer-wins semantic
    `dispatch` already applies to `primary_ws`; no separate exclusivity
    lock is invented."""
    manager = _make_manager()
    first_sent: list[dict[str, Any]] = []
    first_ws = _fake_ws(first_sent)
    entry = _make_entry(manager, "pty_live", first_ws)
    assert entry.detached_at is None  # never dropped

    second_sent: list[dict[str, Any]] = []
    second_ws = _fake_ws(second_sent)
    await manager._reattach("pty_live", None, second_ws)  # noqa: SLF001

    assert entry.ws is second_ws
    acks = [f for f in second_sent if f.get("type") == "terminal_reattached"]
    assert len(acks) == 1


@pytest.mark.asyncio
async def test_reattach_failed_when_pane_gone() -> None:
    manager = _make_manager()
    sent: list[dict[str, Any]] = []
    ws = _fake_ws(sent)

    await manager._reattach("pty_never_existed", "0", ws)  # noqa: SLF001

    assert sent == [{"type": "terminal_reattach_failed", "pane_id": "pty_never_existed"}]
