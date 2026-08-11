"""Ollama primitives shared by everything that talks to the local daemon.

Resolving the binary, probing the endpoint, spawning `ollama serve`
detached, listing tags, and asking whether a model is among them. Mirror
startup (`mirror/server/app.py`), the Settings supervisor
(`mirror/server/ollama_supervisor.py`) and the installer script
(`scripts/ensure_ollama.py`) each compose these differently — the policy
of what to do about a missing daemon lives with them, not here.

Nothing in this module pulls a model. That is deliberate at runtime:
gigabytes of download are not a silent side effect. `ensure_ollama` is the
install-time counterpart, where the operator is watching a progress bar and
expecting exactly that.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import httpx

from tesseract.brain.boot import ollama_up

log = logging.getLogger(__name__)


def ollama_exe() -> str | None:
    """Resolve the ollama binary, including the default per-user install dir.

    A freshly-run installer does not update THIS process's PATH, so a bare
    `shutil.which` immediately after one reports "not installed" against a
    binary sitting in `%LOCALAPPDATA%\\Programs\\Ollama`. Every caller that
    can run after an in-process install — the installer script, the Settings
    supervisor, the adapter builder — needs this form rather than `which`.
    """
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.exists():
                return str(candidate)
    return None


async def _probe(base_url: str, timeout_s: float = 2.0) -> bool:
    return await asyncio.to_thread(ollama_up, base_url, timeout_s)


@dataclass(frozen=True)
class TagFetch:
    """Outcome of one ``/api/tags`` request — asked-and-answered, or not asked.

    The distinction is the whole point of the type. ``ok=False`` means the
    daemon could not be reached or did not answer usefully, so nothing is
    known about which models are pulled; the remedy is to look at the
    daemon. ``ok=True`` with empty ``tags`` means it answered and genuinely
    has nothing; the remedy is to pull. Returning ``[]`` for both is what
    told the operator for two days that `nomic-embed-text` was missing while
    it was installed the entire time — under loop starvation the 5 s timeout
    was routine, and every consumer read the timeout as an empty daemon.
    """

    ok: bool
    tags: tuple[str, ...] = ()
    error: str = ""


async def fetch_tags(
    base_url: str,
    timeout_s: float = 5.0,
    *,
    client: httpx.AsyncClient | None = None,
) -> TagFetch:
    """Fetch the Ollama tag list. Accepts an optional shared client.

    When ``client`` is provided, the caller owns the connection lifecycle
    — the function reuses the keep-alive pool and stops creating fresh
    TCP sockets per probe. With Mirror polling Ollama every 5s through
    ``OllamaSupervisor.status``, that previously accrued hundreds of
    ``TIME_WAIT`` sockets to ``localhost:11434`` on Windows. Without a
    shared client the function falls back to its old per-call behaviour
    (test callers / one-off probes).

    Never raises: the failure rides in the returned :class:`TagFetch`.
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
    except httpx.TimeoutException:
        return TagFetch(ok=False, error=f"no response within {timeout_s:g}s")
    except httpx.HTTPError as exc:
        return TagFetch(ok=False, error=f"{type(exc).__name__}: {exc}")
    except ValueError as exc:
        return TagFetch(ok=False, error=f"unreadable response: {exc}")
    # Valid JSON of the wrong shape is still a daemon we could not read. A
    # bare `data.get(...)` here raises AttributeError on a list or a string
    # body — outside the try, so it escapes the "never raises" contract above
    # and 500s the Settings endpoint this function exists to make honest.
    if not isinstance(data, dict):
        return TagFetch(
            ok=False,
            error=f"unexpected response shape: {type(data).__name__}",
        )
    models = data.get("models")
    if models is None:
        # The daemon answered and listed nothing. Genuinely empty, not a
        # failure — this is the state that earns a "pull it" remedy.
        return TagFetch(ok=True)
    if not isinstance(models, list):
        return TagFetch(
            ok=False,
            error=f"unexpected 'models' shape: {type(models).__name__}",
        )
    # Names are validated as strings, not merely present: `_model_present`
    # calls `tag.split(":", 1)`, so a numeric name would raise there instead —
    # inside `OllamaSupervisor.status`, which the Settings route calls with no
    # handler, turning a malformed payload into a 500 on the endpoint this
    # function exists to keep honest.
    tags = tuple(
        m["name"] for m in models
        if isinstance(m, dict) and isinstance(m.get("name"), str)
    )
    return TagFetch(ok=True, tags=tags)


def _is_localhost(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""}


def _spawn_ollama_serve_sync() -> bool:
    # `ollama_exe`, not `shutil.which`: the install-time caller spawns this
    # immediately after running the vendor installer, and that install has
    # not touched this process's PATH.
    exe = ollama_exe()
    if exe is None:
        log.warning("ollama binary not found — cannot auto-start")
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


async def _wait_for_ollama(base_url: str, total_s: float = 10.0, poll_s: float = 0.5) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_s
    while loop.time() < deadline:
        if await _probe(base_url, timeout_s=1.0):
            return True
        await asyncio.sleep(poll_s)
    return False


def _model_present(tags: Sequence[str], model: str) -> bool:
    """Whether `model` appears in a tag list that was actually fetched.

    Callers must check `TagFetch.ok` first: an empty `tags` here means the
    daemon reported nothing, and this returns False for every model — which
    is only the truth when the fetch succeeded.
    """
    if not tags:
        return False
    target = model.split(":", 1)[0]
    return any(tag == model or tag.split(":", 1)[0] == target for tag in tags)
