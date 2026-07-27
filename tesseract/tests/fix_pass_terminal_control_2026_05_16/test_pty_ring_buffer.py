"""Phase 2/3/5 — PTY ring buffer + wait_idle + read_screen + list + close.

Server-side coverage. Each test instantiates a real :class:`PTYManager`
and a real :class:`PTYEntry` with a stubbed :class:`PtyProcess`, so we
exercise the ring-buffer / quiescence / read-cursor logic without
spawning a winpty subprocess (and without writing to ``tesseract/
logs/`` — fixtures pin ``TESSERACT_HOME`` to ``tmp_path`` defensively).

Tests pin behavior, not implementation:
- buffer eviction at OUTPUT_BUFFER_CHAR_CAP
- monotonic byte_count even after eviction
- since_token delta read; truncated flag when cursor predates the buffer
- ANSI stripping in non-raw mode; raw mode preserves escapes
- wait_idle returns 'idle' after quiescence_ms of silence
- wait_idle returns 'matched' when the pattern hits
- wait_idle returns 'timeout' when neither fires
- list_panes_for_agent enumerates live entries
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.mirror.server.config import (
    ShellProfile,
    TerminalServerConfig,
)
from tesseract.mirror.server.pty_manager import (
    OUTPUT_BUFFER_CHAR_CAP,
    PTYEntry,
    PTYManager,
)


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch) -> None:
    """CLAUDE.md zero-tolerance: never let tests write to tesseract/logs/.
    Pin TESSERACT_HOME so any log writer instantiated downstream lands
    under tmp_path even if it resolves at import-time.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def _make_manager() -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="cmd",
        max_tabs=4,
        max_panes_per_tab=4,
        shell_profiles={"cmd": ShellProfile(argv=("cmd.exe",), label="cmd")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    return PTYManager(cfg)


def _make_entry(manager: PTYManager, pane_id: str = "pty_test_0001") -> PTYEntry:
    """Insert a fake PTYEntry whose .proc.isalive returns True. The
    proc object is never .read/.write/.terminate'd in these tests —
    every code path under exercise goes through manager state, not
    PtyProcess methods.
    """
    proc = SimpleNamespace(
        isalive=lambda: True,
        # Defensive — if a future test path calls these, fail loud.
        write=lambda *a, **k: (_ for _ in ()).throw(AssertionError("proc.write touched")),
        terminate=lambda *a, **k: None,
        read=lambda *a, **k: "",
    )
    ws = SimpleNamespace(closed=False, send_json=lambda *a, **k: None)
    entry = PTYEntry(
        pane_id=pane_id,
        shell="cmd",
        proc=proc,  # type: ignore[arg-type]
        ws=ws,  # type: ignore[arg-type]
    )
    manager._ptys[pane_id] = entry  # noqa: SLF001 — direct test seam
    return entry


# ─── ring buffer eviction + cursor ────────────────────────────────


def test_buffer_appends_chunks_and_tracks_total() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)

    manager._append_to_buffer(entry, "hello ")  # noqa: SLF001
    manager._append_to_buffer(entry, "world\n")  # noqa: SLF001

    assert entry.output_buffer_chars == len("hello world\n")
    assert entry.output_total_chars == len("hello world\n")
    assert "".join(entry.output_buffer) == "hello world\n"


def test_buffer_evicts_oldest_when_cap_reached() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)

    # Fill to cap with three roughly equal chunks; then push one more
    # to force eviction of the head chunk.
    chunk_size = OUTPUT_BUFFER_CHAR_CAP // 3
    manager._append_to_buffer(entry, "A" * chunk_size)  # noqa: SLF001
    manager._append_to_buffer(entry, "B" * chunk_size)  # noqa: SLF001
    manager._append_to_buffer(entry, "C" * chunk_size)  # noqa: SLF001
    pre_eviction_total = entry.output_total_chars

    manager._append_to_buffer(entry, "D" * chunk_size)  # noqa: SLF001

    # Cap is not exceeded.
    assert entry.output_buffer_chars <= OUTPUT_BUFFER_CHAR_CAP
    # Monotonic total reflects all four pushes.
    assert entry.output_total_chars == pre_eviction_total + chunk_size
    # Head chunk is gone — buffer no longer contains an 'A'.
    assert "A" not in "".join(entry.output_buffer)
    # Tail chunk is intact.
    assert "D" in "".join(entry.output_buffer)


