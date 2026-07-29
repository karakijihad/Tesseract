"""Ollama readiness helper for Mirror startup.

Probes the configured Ollama endpoint; on a localhost endpoint that's down,
optionally spawns `ollama serve` detached and waits for it. Verifies the
embedding model is present in `/api/tags` — never auto-pulls (gigabytes of
download are not a silent side effect).

Fail-open contract: if Ollama is unreachable after all attempts, log WARN and
return False. Callers continue without warm embeddings.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

from tesseract.brain.boot import ollama_up

log = logging.getLogger(__name__)


async def _probe(base_url: str, timeout_s: float = 2.0) -> bool:
    return await asyncio.to_thread(ollama_up, base_url, timeout_s)


async def _fetch_tags(
    base_url: str,
    timeout_s: float = 5.0,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Fetch the Ollama tag list. Accepts an optional shared client.

    When ``client`` is provided, the caller owns the connection lifecycle
    — the function reuses the keep-alive pool and stops creating fresh
    TCP sockets per probe. With Mirror polling Ollama every 5s through
    ``OllamaSupervisor.status``, that previously accrued hundreds of
    ``TIME_WAIT`` sockets to ``localhost:11434`` on Windows. Without a
    shared client the function falls back to its old per-call behaviour
    (test callers / one-off probes).
    """
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        if client is not None:
            r = await client.get(url, timeout=timeout_s)
            r.raise_for_status()
            data = r.json()
        else:
            async with httpx.AsyncClient(timeout=timeout_s) as c:
                r = await c.get(url)
                r.raise_for_status()
                data = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    models = data.get("models") or []
    return [m.get("name", "") for m in models if isinstance(m, dict)]


def _is_localhost(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


def _spawn_ollama_serve_sync() -> bool:
    exe = shutil.which("ollama")
    if exe is None:
        log.warning("ollama binary not on PATH — cannot auto-start")
        return False
    try:
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            # Never inherit our CWD: this child is detached and outlives the
            # app by design, and on Windows a process's working directory
            # LOCKS that folder against deletion. Inheriting the backend's
            # CWD (inside the installed app tree) left a detached ollama
            # holding <TESSERACT_HOME>/app, which wedged every reinstall on
            # 2026-07-29. Its own install dir is stable and never ours to
            # delete.
            "cwd": str(Path(exe).parent),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, "serve"], **kwargs)
        log.info("spawned `ollama serve` detached")
        return True
    except OSError as e:
        log.warning("failed to spawn ollama serve: %s", e)
        return False


async def _spawn_ollama_serve() -> bool:
    return await asyncio.to_thread(_spawn_ollama_serve_sync)


async def _wait_for_ollama(base_url: str, total_s: float = 10.0, poll_s: float = 0.5) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_s
    while loop.time() < deadline:
        if await _probe(base_url, timeout_s=1.0):
            return True
        await asyncio.sleep(poll_s)
    return False


def _model_present(tags: list[str], model: str) -> bool:
    if not tags:
        return False
    target = model.split(":", 1)[0]
    return any(tag == model or tag.split(":", 1)[0] == target for tag in tags)


async def ensure_ollama_ready(base_url: str, model: str, auto_start: bool = True) -> bool:
    """Probe Ollama, optionally auto-start on localhost, verify model present.

    Returns True when Ollama is reachable and `model` is listed in /api/tags.
    Never raises. Never auto-pulls missing models.
    """
    alive = await _probe(base_url, timeout_s=2.0)
    if not alive:
        if auto_start and _is_localhost(base_url):
            if await _spawn_ollama_serve():
                alive = await _wait_for_ollama(base_url, total_s=10.0, poll_s=0.5)
        if not alive:
            log.warning(
                "Ollama not reachable at %s — dedupe + retrieval will fail open",
                base_url,
            )
            return False

    tags = await _fetch_tags(base_url)
    if not _model_present(tags, model):
        log.warning(
            "embedding model %s not in Ollama tags — run `ollama pull %s`",
            model, model,
        )
        return False
    return True
