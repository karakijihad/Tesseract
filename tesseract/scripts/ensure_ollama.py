"""First-run Ollama bring-up for every model config points at.

`provision.rs` installs Python, dependencies, a Chromium build and the
voice model on first run. Nothing installed Ollama, so a fresh
install on someone else's machine came up with semantic search silently
off — `memory_search` fell back to BM25 and the only signal was a doctor
line the operator had to think to run.

`memory/ollama_boot.py` deliberately does neither of the two things a
fresh install needs: it never installs the binary, and it never auto-pulls
("gigabytes of download are not a silent side effect"). Both are the right
call at RUNTIME, mid-session. Neither is right at INSTALL time, where the
operator is already watching a progress bar and expecting downloads. This
module is that install-time half, and composes `ollama_boot`'s primitives
for everything else rather than restating them.

Which models are read from config, never hardcoded: `brain/boot.py::
ollama_refs` walks every slot — embeddings, reranker, each active role's
primary and fallbacks, the voice lanes — and keeps the ones served by local
Ollama. Today that resolves to the embedding model alone, and a fresh
install downloads exactly what it always did. Point a role at
`local.ollama.<llm>` and the pull follows without touching this file.

The installer URL is NOT config: this
script downloads and EXECUTES what that URL serves, so it stays source —
operator-reviewed like `fetch_kokoro_voice`'s pinned upstream — rather than
sitting in a YAML file where a write would become arbitrary code execution
during install.

What the downloaded file is gets checked before it runs: it must carry a
valid Authenticode signature from the publisher named in
`providers.yaml::local.ollama.installer_signer`. That check is the pin
`lib/pinned_fetch.py` provides everywhere else by sha256, which is not
available here — an unversioned URL means a new digest on every Ollama
release. The check fails closed: an unsigned file, a mis-signed one, a
missing config key, or a verification that could not run all refuse the
install rather than proceeding unverified.

Best-effort by construction: every failure path logs and returns. The
script always exits 0, so an offline or declined install still finishes
provisioning with embeddings simply unavailable — exactly the state a
fresh install has today.

Invoked by `provision.rs` as `python -m tesseract.scripts.ensure_ollama`
after the venv + editable install exist.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from tesseract.memory.ollama_boot import ollama_exe as _ollama_exe

logger = logging.getLogger(__name__)

# Official vendor installer. Unversioned by design — Ollama serves the
# current release here, and pinning a version would go stale on exactly
# the schedule that matters (a machine installing TESSERACT months from
# now should get a current Ollama, not a frozen one).
_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"

# `/VERYSILENT` is Inno Setup's no-UI switch; Ollama's installer is Inno.
# Without it the install blocks on a window nobody is watching, behind the
# app's own progress bar.
_SILENT_FLAGS = ("/VERYSILENT", "/NORESTART")

_DOWNLOAD_TIMEOUT_S = 300.0
_INSTALL_TIMEOUT_S = 600.0
_PULL_TIMEOUT_S = 1800.0   # per model, cold network — not a budget for all of them

# How much of the pull's output is taken per read, and how often the newest
# line reaches the log. `ollama pull` redraws its progress line continuously,
# so relaying every one would write hundreds of lines a second.
_PULL_READ_BYTES = 4096
_PULL_LOG_INTERVAL_S = 1.0


def _download_installer(dest: Path) -> bool:
    logger.info("downloading Ollama installer from %s", _INSTALLER_URL)
    try:
        with urllib.request.urlopen(
            _INSTALLER_URL, timeout=_DOWNLOAD_TIMEOUT_S
        ) as resp, dest.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Ollama installer download failed: %s", exc)
        return False
    if not dest.exists() or dest.stat().st_size == 0:
        logger.warning("Ollama installer download produced an empty file")
        return False
    return True


def _expected_signer() -> str:
    """Who may have signed the installer, from the providers catalog.

    Read at use rather than import so a corrected config takes effect on
    the next attempt. Any failure to read it returns blank, and blank is
    refused by `verify_signed_by` — an unreadable catalog must not become
    an unverified install.
    """
    try:
        import yaml

        from tesseract.paths import config_dir

        raw = yaml.safe_load(
            (config_dir() / "providers.yaml").read_text(encoding="utf-8")
        ) or {}
        block = (raw.get("local") or {}).get("ollama") or {}
        return str(block.get("installer_signer") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read the expected installer signer: %s", exc)
        return ""


def _verify_installer(installer: Path) -> bool:
    from tesseract.lib.authenticode import verify_signed_by

    verdict = verify_signed_by(installer, _expected_signer())
    if not verdict.trusted:
        logger.warning(
            "refusing to run the Ollama installer — %s (downloaded from %s)",
            verdict.reason,
            _INSTALLER_URL,
        )
        return False
    logger.info("Ollama installer signature verified (%s)", verdict.subject)
    return True


def _run_installer(installer: Path) -> bool:
    if not _verify_installer(installer):
        return False
    logger.info("running Ollama installer silently")
    try:
        proc = subprocess.run(
            [str(installer), *_SILENT_FLAGS],
            timeout=_INSTALL_TIMEOUT_S,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Ollama installer did not complete: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "Ollama installer exited %s: %s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:400],
        )
        return False
    return True


def _install_ollama() -> bool:
    if sys.platform != "win32":
        # The shipped app is Windows-only (Tauri + NSIS). On anything else
        # the operator installs Ollama themselves; say so rather than
        # guessing at a package manager.
        logger.info(
            "not Windows — install Ollama via your platform's instructions "
            "at https://ollama.com/download"
        )
        return False
    with tempfile.TemporaryDirectory(prefix="tesseract-ollama-") as tmp:
        installer = Path(tmp) / "OllamaSetup.exe"
        if not _download_installer(installer):
            return False
        return _run_installer(installer)


def _pull_model(exe: str, model: str) -> bool:
    """Pull one model, relaying its progress instead of swallowing it.

    This is the step runtime deliberately refuses to take on its own — here it
    is expected, and the operator is watching. `ollama pull` redraws a single
    progress line with carriage returns, so its output is split on `\\r` as
    well as `\\n` and relayed at most once a second: unthrottled it would write
    hundreds of lines a second into `shell.log`, and captured (as it was) a
    multi-gigabyte pull looked identical to a hang.
    """
    logger.info("pulling model %s", model)
    try:
        proc = subprocess.Popen(  # noqa: S603 — exe comes from `ollama_exe()`
            [exe, "pull", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        logger.warning("`ollama pull %s` did not start: %s", model, exc)
        return False

    last = _relay_pull_output(proc, model, _PULL_TIMEOUT_S)
    if last is None:
        logger.warning(
            "`ollama pull %s` exceeded %.0fs and was stopped", model, _PULL_TIMEOUT_S
        )
        return False
    if proc.returncode != 0:
        logger.warning("`ollama pull %s` exited %s: %s", model, proc.returncode, last)
        return False
    return True


def _relay_pull_output(
    proc: subprocess.Popen, model: str, timeout: float
) -> str | None:
    """Drain the pull's output to the log, returning its last line.

    None means the timeout fired and the process was killed — the caller
    reports that as its own outcome rather than as a non-zero exit, because
    the two mean different things to someone reading the log.

    The timeout is a watchdog thread rather than a deadline checked between
    reads: a read on a stalled pipe blocks indefinitely, so a loop that only
    checks the clock after each read cannot enforce anything. Killing the
    process is what closes the pipe and ends the drain.

    `read1` and not `read`: `read(n)` on a buffered pipe blocks until it has
    all `n` bytes, which would hold a redrawing progress line back until
    enough of them accumulated to fill the buffer.
    """
    timed_out = threading.Event()

    def _stop() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _stop)
    watchdog.start()

    pending = bytearray()
    last_line = ""
    last_logged = 0.0
    stream = proc.stdout
    assert stream is not None  # stdout=PIPE above
    try:
        while True:
            block = stream.read1(_PULL_READ_BYTES)
            if not block:
                break
            for byte in block:
                if byte in (0x0A, 0x0D):
                    line = pending.decode("utf-8", "replace").strip()
                    pending.clear()
                    if not line:
                        continue
                    last_line = line
                    now = time.monotonic()
                    if now - last_logged >= _PULL_LOG_INTERVAL_S:
                        last_logged = now
                        logger.info("ollama pull %s: %s", model, line)
                else:
                    pending.append(byte)
        tail = pending.decode("utf-8", "replace").strip()
        if tail:
            last_line = tail
            logger.info("ollama pull %s: %s", model, tail)
        # Reaped INSIDE the guarded region, not after it. EOF on stdout is not
        # the same event as the process exiting — a pull that closes its
        # output while still running would otherwise leave this blocking with
        # the deadline already cancelled, which is the unbounded wait the
        # `subprocess.run(timeout=)` this replaced did not have.
        proc.wait()
    finally:
        watchdog.cancel()
    return None if timed_out.is_set() else last_line


def _configured_models() -> tuple[list[str], str] | None:
    """The Ollama-served models config points at, plus the endpoint.

    ``None`` when the catalog cannot be read. An empty model list means no
    slot is wired to Ollama at all — a clean skip, not a failure.
    """
    try:
        from tesseract.brain.boot import ollama_refs

        refs = ollama_refs()
    except Exception as exc:  # noqa: BLE001 — config broken: nothing to do
        logger.warning("cannot read the provider catalog: %s", exc)
        return None

    if not refs:
        return [], ""

    # Every ref shares one provider block, so the endpoint is whichever the
    # first one carries.
    base_url = refs[0].connection.base_url
    if not base_url:
        logger.warning("local.ollama has no base_url in providers.yaml")
        return None

    models: list[str] = []
    for ref in refs:
        if ref.model.model not in models:
            models.append(ref.model.model)
    return models, base_url


def ensure_ollama(*, allow_install: bool = True) -> bool:
    """Install Ollama if absent, start it, and pull every model config points
    at. Returns whether all of them ended up available. Never raises.

    ``allow_install=False`` skips the vendor-installer download and gives up
    when the binary is absent. That is the every-launch retry's mode: the two
    later steps (start a stopped service, pull a model whose first attempt was
    interrupted) are cheap and worth repeating, while re-downloading hundreds
    of megabytes of installer on every launch — for a machine where the
    install was declined or blocked — is not."""
    configured = _configured_models()
    if configured is None:
        return False
    models, base_url = configured
    if not models:
        logger.info("no config slot points at Ollama — skipping setup")
        return True

    from tesseract.memory.ollama_boot import (
        _model_present,
        _spawn_ollama_serve_sync,
        _wait_for_ollama,
        fetch_tags,
    )

    exe = _ollama_exe()
    if exe is None:
        if not allow_install:
            logger.info(
                "Ollama not installed — skipping install on this retry pass "
                "(run `python -m tesseract.scripts.ensure_ollama` to install it)"
            )
            return False
        if not _install_ollama():
            logger.warning(
                "Ollama unavailable — %s will not be served until it is "
                "installed; memory writes and keyword search are unaffected",
                ", ".join(models),
            )
            return False
        exe = _ollama_exe()
        if exe is None:
            logger.warning("Ollama installed but binary still not found")
            return False

    import asyncio

    async def _bring_up() -> list[str] | None:
        # The installer registers a service on most machines; spawn only if
        # the endpoint is not already answering.
        if not await _wait_for_ollama(base_url, total_s=3.0, poll_s=0.5):
            _spawn_ollama_serve_sync()
            if not await _wait_for_ollama(base_url, total_s=30.0, poll_s=1.0):
                logger.warning("Ollama did not become reachable at %s", base_url)
                return None
        fetched = await fetch_tags(base_url)
        if not fetched.ok:
            # Bail rather than treat an unreadable list as an empty one: every
            # configured model would look absent and this would re-pull
            # gigabytes that are already on disk.
            logger.warning(
                "Ollama is up at %s but its model list could not be read (%s) — "
                "not pulling, since what is already present is unknown",
                base_url, fetched.error,
            )
            return None
        return list(fetched.tags)

    try:
        tags = asyncio.run(_bring_up())
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Ollama bring-up failed: %s", exc)
        return False
    if tags is None:
        return False

    # One failed pull must not strand the models behind it — a missing LLM
    # should never be the reason retrieval has no embeddings.
    ok = True
    for model in models:
        if _model_present(tags, model):
            logger.info("Ollama ready; %s already present", model)
        elif _pull_model(exe, model):
            logger.info("Ollama ready; %s pulled", model)
        else:
            ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bring up Ollama + every model config points at")
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="never download the Ollama installer; only start it and pull the model",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        ensure_ollama(allow_install=not args.no_install)
    except Exception:  # noqa: BLE001
        logger.warning("Ollama setup failed", exc_info=True)
    # Always 0: embeddings are optional and must never fail provisioning.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
