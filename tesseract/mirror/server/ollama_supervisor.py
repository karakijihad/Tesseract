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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tesseract.brain.boot import ollama_up
from tesseract.memory.ollama_boot import (
    _fetch_tags,
    _is_localhost,
    _wait_for_ollama,
)
from tesseract.memory.ollama_boot import ollama_exe as _ollama_exe
from tesseract.scripts.ensure_ollama import ensure_ollama

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
    # Whether the binary exists at all. Without it the panel cannot tell
    # "stopped" from "never installed", and those need different offers: one
    # is a start toggle, the other is a download the operator must choose.
    binary_present: bool
    # An install is a long download, so it runs detached and reports here
    # rather than through the request that started it.
    installing: bool = False
    install_error: str | None = None


class OllamaSupervisor:
    """Singleton-ish supervisor. Tracks a single Popen if Mirror spawned it."""

    def __init__(self, base_url: str, embedding_model: str) -> None:
        self.base_url = base_url
        self.embedding_model = embedding_model
        self._proc: subprocess.Popen | None = None
        self._install_task: asyncio.Task | None = None
        self._install_error: str | None = None
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
        # A detached install would otherwise outlive the app as a pending task
        # and log a "task was destroyed" warning on the way down. Cancelling
        # only drops OUR wait — `ensure_ollama` runs in a thread and the vendor
        # installer is its own process, so neither is interrupted mid-write.
        if self._install_task is not None and not self._install_task.done():
            self._install_task.cancel()
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
            # No tracked model means Ollama is not the one serving embeddings,
            # so there is nothing here to be missing. Reporting False would
            # light the panel's "embedding model missing" warning over a
            # model that lives somewhere else entirely.
            embedding_present=(
                _model_present(tags, self.embedding_model)
                if self.embedding_model
                else True
            ),
            owned_by_mirror=owned,
            binary_present=await asyncio.to_thread(_ollama_exe) is not None,
            installing=self._installing(),
            install_error=self._install_error,
        )

    def _installing(self) -> bool:
        return self._install_task is not None and not self._install_task.done()

    async def install(self) -> tuple[bool, str]:
        """Start installing Ollama + the configured embedding model.

        Returns as soon as the work is *scheduled*, not when it finishes.
        `ensure_ollama`'s own budgets run to ~45 minutes for a single model
        (300s download + 600s install + 1800s pull) and the pull budget is
        per model, so a catalog wiring several costs more again. Awaiting
        that inside the request would hold
        the HTTP connection open for all of it — any client or proxy timeout
        would then report a failure while the install ran happily to
        completion behind it. The panel already polls status every 30s, so
        `installing` / `install_error` are where the outcome belongs.

        The one path the per-launch retry deliberately will not take. That
        retry runs `ensure_ollama --no-install` because re-downloading a
        vendor installer on every launch — on a machine where the install was
        blocked by UAC, antivirus or the operator declining — costs hundreds of
        megabytes to fail the same way each time. So the recovery is an
        operator-initiated act instead, which is also what the runtime already
        says about this class of work: `ollama_boot` refuses to auto-pull
        because "gigabytes of download are not a silent side effect".

        """
        if not _is_localhost(self.base_url):
            return False, f"refuse to install: {self.base_url} is not localhost"
        if self._installing():
            return True, "install already in progress"
        self._install_error = None
        self._install_task = asyncio.create_task(self._run_install())
        return True, "installing — downloading Ollama and the embedding model"

    async def _run_install(self) -> None:
        try:
            ok = await asyncio.to_thread(ensure_ollama, allow_install=True)
        except Exception as exc:  # noqa: BLE001 — surfaced via status, never raised into a task
            log.exception("ollama install failed")
            self._install_error = str(exc)
            return
        self._install_error = None if ok else (
            "install did not complete — the vendor installer may have been "
            "blocked or declined. See the backend log for the failing step."
        )

    async def start(self) -> tuple[bool, str]:
        """Start Ollama if not already running. Returns (ok, message).

        No-op when already up. Localhost-only — refuses to spawn against
        a remote `base_url` because we can't reach across the network."""
        if await asyncio.to_thread(ollama_up, self.base_url, 2.0):
            return True, "already running"
        if not _is_localhost(self.base_url):
            return False, f"refuse to start: {self.base_url} is not localhost"
        # `_ollama_exe`, not a bare `shutil.which`: a just-completed install
        # does not update THIS process's PATH, so the start immediately after
        # one would have reported "not on PATH" against a binary sitting in
        # the default per-user install dir.
        exe = _ollama_exe()
        if exe is None:
            return False, "ollama is not installed"

        def _spawn() -> subprocess.Popen | None:
            kwargs: dict[str, Any] = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
                # Same contract as ollama_boot._spawn_ollama_serve_sync: a
                # detached child that outlives the app must never inherit
                # our CWD — on Windows it would lock that folder against
                # deletion, and ours is the replaceable app tree
                # (2026-07-29 reinstall wedge).
                "cwd": str(Path(exe).parent),
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
