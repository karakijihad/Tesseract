"""The dependencies that are not files with digests.

A venv, a browser engine, a set of CUDA wheels and a daemon cannot be judged
by comparing a recorded sha256, so each gets the strongest cheap probe
available — and the rule from `gpu_packages_ready` governs all of them:

> **"Present" is not "correct", and a probe that loads beats a probe that
> lists.**

That is not a preference. `onnxruntime.get_available_providers()` reports
`CUDAExecutionProvider` from the provider DLL merely existing on disk, and
keeps reporting it when that DLL cannot load one of its dependencies — so an
install with the CUDA 13 build beside CUDA 12 wheels passes every metadata
check and runs entirely on the processor. That install shipped, on this
machine, for weeks.

Nothing here installs, starts or repairs anything. `ensure_ollama` already
owns that, and a reconciler that started a daemon while deciding whether one
was running could never report the truth about a machine.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from tesseract.capability.state import (
    Consent,
    ConsentOrigin,
    DependencyRecord,
    DependencyState,
)

logger = logging.getLogger(__name__)

#: Where Playwright keeps browsers when nothing overrides it. Provisioning
#: runs `python -m playwright install chromium` without setting
#: `PLAYWRIGHT_BROWSERS_PATH`, so this default is where the bytes actually
#: land — checking `runtime/` instead would report every healthy install as
#: missing its browser.
_BROWSERS_ENV = "PLAYWRIGHT_BROWSERS_PATH"


def _record(
    dep_id: str,
    kind: str,
    state: DependencyState,
    reason: str = "",
    size_mb: int | None = None,
    *,
    wanted: bool = True,
    version: str = "",
) -> DependencyRecord:
    """One verdict.

    `wanted=False` means this machine's configuration does not ask for it — a
    GPU-less box and its CUDA wheels, an install with nothing wired to Ollama.
    Consent then stays `never_asked` rather than becoming `declined`, because
    nobody was asked; the difference is what stops a not-applicable dependency
    being reported as a problem or repaired as one.
    """
    return DependencyRecord(
        id=dep_id,
        kind=kind,
        state=state,
        consent=Consent.GRANTED if wanted else Consent.NEVER_ASKED,
        consent_origin=ConsentOrigin.CONFIG if wanted else ConsentOrigin.UNASKED,
        reason=reason,
        size_mb=size_mb,
        version=version,
    )


def ollama_version(exe: str) -> str:
    """What `ollama --version` reports, or empty if it would not say.

    Recorded because it is one of exactly two things in this runtime with a
    real upstream version. `_INSTALLER_URL` is unversioned by design — the
    comment in `ensure_ollama` argues the case and is right, since pinning it
    would go stale on precisely the machines that need it most — and the
    consequence is that nothing has ever recorded which Ollama is installed.

    Blocking (it spawns a process), so callers keep it off the event loop.
    Never raises: an unanswerable probe records nothing rather than costing
    the daemon's verdict.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("capability: could not read the Ollama version (%s)", exc)
        return ""
    if proc.returncode != 0:
        return ""

    # Both streams, and every line — not the last word of the first line.
    # Measured against a machine whose daemon was stopped: `ollama --version`
    # prints "Warning: could not connect to a running Ollama instance" FIRST
    # and the version SECOND, so taking the head of the output recorded the
    # word "instance" as the installed version.
    import re

    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    for line in text.splitlines():
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?)\b", line)
        if match:
            return match.group(1)
    return ""


def check_venv() -> DependencyRecord:
    """Whether the interpreter the supervisor launches actually exists.

    Deliberately not "can it import everything" — that is a multi-second
    import of the whole runtime, on the launch path, to answer a question the
    launch itself is about to answer by succeeding or failing. The interpreter
    existing is what `is_provisioned` checks and what the supervisor needs.
    """
    from tesseract.paths import is_installed_tree, runtime_dir

    if sys.platform == "win32":
        interpreter = runtime_dir() / "venv" / "Scripts" / "python.exe"
    else:
        interpreter = runtime_dir() / "venv" / "bin" / "python"

    if interpreter.is_file():
        return _record("venv", "runtime", DependencyState.OK)

    if not is_installed_tree():
        # A dev checkout has no provisioned venv and never will. Reporting it
        # absent would put a permanent false problem on every developer's
        # screen, which is how a report stops being read.
        return _record(
            "venv",
            "runtime",
            DependencyState.UNKNOWN,
            "running from a development checkout, which has no provisioned environment",
            wanted=False,
        )
    return _record(
        "venv",
        "runtime",
        DependencyState.ABSENT,
        "the Python environment is missing — the next launch will rebuild it",
    )


