"""Mirror shutdown teardown tests.

When Mirror exits, anything Mirror owns must shut down: WS sessions
(so per-session autosave/cleanup runs), background tasks, and any
local daemons Mirror itself spawned (Ollama). External daemons stay
running — the operator may have other apps depending on them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web

import tesseract.mirror.server.app as app_mod
import tesseract.mirror.server.ollama_supervisor as sup_mod
from tesseract.mirror.server.ollama_supervisor import OllamaSupervisor


class _FakeProc:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def patch_probe(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict[str, Any] = {"running": True, "tags": ["nomic-embed-text:latest"]}

    def _fake_up(base_url: str, timeout: float = 2.0) -> bool:
        return state["running"]

    async def _fake_tags(
        base_url: str, timeout_s: float = 5.0, *, client: Any = None,
    ) -> list[str]:
        return state["tags"] if state["running"] else []

    monkeypatch.setattr(sup_mod, "ollama_up", _fake_up)
    monkeypatch.setattr(sup_mod, "_fetch_tags", _fake_tags)
    return state


async def test_stop_owned_ollama_terminates_when_owned(patch_probe: dict) -> None:
    """Mirror auto-started Ollama → supervisor holds the Popen → shutdown
    must terminate it so the daemon doesn't outlive the parent."""
    fake_proc = _FakeProc()
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    sup._proc = fake_proc  # noqa: SLF001 — simulating prior auto-start

    app = web.Application()
    app["ollama_supervisor"] = sup

    await app_mod._stop_owned_ollama(app)

    assert fake_proc.terminated is True
    assert sup._proc is None  # noqa: SLF001 — cleared after successful terminate


async def test_stop_owned_ollama_skips_external_instance(patch_probe: dict) -> None:
    """External Ollama (operator started it outside Mirror) must NOT be
    killed on shutdown — other apps may rely on it."""
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    # _proc is None → external

    app = web.Application()
    app["ollama_supervisor"] = sup

    # No raise. Ollama "stays running" because we never call stop().
    await app_mod._stop_owned_ollama(app)
    # Status path probes ollama_up() which our fake says True; that's
    # external state and should be left alone.


async def test_stop_owned_ollama_noop_when_no_supervisor(
    patch_probe: dict,
) -> None:
    """Mirror was configured with `provider != ollama` → no supervisor.
    Shutdown must not raise."""
    app = web.Application()
    app["ollama_supervisor"] = None
    await app_mod._stop_owned_ollama(app)


async def test_close_all_websockets_closes_open_sessions() -> None:
    """Every open WS gets `ws.close(1001, 'mirror shutting down')` so
    the per-session handler's finally-block can run autosave + cleanup
    before the event loop tears down."""
    closed: list[tuple[int, bytes]] = []

    class _FakeWs:
        def __init__(self) -> None:
            self.closed = False

        async def close(self, code: int = 1000, message: bytes = b"") -> None:
            self.closed = True
            closed.append((code, message))

    sessions = {
        "s1": type("S", (), {"ws": _FakeWs(), "session_id": "s1"})(),
        "s2": type("S", (), {"ws": _FakeWs(), "session_id": "s2"})(),
    }
    app = web.Application()
    app["server_sessions"] = sessions

    await app_mod._close_all_websockets(app)

    assert len(closed) == 2
    for code, msg in closed:
        assert code == 1001
        assert msg == b"mirror shutting down"


async def test_close_all_websockets_skips_already_closed() -> None:
    """Sessions whose WS already closed (operator closed the tab mid-shutdown)
    must be skipped — calling close() twice is a wire-level error."""
    closed_count = 0

    class _FakeWs:
        def __init__(self, closed: bool) -> None:
            self.closed = closed

        async def close(self, **_kw: Any) -> None:
            nonlocal closed_count
            closed_count += 1

    sessions = {
        "alive": type("S", (), {"ws": _FakeWs(closed=False), "session_id": "alive"})(),
        "dead": type("S", (), {"ws": _FakeWs(closed=True), "session_id": "dead"})(),
    }
    app = web.Application()
    app["server_sessions"] = sessions

    await app_mod._close_all_websockets(app)
    assert closed_count == 1