def test_single_giant_chunk_is_tail_trimmed() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)

    payload = "X" * (OUTPUT_BUFFER_CHAR_CAP * 2)
    manager._append_to_buffer(entry, payload)  # noqa: SLF001

    assert entry.output_buffer_chars == OUTPUT_BUFFER_CHAR_CAP
    assert entry.output_total_chars == len(payload)


# ─── read_buffer_for_pane ─────────────────────────────────────────


def test_read_buffer_tail_chars_returns_suffix() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "abcdef")  # noqa: SLF001

    result = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=3, since_token=None, raw=False,
    )
    assert result["ok"] is True
    assert result["text"] == "def"
    assert result["next_token"] == "6"


def test_read_buffer_since_token_returns_delta() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "first.")  # noqa: SLF001

    snap_a = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=None, raw=False,
    )
    token_a = snap_a["next_token"]

    manager._append_to_buffer(entry, "second.")  # noqa: SLF001
    snap_b = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=token_a, raw=False,
    )
    assert snap_b["text"] == "second."
    assert snap_b["truncated"] is False
    assert snap_b["next_token"] == str(len("first.") + len("second."))


def test_read_buffer_since_token_at_head_returns_empty() -> None:
    """Cursor == total is the primary polling-loop path: caller has
    already seen everything; tool returns empty text with the same
    next_token. Pinned because the polling loop is the most common
    production use.
    """
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "alpha")  # noqa: SLF001
    snap = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=None, raw=False,
    )
    again = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=snap["next_token"], raw=False,
    )
    assert again["ok"] is True
    assert again["text"] == ""
    assert again["next_token"] == snap["next_token"]
    assert again["truncated"] is False


def test_read_buffer_truncated_when_cursor_predates_buffer() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "AAA")  # noqa: SLF001
    snap_a = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=None, raw=False,
    )
    token_a = snap_a["next_token"]  # 3
    # Rotate the buffer past the cursor.
    manager._append_to_buffer(entry, "X" * (OUTPUT_BUFFER_CHAR_CAP + 10))  # noqa: SLF001

    snap_b = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=token_a, raw=False,
    )
    assert snap_b["truncated"] is True
    # The "AAA" prefix is gone — we got whatever survives in the buffer.
    assert "A" not in snap_b["text"]


def test_read_buffer_clamps_negative_cursor_to_full_replay() -> None:
    """Live-gate fix pass, Finding 1 — a cursor a client carried over from
    a previous page lifetime (or a raw negative value) must never error
    out or return nothing; it must clamp to the buffer start and replay
    everything held, since the terminal it's about to be paired with may
    have zero content (a fresh reload). Rotate the buffer first so the
    buffer start is > 0 — pre-fix, a negative cursor errored regardless
    of rotation state; the clamp must produce the full held buffer here
    too, not just when buffer_start happens to be 0."""
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "X" * (OUTPUT_BUFFER_CHAR_CAP + 10))  # noqa: SLF001

    result = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token="-1", raw=False,
    )
    assert result["ok"] is True
    assert result["text"] == "".join(entry.output_buffer)
    assert result["truncated"] is True


def test_read_buffer_clamps_cursor_ahead_of_total() -> None:
    """A cursor ahead of what this pane has actually produced (stale /
    corrupted) used to error as since_token_out_of_range; it now clamps
    to `total` — the safe "nothing new yet" reading rather than a
    dropped/failed replay."""
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "hi")  # noqa: SLF001

    result = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token="999999", raw=False,
    )
    assert result["ok"] is True
    assert result["text"] == ""
    assert result["truncated"] is False


def test_read_buffer_strips_ansi_unless_raw() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "before\x1b[31mRED\x1b[0mafter")  # noqa: SLF001

    stripped = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=None, raw=False,
    )
    raw = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token=None, raw=True,
    )

    assert stripped["text"] == "beforeREDafter"
    assert "\x1b[31m" in raw["text"]


def test_read_buffer_pane_not_found() -> None:
    manager = _make_manager()
    result = manager.read_buffer_for_pane(
        "nope", tail_chars=10, since_token=None, raw=False,
    )
    assert result["ok"] is False
    assert result["error"] == "pane_not_found"