def _browsers_root() -> Path | None:
    override = os.environ.get(_BROWSERS_ENV, "").strip()
    if override:
        # `0` is Playwright's "install beside the package" sentinel, not a path.
        if override == "0":
            return None
        return Path(override)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "ms-playwright" if local else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def check_browser_engine() -> DependencyRecord:
    """Whether a Chromium build Playwright can drive is on disk.

    Directory-shaped rather than version-matched on purpose: the browser's
    build number is decided by the installed `playwright` package, which
    `uv pip install -e` already reconciles on every dependency change. Pinning
    it a second time here would give two owners to one fact.
    """
    root = _browsers_root()
    if root is None:
        return _record(
            "browser-engine",
            "runtime",
            DependencyState.UNKNOWN,
            "the browser location is overridden, so it cannot be checked here",
        )
    try:
        installed = any(
            entry.is_dir() and entry.name.startswith("chromium")
            for entry in root.iterdir()
        )
    except OSError:
        installed = False
    if installed:
        return _record("browser-engine", "runtime", DependencyState.OK)
    return _record(
        "browser-engine",
        "runtime",
        DependencyState.ABSENT,
        "reading web pages is unavailable until the browser engine is installed",
    )


def check_gpu_packages() -> DependencyRecord:
    """Whether the acceleration this machine's profile calls for actually
    loads.

    Three distinct answers, and collapsing any two of them is a defect:

    - the profile wants no extras (no card, or the engines are switched off) —
      nothing is missing, and there is nothing to offer
    - the profile wants extras and they load — `ok`
    - the profile wants extras and they do not load — `absent`, and this is
      the case that cost 75 seconds a spoken turn while every metadata check
      reported health
    """
    try:
        from tesseract.scripts.check_dependencies import _detect_gpu
        from tesseract.scripts.provision_hardware import (
            gpu_packages_ready,
            load_hardware_config,
            select_profile,
            wanted_extras,
        )

        cfg = load_hardware_config()
        profile = select_profile(cfg, _detect_gpu())
        extras = wanted_extras(profile, cfg)
    except Exception as exc:  # noqa: BLE001 — an unreadable profile is a state
        logger.warning("capability: could not resolve the hardware profile (%s)", exc)
        return _record(
            "gpu-acceleration",
            "packages",
            DependencyState.UNKNOWN,
            "this machine's hardware profile could not be read",
        )

    if not extras:
        return _record(
            "gpu-acceleration",
            "packages",
            DependencyState.ABSENT,
            "not applicable to this machine",
            wanted=False,
        )

    try:
        ready = gpu_packages_ready(extras)
    except Exception as exc:  # noqa: BLE001 — a probe that throws is a probe that failed
        logger.warning("capability: the GPU probe did not complete (%s)", exc)
        return _record(
            "gpu-acceleration",
            "packages",
            DependencyState.UNKNOWN,
            "the graphics acceleration check could not run",
        )

    if ready:
        return _record("gpu-acceleration", "packages", DependencyState.OK)
    return _record(
        "gpu-acceleration",
        "packages",
        DependencyState.ABSENT,
        "this machine has a compatible graphics card, but speech is running "
        "on the processor instead",
    )


