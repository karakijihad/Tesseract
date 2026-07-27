"""Phase 5 Task 3 — `_forward_to_observer` scrubs secrets at the CAPTURE
point, before the chunk ever reaches `observer.observe_incremental`.

Fixture pattern mirrors `fix_pass_2026_04_25/test_pty_observer_backpressure.py`
(same `TerminalServerConfig` + stub `PTYManager` wiring).
"""

from __future__ import annotations

from aiohttp import web

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.mirror.server.pty_manager import PTYManager


class _RecordingObserver:
    def __init__(self) -> None:
        self.pushed_lines: list[dict] = []

    async def observe_incremental(self, new_turns, mode="meta"):
        self.pushed_lines.extend(new_turns)
        return None


def _build_pty_with_observer(observer) -> tuple[PTYManager, web.Application]:
    cfg = TerminalServerConfig(
        default_shell="bash",
        max_tabs=1,
        max_panes_per_tab=4,
        shell_profiles={"bash": ShellProfile(argv=("bash",), label="bash")},
        coalesce_flush_ms=8.0,
        coalesce_flush_chars=4096,
        reattach_grace_s=30.0,
        pause_buffer_cap_chars=2_000_000,
    )
    pty = PTYManager(cfg)
    app = web.Application()
    app["observer"] = observer
    app["observer_state"] = "observing"
    app["observer_consented_panes"] = {"p1"}
    pty.bind_app(app)
    return pty, app


async def test_forward_to_observer_scrubs_secret_before_push() -> None:
    observer = _RecordingObserver()
    pty, _app = _build_pty_with_observer(observer)

    pty._forward_to_observer("p1", "export OPENAI_API_KEY=sk-abc123DEF456ghi789JKL\n")
    await _drain_tasks(pty)

    assert observer.pushed_lines, "observer never received the pushed line"
    text = observer.pushed_lines[0]["text"]
    assert "sk-abc123DEF456ghi789JKL" not in text
    assert "[redacted]" in text


async def test_forward_to_observer_passes_through_non_secret_text() -> None:
    observer = _RecordingObserver()
    pty, _app = _build_pty_with_observer(observer)

    pty._forward_to_observer("p1", "npm run build\n")
    await _drain_tasks(pty)

    assert observer.pushed_lines[0]["text"] == "npm run build\n"


async def _drain_tasks(pty: PTYManager) -> None:
    import asyncio

    tasks = list(pty._app.get("observer_pty_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)
