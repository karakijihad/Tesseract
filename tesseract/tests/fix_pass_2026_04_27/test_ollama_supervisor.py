"""Ollama supervisor lifecycle tests.

Mirror's Settings panel exposes a start/stop toggle for the local Ollama
daemon. The supervisor must (a) auto-start when invoked from Mirror's
boot path, (b) refuse to stop a daemon Mirror didn't spawn, (c) cleanly
terminate one it did spawn, and (d) be safe to call repeatedly when the
spawn path raises mid-stop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import tesseract.mirror.server.ollama_supervisor as sup_mod
from tesseract.mirror.server.ollama_supervisor import OllamaSupervisor


class _FakeProc:
    """Stand-in for `subprocess.Popen` so tests don't actually spawn anything."""

    def __init__(self, terminate_raises: bool = False, alive: bool = True) -> None:
        self._alive = alive
        self.terminated = False
        self.killed = False
        self.terminate_raises = terminate_raises

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        if self.terminate_raises:
            raise RuntimeError("simulated terminate failure")
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def patch_probe(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub `ollama_up` + `_wait_for_ollama` + `_fetch_tags` + `shutil.which`
    so supervisor logic can be exercised without a real daemon."""
    state: dict[str, Any] = {
        "running": False,
        "tags": ["nomic-embed-text:latest"],
        "which": "/usr/bin/ollama",
        "wait_returns": True,
    }

    def _fake_up(base_url: str, timeout: float = 2.0) -> bool:
        return state["running"]

    async def _fake_wait(base_url: str, total_s: float = 10.0, poll_s: float = 0.5) -> bool:
        return state["wait_returns"]

    async def _fake_tags(
        base_url: str, timeout_s: float = 5.0, *, client: Any = None,
    ) -> list[str]:
        return state["tags"] if state["running"] else []

    def _fake_which(name: str) -> str | None:
        return state["which"]

    monkeypatch.setattr(sup_mod, "ollama_up", _fake_up)
    monkeypatch.setattr(sup_mod, "_wait_for_ollama", _fake_wait)
    monkeypatch.setattr(sup_mod, "_fetch_tags", _fake_tags)
    monkeypatch.setattr(sup_mod.shutil, "which", _fake_which)
    return state


def test_start_noop_when_already_running(patch_probe: dict) -> None:
    """Auto-start path: if Ollama is already up (operator started it
    externally), supervisor returns success without spawning. Owned-flag
    stays False so a later UI stop refuses with 409."""
    patch_probe["running"] = True
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    ok, msg = asyncio.run(sup.start())
    assert ok is True
    assert "already" in msg
    assert sup._proc is None  # noqa: SLF001 — verifying internal state for owned flag


def test_start_spawns_when_not_running(
    patch_probe: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror auto-launch: operator boots Mirror with no Ollama running →
    supervisor spawns `ollama serve` and tracks the Popen so a later
    shutdown can terminate it."""
    fake_proc = _FakeProc()

    def _fake_popen(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return fake_proc

    monkeypatch.setattr(sup_mod.subprocess, "Popen", _fake_popen)

    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    ok, msg = asyncio.run(sup.start())
    assert ok is True
    assert msg == "started"
    assert sup._proc is fake_proc  # noqa: SLF001


def test_stop_refuses_external_instance(patch_probe: dict) -> None:
    """Operator started Ollama outside Mirror; UI clicks stop → 409.
    Mirror won't kill processes it doesn't own (other apps may be using
    the daemon)."""
    patch_probe["running"] = True
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    # _proc is None → not owned.
    ok, msg = asyncio.run(sup.stop())
    assert ok is False
    assert "outside Mirror" in msg


def test_stop_terminates_owned_process(patch_probe: dict) -> None:
    """Mirror-spawned daemon: clean stop terminates the Popen and clears
    the tracked handle so a subsequent `status()` reports owned=False."""
    fake_proc = _FakeProc()
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    sup._proc = fake_proc  # noqa: SLF001 — simulating prior start

    ok, msg = asyncio.run(sup.stop())
    assert ok is True
    assert msg == "stopped"
    assert fake_proc.terminated is True
    assert sup._proc is None  # noqa: SLF001


def test_stop_keeps_proc_set_when_terminate_raises(patch_probe: dict) -> None:
    """If terminate raises mid-stop, the supervisor must NOT clear `_proc`
    — otherwise a retry would see no owned proc, probe Ollama as still
    running, and refuse with the external-instance 409 (operator is
    locked out of the UI toggle for a process Mirror spawned)."""
    fake_proc = _FakeProc(terminate_raises=True)
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    sup._proc = fake_proc  # noqa: SLF001

    ok, msg = asyncio.run(sup.stop())
    assert ok is False
    assert msg == "terminate failed"
    assert sup._proc is fake_proc  # noqa: SLF001 — handle preserved for retry


def test_status_reports_owned_when_proc_alive(patch_probe: dict) -> None:
    """Status drives the UI toggle's hint text ('started by Mirror' vs
    'started outside Mirror'). Owned flag must reflect whether `_proc`
    is alive — not just whether Ollama is reachable."""
    patch_probe["running"] = True
    fake_proc = _FakeProc()
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    sup._proc = fake_proc  # noqa: SLF001

    s = asyncio.run(sup.status())
    assert s.running is True
    assert s.owned_by_mirror is True
    assert s.embedding_present is True


def test_status_owned_false_when_proc_dead(patch_probe: dict) -> None:
    """If the tracked Popen exited (Ollama crashed) but the daemon is
    somehow still reachable (operator restarted it externally), owned
    must read False — Mirror no longer controls the live process."""
    patch_probe["running"] = True
    fake_proc = _FakeProc(alive=False)
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    sup._proc = fake_proc  # noqa: SLF001

    s = asyncio.run(sup.status())
    assert s.running is True
    assert s.owned_by_mirror is False


def test_supervisor_passes_keepalive_client_to_fetch_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keepalive contract — supervisor owns a single long-lived
    httpx.AsyncClient and threads it into ``_fetch_tags`` so successive
    polls reuse the TCP keepalive pool instead of opening a fresh socket
    to localhost:11434 each time (TIME_WAIT exhaustion on Windows)."""
    import httpx

    seen: dict[str, Any] = {}

    async def _capture_tags(
        base_url: str, timeout_s: float = 5.0, *, client: Any = None,
    ) -> list[str]:
        seen["client"] = client
        return []

    def _fake_up(base_url: str, timeout: float = 2.0) -> bool:
        return True

    monkeypatch.setattr(sup_mod, "ollama_up", _fake_up)
    monkeypatch.setattr(sup_mod, "_fetch_tags", _capture_tags)

    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    assert isinstance(sup._client, httpx.AsyncClient)  # noqa: SLF001
    asyncio.run(sup.status())
    assert seen["client"] is sup._client  # noqa: SLF001


def test_supervisor_aclose_closes_keepalive_client() -> None:
    """aclose must shut down the keepalive pool so Mirror's shutdown
    doesn't leak open TCP sockets to Ollama."""
    sup = OllamaSupervisor("http://localhost:11434", "nomic-embed-text")
    asyncio.run(sup.aclose())
    assert sup._client.is_closed is True  # noqa: SLF001
