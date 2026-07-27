"""Shared fixtures for the CR-3 terminal-coalescing suite.

Drives ``PTYManager._reader_loop`` directly with a fake ``proc.read``
that yields a scripted sequence of chunks then raises ``EOFError`` to
terminate the loop. Captures ``ws.send_json`` calls, per-chunk
consumers, and observer pushes so tests can assert on the coalesce
invariant: many small chunks → one WS frame, but consumers + observer
still see each chunk individually.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYEntry, PTYManager

# Test-local mirrors of the prod pty_thresholds config values (permissions.yaml).
# Tests own their own fixture config rather than importing prod constants —
# TerminalServerConfig is config-driven (no more module-level defaults in
# pty_manager.py), so each suite states the values it's exercising.
COALESCE_FLUSH_MS = 8.0
COALESCE_FLUSH_CHARS = 4096
REATTACH_GRACE_S = 30.0
PAUSE_BUFFER_CAP_CHARS = 2_000_000


@pytest.fixture(autouse=True)
def _isolate_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md zero-tolerance: never let tests write to tesseract/logs/."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))


def make_manager() -> PTYManager:
    cfg = TerminalServerConfig(
        default_shell="cmd",
        max_tabs=4,
        max_panes_per_tab=4,
        shell_profiles={"cmd": ShellProfile(argv=("cmd.exe",), label="cmd")},
        coalesce_flush_ms=COALESCE_FLUSH_MS,
        coalesce_flush_chars=COALESCE_FLUSH_CHARS,
        reattach_grace_s=REATTACH_GRACE_S,
        pause_buffer_cap_chars=PAUSE_BUFFER_CAP_CHARS,
    )
    return PTYManager(cfg)


def make_scripted_entry(
    manager: PTYManager,
    pane_id: str,
    chunks: list[Any],
) -> tuple[PTYEntry, list[dict[str, Any]]]:
    """Insert a ``PTYEntry`` whose ``proc.read`` yields the scripted
    sequence ``chunks`` one item per call, then raises ``EOFError``.

    Each item may be a string (returned as the chunk) or a callable
    (executed for its side-effect, then ``""`` returned — useful to
    simulate a delay without inserting a chunk).

    Returns ``(entry, sent)`` where ``sent`` is a list mutated by the
    fake ``ws.send_json`` — each appended dict is one WS frame.
    """
    seq = iter(chunks)

    def _read(_n: int) -> str:
        nxt = next(seq)
        if callable(nxt):
            return nxt() or ""
        return nxt

    proc = SimpleNamespace(
        isalive=lambda: True,
        write=lambda *a, **k: None,
        terminate=lambda *a, **k: None,
        read=_read,
    )

    sent: list[dict[str, Any]] = []

    async def _send_json(payload: dict[str, Any]) -> None:
        sent.append(payload)

    ws = SimpleNamespace(closed=False, send_json=_send_json)
    entry = PTYEntry(
        pane_id=pane_id,
        shell="cmd",
        proc=proc,  # type: ignore[arg-type]
        ws=ws,  # type: ignore[arg-type]
    )
    manager._ptys[pane_id] = entry  # noqa: SLF001 — direct test seam
    return entry, sent
