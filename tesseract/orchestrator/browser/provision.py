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


def _unanswered() -> bool:
    """True when nobody was asked and nobody has answered since.

    The switch below is not enough on its own, and that was a live hole. An
    install whose setup form never opened never runs `apply_first_run_setup`,
    so `services.browser.enabled` still holds the value the catalog SHIPS —
    `true` — and the shell's own guard cannot help, because the shell already
    finished and skipped its browser stage. This warm-up runs on every Mirror
    boot afterwards and would fetch the engine on the strength of a default
    nobody chose.

    So the ledger is consulted for exactly the case the marker describes: with
    setup deferred, only an answer a PERSON gave — the form, or a Settings
    toggle — authorises the download. Total: an unreadable ledger reads as
    unanswered, because the failure this guards costs ~700 MB.
    """
    from tesseract.capability.consent import read_ledger, setup_deferred
    from tesseract.capability.state import AUTHORITATIVE_ORIGINS, Consent

    if not setup_deferred():
        return False
    try:
        answer = read_ledger().answers.get("browser-engine")
    except Exception:  # noqa: BLE001 — a ledger that will not load has not answered
        return True
    return not (
        answer is not None
        and answer.consent is Consent.GRANTED
        and answer.origin in AUTHORITATIVE_ORIGINS
    )


async def ensure_browsers_if_wanted() -> bool:
    """`ensure_browsers`, unless the engine is switched off or unauthorised.

    One place decides, and both callers use it: the provisioning stage (via
    ``__main__`` below) and the Mirror's boot warm-up. The switch lives in
    ``providers.yaml::services.browser`` because it is read at call time —
    turning the engine back on in Settings takes effect on the next launch
    without a restart.

    Two gates, and they answer different questions. The switch says whether the
    operator wants it; `_unanswered` says whether anyone was ever asked.
    """
    from tesseract.kernel.tools.web_providers.base import service_disabled_reason

    off = service_disabled_reason("browser")
    if off is not None:
        log.info("browser provision: skipped — %s", off)
        return False
    if _unanswered():
        log.info(
            "browser provision: skipped — setup never ran on this install, so "
            "the shipped default is not an answer. Turn the browser engine on "
            "in Settings and it downloads then."
        )
        return False
    return await ensure_browsers()


if __name__ == "__main__":  # pragma: no cover - provisioning entry point
    # Always exit 0. The shell treats every optional stage as best-effort, and
    # a declined engine is a successful outcome, not a failed download.
    asyncio.run(ensure_browsers_if_wanted())


__all__ = ["ensure_browsers", "ensure_browsers_if_wanted"]
