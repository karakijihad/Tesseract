"""Best-effort Playwright browser provisioning.

Each playwright package version pins an exact browser revision; upgrading
the package orphans the binaries on disk and ``chromium.launch()`` fails
(2026-07-16: browser_navigate broke this way after an upgrade). The
``playwright install`` CLI is idempotent — a fast no-op when the pinned
revision is already present — so the Mirror fires this at boot as a
warmup task and the browser tools self-heal after upgrades.
"""

from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)


async def ensure_browsers() -> bool:
    """Run ``python -m playwright install chromium`` for the running venv.

    Returns True when the binaries are present (already, or after a
    download). Never raises: provisioning is best-effort and must not
    affect boot — a launch failure inside BrowserManager stays the
    operator-visible signal. No timeout: a long first download is
    legitimate, and Mirror shutdown cancels the task via the warmup
    drain (the subprocess is killed on cancellation).
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            log.info("browser provision: chromium binaries ready")
            return True
        log.warning(
            "browser provision: install exited %s — %s",
            proc.returncode,
            (out or b"").decode(errors="replace")[-500:].strip(),
        )
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        raise
    except Exception:  # noqa: BLE001 — boot must never fail on provisioning
        log.exception("browser provision: failed — browser tools may be unavailable")
    return False


__all__ = ["ensure_browsers"]
