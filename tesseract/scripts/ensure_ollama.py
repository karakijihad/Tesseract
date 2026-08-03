"""First-run Ollama bring-up for the shipped embedding model.

`provision.rs` installs Python, dependencies, a Chromium build and the
Piper voice model on first run. Nothing installed Ollama, so a fresh
install on someone else's machine came up with semantic search silently
off — `memory_search` fell back to BM25 and the only signal was a doctor
line the operator had to think to run.

`memory/ollama_boot.py::ensure_ollama_ready` deliberately does neither of
the two things a fresh install needs: it gives up when the binary is not
on PATH, and it never auto-pulls ("gigabytes of download are not a silent
side effect"). Both are the right call at RUNTIME, mid-session. Neither is
right at INSTALL time, where the operator is already watching a progress
bar and expecting downloads. This module is that install-time half, and
reuses `ollama_boot` for everything else rather than restating it.

The model is read from config (`roles.yaml::embeddings.primary` → the
providers entry), never hardcoded. The installer URL is NOT config: this
script downloads and EXECUTES what that URL serves, so it stays source —
operator-reviewed like `fetch_piper_voice`'s pinned upstream — rather than
sitting in a YAML file where a write would become arbitrary code execution
during install.

Best-effort by construction: every failure path logs and returns. The
script always exits 0, so an offline or declined install still finishes
provisioning with embeddings simply unavailable — exactly the state a
fresh install has today.

Invoked by `provision.rs` as `python -m tesseract.scripts.ensure_ollama`
after the venv + editable install exist.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

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
_PULL_TIMEOUT_S = 1800.0   # first pull of an embedding model, cold network


def _ollama_exe() -> str | None:
    """Resolve the ollama binary, including the default per-user install
    dir — a freshly-run installer does not update THIS process's PATH."""
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "win32":
        import os

        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.exists():
                return str(candidate)
    return None


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


def _run_installer(installer: Path) -> bool:
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
    """Pull the embedding model. This is the step runtime deliberately
    refuses to take on its own — here it is expected, and the operator is
    watching a progress bar that says so."""
    logger.info("pulling embedding model %s", model)
    try:
        proc = subprocess.run(
            [exe, "pull", model],
            timeout=_PULL_TIMEOUT_S,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("`ollama pull %s` did not complete: %s", model, exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "`ollama pull %s` exited %s: %s",
            model,
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:400],
        )
        return False
    return True


def ensure_ollama() -> bool:
    """Install Ollama if absent, start it, and pull the configured
    embedding model. Returns whether embeddings ended up available.
    Never raises."""
    try:
        from tesseract.brain.boot import load_embeddings_cfg
    except Exception as exc:  # noqa: BLE001 — config broken: nothing to do
        logger.warning("cannot read embeddings config: %s", exc)
        return False

    cfg = load_embeddings_cfg()
    if not cfg:
        logger.info("embeddings disabled in config — skipping Ollama setup")
        return True

    model = cfg["model"]
    base_url = cfg["base_url"]

    from tesseract.memory.ollama_boot import (
        _fetch_tags,
        _model_present,
        _spawn_ollama_serve_sync,
        _wait_for_ollama,
    )

    exe = _ollama_exe()
    if exe is None:
        if not _install_ollama():
            logger.warning(
                "Ollama unavailable — semantic search will be off until it is "
                "installed; memory writes and keyword search are unaffected"
            )
            return False
        exe = _ollama_exe()
        if exe is None:
            logger.warning("Ollama installed but binary still not found")
            return False

    import asyncio

    async def _bring_up() -> bool:
        # The installer registers a service on most machines; spawn only if
        # the endpoint is not already answering.
        if not await _wait_for_ollama(base_url, total_s=3.0, poll_s=0.5):
            _spawn_ollama_serve_sync()
            if not await _wait_for_ollama(base_url, total_s=30.0, poll_s=1.0):
                logger.warning("Ollama did not become reachable at %s", base_url)
                return False
        tags = await _fetch_tags(base_url)
        return _model_present(tags, model)

    try:
        already = asyncio.run(_bring_up())
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Ollama bring-up failed: %s", exc)
        return False

    if already:
        logger.info("Ollama ready; %s already present", model)
        return True

    if not _pull_model(exe, model):
        return False

    logger.info("Ollama ready; %s pulled", model)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        ensure_ollama()
    except Exception:  # noqa: BLE001
        logger.warning("Ollama setup failed", exc_info=True)
    # Always 0: embeddings are optional and must never fail provisioning.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
