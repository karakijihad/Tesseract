"""Ollama lifecycle supervisor for the Mirror Settings panel.

Wraps the existing readiness helper (`memory/ollama_boot.py`) with start/stop
controls reachable from the Settings UI. Tracks the Popen for processes we
spawn ourselves so a "stop" toggle is safe — if Ollama was started outside
Mirror (operator's terminal, system tray, etc.) we refuse to kill it and
surface a 409 to the UI rather than risk taking down another app's daemon.

State is held at module level (single supervisor per Mirror process) so the
startup path's auto-start and the operator's later UI clicks share a view
of "is Ollama ours or not."
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import httpx

from tesseract.brain.boot import ollama_up
from tesseract.memory.ollama_boot import _fetch_tags, _is_localhost, _wait_for_ollama

log = logging.getLogger(__name__)

# Keepalive pool for the high-frequency Ollama tag probe. LocalModels
# panel polls /api/system/ollama every 5s while open, which means a
# fresh TCP socket to localhost:11434 each call without a shared client
# — and TIME_WAIT on Windows holds those sockets for ~4 min apiece.
# Two persistent connections is more than enough for a single-process
# Mirror talking to a single local Ollama; cap higher and the OS won't
# thank us.
_OLLAMA_CLIENT_LIMITS = httpx.Limits(max_keepalive_connections=2, max_connections=4)


@dataclass
class OllamaStatus:
    running: bool
    base_url: str
    embedding_model: str
    tags: list[str]
    embedding_present: bool
    owned_by_mirror: bool


class OllamaSupervisor:
    """Singleton-ish supervisor. Tracks a single Popen if Mirror spawned it."""

    def __init__(self, base_url: str, embedding_model: str) -> None:
        self.base_url = base_url
        self.embedding_model = embedding_model
        self._proc: subprocess.Popen | None = None
        # Default 5s per-request timeout — defensive belt against a future
        # caller that uses the shared client without passing an explicit
        # `timeout=` per request. A hung Ollama (rare but possible during
        # a model load) cannot block this pool indefinitely.
        self._client = httpx.AsyncClient(
            limits=_OLLAMA_CLIENT_LIMITS,
            timeout=httpx.Timeout(5.0),
        )

    async def aclose(self) -> None:
        """Close the keepalive client. Called from Mirror shutdown so the
        TCP sockets to localhost:11434 unwind cleanly instead of being
        reaped by Python's GC at process exit."""
        try:
            await self._client.aclose()
        except Exception:
            log.exception("ollama supervisor aclose failed")

    async def status(self) -> OllamaStatus:
        running = await asyncio.to_thread(ollama_up, self.base_url, 2.0)
        tags: list[str] = []
        if running:
            tags = await _fetch_tags(self.base_url, client=self._client)
        owned = self._proc is not None and self._proc.poll() is None
        return OllamaStatus(
            running=running,
            base_url=self.base_url,
            embedding_model=self.embedding_model,
            tags=tags,
            embedding_present=_model_present(tags, self.embedding_model),
            owned_by_mirror=owned,
        )

    async def start(self) -> tuple[bool, str]:
        """Start Ollama if not already running. Returns (ok, message).

        No-op when already up. Localhost-only — refuses to spawn against
        a remote `base_url` because we can't reach across the network."""
        if await asyncio.to_thread(ollama_up, self.base_url, 2.0):
            return True, "already running"
        if not _is_localhost(self.base_url):
            return False, f"refuse to start: {self.base_url} is not localhost"
        exe = shutil.which("ollama")
        if exe is None:
            return False, "ollama binary not on PATH"

        def _spawn() -> subprocess.Popen | None:
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            else:
                kwargs["start_new_session"] = True
            try:
                return subprocess.Popen([exe, "serve"], **kwargs)  # noqa: S603
            except OSError as e:
                log.warning("ollama spawn failed: %s", e)
                return None

        proc = await asyncio.to_thread(_spawn)
        if proc is None:
            return False, "spawn failed"
        self._proc = proc
        alive = await _wait_for_ollama(self.base_url, total_s=15.0, poll_s=0.5)
        if not alive:
            return False, "spawned but did not become reachable within 15s"
        return True, "started"

    async def stop(self) -> tuple[bool, str]:
        """Stop the Mirror-spawned Ollama process. Refuses to kill an
        externally-started instance — the operator may have other apps
        relying on it. The UI surfaces this 409 so the user can stop it
        manually."""
        if self._proc is None or self._proc.poll() is not None:
            running = await asyncio.to_thread(ollama_up, self.base_url, 1.0)
            if not running:
                return True, "already stopped"
            return False, (
                "Ollama is running but was started outside Mirror. "
                "Stop it manually (`ollama stop` or task manager); "
                "Mirror will not kill processes it doesn't own."
            )
        proc = self._proc
        # Keep `self._proc` set until terminate completes — clearing it before
        # the kill races with `status()` (would wrongly read `owned=False`)
        # and orphans the Popen if `_terminate_proc` raises (a retry would
        # then refuse with the external-instance 409, locking the operator
        # out of the UI toggle).
        try:
            await asyncio.to_thread(_terminate_proc, proc)
        except Exception:
            log.exception("terminate ollama proc failed; leaving _proc set so a retry can try again")
            return False, "terminate failed"
        self._proc = None
        return True, "stopped"


def _terminate_proc(proc: subprocess.Popen, timeout_s: float = 5.0) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout_s)


def _model_present(tags: list[str], model: str) -> bool:
    if not tags:
        return False
    target = model.split(":", 1)[0]
    return any(tag == model or tag.split(":", 1)[0] == target for tag in tags)