def check_package_conflicts() -> DependencyRecord:
    """Whether a distribution the profile explicitly removes is back.

    `hardware.yaml::conflicts` exists because `onnxruntime` (the CPU build) and
    `onnxruntime-gpu` unpack into the SAME `onnxruntime/` directory while
    resolvers treat them as unrelated distributions — so both can be installed
    at once, and **whichever was written last decides whether the GPU is used
    at all.** `provision_hardware` prunes the loser after installing the
    extras.

    But the prune is not sticky, and that is what this checks. `ensure_packages`
    returns early — "GPU packages already resolve, nothing to install" — the
    moment `gpu_packages_ready` is True, which is BEFORE it would prune. So
    once the GPU path works, any later `uv pip install -e` that re-adds the CPU
    build transitively (piper-tts and kokoro-onnx both depend on it) leaves the
    machine one dependency reinstall away from silently dropping to the
    processor, and nothing will ever prune it again.

    This is the phase's own principle turned on the package set: the DLL
    loading is not proof the install is CORRECT, only that it works *today*.
    Reported, never auto-repaired — uninstalling a distribution underneath a
    running process is a different risk class from downloading a file, and it
    is the operator's call.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        from tesseract.scripts.check_dependencies import _detect_gpu
        from tesseract.scripts.provision_hardware import (
            load_hardware_config,
            select_profile,
            wanted_extras,
        )

        cfg = load_hardware_config()
        profile = select_profile(cfg, _detect_gpu())
        extras = wanted_extras(profile, cfg)
    except Exception as exc:  # noqa: BLE001
        logger.info("capability: could not check package conflicts (%s)", exc)
        return _record(
            "package-conflicts",
            "packages",
            DependencyState.UNKNOWN,
            "the hardware profile could not be read",
        )

    if not extras:
        return _record(
            "package-conflicts",
            "packages",
            DependencyState.OK,
            wanted=False,
        )

    conflicting = sorted(
        {name for extra in extras for name in cfg.conflicts.get(extra, [])}
    )
    present: list[str] = []
    for name in conflicting:
        try:
            version(name)
        except PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001 — an unreadable dist is not a conflict
            continue
        present.append(name)

    if not present:
        return _record("package-conflicts", "packages", DependencyState.OK)
    return _record(
        "package-conflicts",
        "packages",
        DependencyState.STALE,
        f"{', '.join(present)} is installed alongside the accelerated build "
        f"they replace — both unpack to the same place, so the next dependency "
        f"reinstall could silently move speech back onto the processor",
    )


async def check_ollama() -> list[DependencyRecord]:
    """The daemon and the models configured to run on it.

    Two records rather than one: the binary being absent and the embedding
    model being unpulled are different problems with different remedies, and
    a single row cannot say which.

    `TagFetch` is used rather than a bare list because "could not ask" and
    "asked, nothing there" must not collapse — that collapse told the operator
    for two days that an installed embedding model was missing.
    """
    from tesseract.memory.ollama_boot import fetch_tags, ollama_exe
    from tesseract.scripts.ensure_ollama import _configured_models

    configured = _configured_models()
    if configured is None:
        return [
            _record(
                "ollama",
                "service",
                DependencyState.UNKNOWN,
                "the provider catalog could not be read",
            )
        ]

    models, base_url = configured
    if not models:
        return [
            _record(
                "ollama",
                "service",
                DependencyState.ABSENT,
                "nothing in this configuration runs on it",
                wanted=False,
            )
        ]

    exe = ollama_exe()
    if exe is None:
        return [
            _record(
                "ollama",
                "service",
                DependencyState.ABSENT,
                "not installed — searching your memory and files falls back to "
                "matching words rather than meaning",
            )
        ]

    # Concurrently: the version spawns a process and the tag fetch is a
    # request, and neither needs the other's answer.
    version, fetched = await asyncio.gather(
        asyncio.to_thread(ollama_version, exe), fetch_tags(base_url)
    )

    if not fetched.ok:
        return [
            _record(
                "ollama",
                "service",
                DependencyState.UNKNOWN,
                f"installed, but it did not answer ({fetched.error or 'no reason given'})",
                version=version,
            )
        ]

    out = [_record("ollama", "service", DependencyState.OK, version=version)]
    # Ollama reports `name:tag`; config may name either form. Compared both
    # ways so a bare `nomic-embed-text` matches a pulled `nomic-embed-text:latest`.
    have = {tag for tag in fetched.tags}
    have |= {tag.split(":", 1)[0] for tag in fetched.tags}
    absent = [m for m in models if m not in have and m.split(":", 1)[0] not in have]
    if absent:
        out.append(
            _record(
                "ollama-models",
                "service",
                DependencyState.ABSENT,
                f"{', '.join(sorted(absent))} "
                f"{'has' if len(absent) == 1 else 'have'} not been downloaded",
            )
        )
    else:
        out.append(
            _record(
                "ollama-models",
                "service",
                DependencyState.OK,
                version=await _model_digests(base_url, models),
            )
        )
    return out


async def _model_digests(base_url: str, models: list[str]) -> str:
    """Short digests for the configured models, as `name@abcdef12`.

    The second of the two things worth a version. `nomic-embed-text:latest` is
    a MOVING tag: what it resolves to changes upstream, and nothing has ever
    recorded which one is actually here — so "the embedding model changed
    under me" was undetectable, on one machine or between two.

    A separate request from `fetch_tags`, deliberately. That function carries
    the `ok`-versus-empty distinction this phase depends on (P2's fix, after a
    collapsed timeout reported an installed model as missing for two days),
    and reimplementing it here to save one localhost round trip would put that
    lesson in two places.

    Best-effort in the strongest sense: it returns empty on any failure and
    never affects the verdict. A digest is a nice-to-know; whether the model
    is present is the answer, and `fetch_tags` already gave it.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 — a missing digest costs nothing
        logger.debug("capability: could not read Ollama model digests (%s)", exc)
        return ""

    wanted = {m for m in models} | {m.split(":", 1)[0] for m in models}
    seen: list[str] = []
    for entry in payload.get("models") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if name not in wanted and name.split(":", 1)[0] not in wanted:
            continue
        digest = str(entry.get("digest") or "").removeprefix("sha256:")
        if digest:
            seen.append(f"{name}@{digest[:8]}")
    return ", ".join(sorted(seen))


#: Every synchronous system probe. Each is blocking — `gpu_packages_ready`
#: loads DLLs — so the pass runs them off the event loop.
SYNC_CHECKS = (
    check_venv,
    check_browser_engine,
    check_gpu_packages,
    check_package_conflicts,
)