def test_read_buffer_bad_since_token() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    result = manager.read_buffer_for_pane(
        entry.pane_id, tail_chars=0, since_token="not-an-int", raw=False,
    )
    assert result["ok"] is False
    assert result["error"] == "bad_since_token"


# ─── wait_idle_for_pane ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_idle_returns_idle_after_quiescence() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "burst")  # noqa: SLF001
    # last_output_at is `now` — wait 80 ms of silence; quiescence_ms=50
    # should resolve as soon as we've been idle that long.
    result = await manager.wait_idle_for_pane(
        entry.pane_id,
        quiescence_ms=50.0,
        pattern=None,
        timeout_ms=2000.0,
        tail_chars=200,
    )
    assert result["ok"] is True
    assert result["status"] == "idle"
    assert result["tail"] == "burst"
    assert result["idle_ms"] >= 50.0


@pytest.mark.asyncio
async def test_wait_idle_matches_pattern_against_stripped_tail() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    manager._append_to_buffer(entry, "loading...\x1b[32mready>\x1b[0m")  # noqa: SLF001

    result = await manager.wait_idle_for_pane(
        entry.pane_id,
        quiescence_ms=10_000.0,  # never reaches quiescence
        pattern=r"ready>",
        timeout_ms=2000.0,
        tail_chars=200,
    )
    assert result["status"] == "matched"
    assert result["match"] == "ready>"


@pytest.mark.asyncio
async def test_wait_idle_timeout_when_neither_fires() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)

    async def _heartbeat() -> None:
        # Push a chunk every 30 ms so quiescence (200 ms) never wins.
        for _ in range(8):
            await asyncio.sleep(0.030)
            manager._append_to_buffer(entry, "tick ")  # noqa: SLF001

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        result = await manager.wait_idle_for_pane(
            entry.pane_id,
            quiescence_ms=200.0,
            pattern=r"NEVER",
            timeout_ms=180.0,
            tail_chars=200,
        )
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    assert result["status"] == "timeout"


@pytest.mark.asyncio
async def test_wait_idle_short_circuits_on_dead_pane() -> None:
    """Dead pane stops updating last_output_at; without a liveness
    check, quiescence would eventually fire and trick TARS into
    thinking the CLI was just done. Reviewer-driven regression pin.
    """
    manager = _make_manager()
    entry = _make_entry(manager)
    # Flip the stub to dead. The reader loop normally would have popped
    # the entry — we simulate the race where wait_idle holds a
    # reference before _stop runs.
    entry.proc.isalive = lambda: False  # type: ignore[assignment]
    manager._append_to_buffer(entry, "last words before crash")  # noqa: SLF001

    result = await manager.wait_idle_for_pane(
        entry.pane_id,
        quiescence_ms=50.0,
        pattern=None,
        timeout_ms=2000.0,
        tail_chars=200,
    )
    assert result["status"] == "closed"
    assert result["alive"] is False
    assert "last words" in result["tail"]


@pytest.mark.asyncio
async def test_wait_idle_pattern_compile_error_surfaces_cleanly() -> None:
    manager = _make_manager()
    entry = _make_entry(manager)
    result = await manager.wait_idle_for_pane(
        entry.pane_id,
        quiescence_ms=50.0,
        pattern="(unclosed",
        timeout_ms=200.0,
        tail_chars=0,
    )
    assert result["ok"] is False
    assert result["error"].startswith("bad_pattern:")


# ─── list_panes_for_agent ─────────────────────────────────────────


def test_list_panes_returns_each_live_entry() -> None:
    manager = _make_manager()
    a = _make_entry(manager, pane_id="pty_a")
    b = _make_entry(manager, pane_id="pty_b")
    manager._append_to_buffer(a, "hi")  # noqa: SLF001

    rows = manager.list_panes_for_agent()
    by_id = {r["pane_id"]: r for r in rows}
    assert set(by_id) == {"pty_a", "pty_b"}
    assert by_id["pty_a"]["byte_count"] == 2
    assert by_id["pty_b"]["byte_count"] == 0
    assert by_id["pty_a"]["alive"] is True


def test_list_panes_empty_when_no_panes() -> None:
    manager = _make_manager()
    assert manager.list_panes_for_agent() == []
