"""Profile this machine, then give it the configuration it can actually run.

Runs during provisioning, after `apply_first_run_setup` has seeded
`home/config/` and before the model fetchers read it. Two jobs:

- **Once:** pick the speech model this machine should use and write it into
  `providers.yaml`, so the fetch stage downloads that one and not a fixed
  default. Recorded in `runtime/hardware-profile.json` and never redone —
  an operator who changes the model in Settings afterwards keeps it.
- **Every launch:** make sure the GPU packages the chosen profile calls for
  are present. Idempotent and cheap: when they already resolve, it exits
  without invoking the installer at all, which is what makes it safe to
  retry on a machine that was offline during first run.

Never fails provisioning. A machine that could not be profiled, or whose
wheels would not install, keeps the CPU path — slower, and working.

Usage: python -m tesseract.scripts.provision_hardware [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_LABEL = "hardware profile"

# The shell owns the `uv` binary (it ships as a Tauri resource, outside the
# state root) and hands its path down. Without it we can still choose the
# model — the half that needs no installer — and say why the rest was
# skipped, rather than guessing a path that would only ever be wrong.
#
# Unverified on purpose, unlike the Ollama installer, whose Authenticode
# subject is pinned because it is fetched over the network from a vendor.
# This path is not fetched and not operator-supplied: it is a resource
# resolved inside the same signed shell binary that sets the variable and
# spawns this process. A check here would be that binary verifying its own
# resource against a value it also supplied, which establishes nothing that
# its own integrity does not already establish.
_UV_ENV = "TESSERACT_UV"

# This module is re-run on EVERY launch, and its install step is ~2.2 GB. A
# machine that cannot complete it — full disk, a wheel that 404s, a network
# that drops — would otherwise re-attempt the whole thing at every start,
# forever, with nothing on screen to explain the activity. The count is kept
# in the profile record and cleared by a success, so a transient failure
# costs a retry and a permanent one stops asking.
MAX_CONSECUTIVE_FAILURES = 3

# PEP 503 normalised-name shape. Distribution names may hold letters, digits,
# `.`, `-` and `_`, and must start and end alphanumeric.
_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")


class ProfileMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gpu_vendor: str | None = None
    min_vram_mb: int | None = Field(default=None, ge=0)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    match: ProfileMatch
    stt_model: str
    pip_extras: list[str]
    tts_note: str


class HardwareConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[Profile]
    conflicts: dict[str, list[str]]
    extra_consumers: dict[str, list[str]]


def load_hardware_config(path: Path | None = None) -> HardwareConfig:
    from tesseract.paths import config_dir

    target = path or config_dir() / "hardware.yaml"
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    return HardwareConfig.model_validate(raw)


def record_path() -> Path:
    """Where the applied profile is recorded.

    Under `runtime/` because it describes THIS machine: carrying it to the
    operator's other PC in `home/` would tell a different box it had already
    been profiled, and it would keep the first machine's model.
    """
    from tesseract.paths import runtime_dir

    return runtime_dir() / "hardware-profile.json"


def _read_record() -> dict:
    try:
        raw = json.loads(record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def recorded_profile() -> str | None:
    """The profile this machine resolved to last time, or None if never.

    None and "a different name" are different answers: the first means this
    machine has never been profiled, the second means its hardware changed
    under us. Only the second is a reason to revisit a config the operator
    may since have tuned.
    """
    name = _read_record().get("profile")
    return name if isinstance(name, str) and name else None


def previous_failures() -> int:
    """Consecutive failed GPU-install attempts recorded on this machine.

    An unreadable or absent record reads as zero: the breaker exists to stop
    a doomed retry loop, not to stop a first attempt.
    """
    try:
        return int(_read_record().get("install_failures", 0))
    except (TypeError, ValueError):
        return 0


def select_profile(cfg: HardwareConfig, gpu: Any) -> Profile:
    """First profile whose match the probe satisfies.

    Raises when nothing matches rather than returning None: the floor profile
    carries an empty match precisely so this cannot happen, and a config that
    lost it should say so loudly instead of silently installing nothing.
    """
    vendor = str(getattr(gpu, "vendor", "") or "").lower()
    vram = int(getattr(gpu, "memory_mb", 0) or 0)
    for profile in cfg.profiles:
        wanted = profile.match
        if wanted.gpu_vendor is not None and wanted.gpu_vendor.lower() != vendor:
            continue
        if wanted.min_vram_mb is not None and vram < wanted.min_vram_mb:
            continue
        return profile
    raise ValueError(
        "hardware.yaml has no profile matching this machine and no floor "
        "profile with an empty `match` — every machine must resolve to one"
    )


def _target_python() -> str:
    """The interpreter the packages must land in.

    `sys.executable` is right only because the shell invokes this module with
    the venv interpreter. Nothing in the module guaranteed that, and the
    docstring advertises running it by hand — from a system Python with the
    package on `PYTHONPATH`, that would install ~2 GB of CUDA wheels into the
    wrong environment and report success. Checked against the venv the state
    root actually owns, and refused rather than guessed.
    """
    from tesseract.paths import runtime_dir

    expected = runtime_dir() / "venv"
    running = Path(sys.executable).resolve()
    try:
        running.relative_to(expected.resolve())
    except ValueError:
        raise RuntimeError(
            f"refusing to install into {running}: it is not inside the "
            f"provisioned venv at {expected}. Run this through the packaged "
            f"shell, or with that interpreter."
        ) from None
    return str(running)


def _run_uv(uv: str, args: list[str]) -> bool:
    python = _target_python()
    try:
        proc = subprocess.run(
            [uv, "pip", *args, "--python", python],
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s: uv %s failed to run (%s)", _LABEL, args[0], exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "%s: uv %s exited %d - %s",
            _LABEL, args[0], proc.returncode, (proc.stderr or "").strip()[:400],
        )
        return False
    return True


# The wheels each extra brings, so readiness can be asked about the extras
# this machine actually wants rather than about a fixed global set. Asking
# globally is a false negative on any partial configuration — a speech-to-text
# only install has a complete and correct `[gpu]` and would still be told it
# was not ready, then reinstall ~2 GB on every launch until the breaker
# tripped. Both entries list cuBLAS and cuDNN because both providers link
# against them; see the note in pyproject.toml.
_EXTRA_WHEEL_PACKAGES: dict[str, tuple[str, ...]] = {
    "gpu": ("nvidia.cublas", "nvidia.cudnn"),
    "voice-local": (
        "nvidia.cublas",
        "nvidia.cudnn",
        "nvidia.cuda_nvrtc",
        "nvidia.cuda_runtime",
        "nvidia.cufft",
        "nvidia.curand",
        "nvidia.cusparse",
        "nvidia.cusolver",
    ),
}


def _add_cuda_dll_dirs(packages: tuple[str, ...]) -> bool:
    """Put the CUDA wheels' `bin/` dirs on the DLL search path.

    Mirrors `voice/providers/local_whisper.py::_ensure_cuda_dll_dirs`. The
    wheels ship their DLLs inside site-packages, where nothing finds them
    unless they are added explicitly. False means a wheel is missing, which
    is itself an answer.
    """
    from importlib.util import find_spec

    found = False
    for package in packages:
        try:
            spec = find_spec(package)
        except ModuleNotFoundError:
            return False
        if spec is None:
            return False
        for location in list(spec.submodule_search_locations or []):
            bin_dir = Path(location) / "bin"
            if not bin_dir.is_dir():
                continue
            found = True
            if sys.platform == "win32":
                if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
                    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    return False
    return found


def _ctranslate2_cuda_loadable() -> bool:
    """Whether Whisper's cuBLAS dependency resolves.

    The same question `local_whisper._cuda_runtime_loadable` asks before
    trusting `device: auto`, asked here so a speech-to-text-only machine can
    be judged on the stack it actually installed instead of on onnxruntime's,
    which it has no reason to have.
    """
    if sys.platform != "win32":
        return True
    import ctypes

    for name in ("cublas64_12.dll", "cublas64_11.dll"):
        try:
            ctypes.WinDLL(name)
            return True
        except OSError:
            continue
    return False


def gpu_packages_ready(extras: list[str] | None = None) -> bool:
    """True when the GPU path actually LOADS — not merely when it is installed.

    Scoped to `extras`, because the two serve different engines and a machine
    may legitimately want only one. Asking a fixed global question meant a
    speech-to-text-only install — whose `[gpu]` was complete and correct —
    was told it was not ready because onnxruntime's provider DLL was absent,
    and reinstalled on every launch until the breaker stopped it.

    The distinction is the whole reason this function is not a version check.
    `onnxruntime.get_available_providers()` lists `CUDAExecutionProvider`
    from the provider DLL being present on disk, and keeps listing it when
    that DLL cannot load a single one of its dependencies; session creation
    then falls back to CPU and reports success. An install with the CUDA 13
    build of onnxruntime-gpu sitting beside CUDA 12 wheels looks healthy by
    every metadata check and runs entirely on the processor.

    So the DLL is loaded directly, the same way `local_whisper` probes cuBLAS
    before trusting `device: auto`. Loading it is the question worth asking:
    it is what fails, with error 126, when the CUDA majors disagree or when
    the CPU `onnxruntime` distribution has overwritten the GPU one.
    """
    wanted = list(extras) if extras else list(_EXTRA_WHEEL_PACKAGES)
    packages: tuple[str, ...] = tuple(
        dict.fromkeys(
            pkg for extra in wanted for pkg in _EXTRA_WHEEL_PACKAGES.get(extra, ())
        )
    )
    if not packages:
        return True  # nothing was asked for, so nothing is missing
    if not _add_cuda_dll_dirs(packages):
        return False

    # Whisper's half. CTranslate2 needs cuBLAS and nothing from onnxruntime.
    if "gpu" in wanted and not _ctranslate2_cuda_loadable():
        return False
    if "voice-local" not in wanted:
        return True

    # Kokoro's half.
    try:
        import onnxruntime as ort  # type: ignore[import-not-found]
    except ImportError:
        return False
    if sys.platform != "win32":
        # The non-Windows wheels link through the loader's normal search
        # path; there is no separate provider DLL to probe by hand.
        return "CUDAExecutionProvider" in ort.get_available_providers()

    capi = Path(ort.__file__).parent / "capi"
    provider_dll = capi / "onnxruntime_providers_cuda.dll"
    if not provider_dll.exists():
        return False
    # The provider links against `onnxruntime.dll` and the shared provider
    # DLL sitting beside it, which are not on the search path either.
    try:
        os.add_dll_directory(str(capi))
    except OSError:
        return False
    import ctypes

    try:
        ctypes.WinDLL(str(provider_dll))
    except OSError:
        return False
    return True


def wanted_extras(profile: Profile, cfg: HardwareConfig) -> list[str]:
    """The profile's extras, minus any whose consumers the operator declined.

    The first-run form lets speech be turned off entirely, and declining a
    lane writes `enabled: false` onto its provider — which the fetch scripts
    already honour by downloading nothing. Accelerating an engine that will
    never run costs ~2 GB and buys nothing, and it breaks the form's promise
    that declining a lane costs nothing.

    A providers.yaml that cannot be read yields the profile's extras
    unchanged: this is an optimisation, and failing to read config is not a
    reason to leave a capable machine on the CPU path.
    """
    try:
        from tesseract.config.loader import load_config

        local = load_config().providers_raw.get("local") or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("%s: could not read provider switches (%s) - keeping extras", _LABEL, exc)
        return list(profile.pip_extras)

    keep: list[str] = []
    for extra in profile.pip_extras:
        consumers = cfg.extra_consumers.get(extra)
        if not consumers:
            keep.append(extra)  # nothing declared it optional
            continue
        if any(bool((local.get(name) or {}).get("enabled")) for name in consumers):
            keep.append(extra)
        else:
            logger.info(
                "%s: skipping [%s] - %s disabled in this config",
                _LABEL, extra, "/".join(consumers),
            )
    return keep


def ensure_packages(
    extras: list[str], cfg: HardwareConfig, *, dry_run: bool
) -> bool:
    """Install `extras`, then drop what they conflict with.

    Takes the resolved list rather than the profile so it is computed — and
    logged — exactly once per run, by the caller.
    """
    if not extras:
        logger.info("%s: no GPU packages wanted", _LABEL)
        return True
    if gpu_packages_ready(extras):
        logger.info("%s: GPU packages already resolve - nothing to install", _LABEL)
        return True

    failures = previous_failures()
    if failures >= MAX_CONSECUTIVE_FAILURES:
        logger.warning(
            "%s: the GPU package install has failed %d times on this machine - "
            "not retrying. Delete %s to try again.",
            _LABEL, failures, record_path(),
        )
        return False

    uv = os.environ.get(_UV_ENV, "").strip()
    if not uv or not Path(uv).exists():
        logger.warning(
            "%s: %s is not set to an existing uv binary - GPU packages skipped, "
            "this machine keeps the CPU path",
            _LABEL, _UV_ENV,
        )
        return False

    try:
        _target_python()
    except RuntimeError as exc:
        # Caught here rather than left to main()'s catch-all: by this point
        # the model choice may already have been applied successfully, and
        # main() would report the whole run as "CPU path kept" — a wrong
        # diagnostic on the one path whose job is diagnosis.
        logger.warning("%s: %s", _LABEL, exc)
        return False

    from tesseract.paths import TESSERACT_DIR

    target = f"{TESSERACT_DIR}[{','.join(extras)}]"
    conflicting = sorted(
        {name for extra in extras for name in cfg.conflicts.get(extra, [])}
    )
    # These names become `uv pip uninstall` arguments, and `hardware.yaml` is
    # seeded into the operator-writable config tree. Shape-checking them is
    # what stops an entry like `-r` or `--system` from being read as a flag
    # rather than as a package — a removal that would present as an
    # inexplicably broken venv rather than as the config edit it was. The
    # pattern is PEP 503's; it cannot begin with a dash.
    bad = [name for name in conflicting if not _DISTRIBUTION_NAME.fullmatch(name)]
    if bad:
        logger.warning(
            "%s: ignoring malformed conflict entr%s %s in hardware.yaml",
            _LABEL, "y" if len(bad) == 1 else "ies", ", ".join(repr(b) for b in bad),
        )
        conflicting = [name for name in conflicting if name not in set(bad)]
    if dry_run:
        logger.info("%s: would install %s then remove %s", _LABEL, target, conflicting)
        return True

    if not _run_uv(uv, ["install", "-e", target]):
        return False
    # After, not before: the editable install re-adds these transitively, so
    # removing them first accomplishes nothing.
    if conflicting and not _run_uv(uv, ["uninstall", *conflicting]):
        return False
    return True


def apply_stt_model(profile: Profile) -> bool:
    """Ensure `providers.yaml` names the profile's speech model.

    Returns whether the config now MATCHES the profile — not whether a write
    happened. The caller uses this to decide whether the choice may be
    recorded as settled, and "it was already correct" settles it just as
    firmly as "it was rewritten". Returning False for the no-op case would
    make a correct install retry forever and warn on every launch.
    """
    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.paths import config_dir

    path = config_dir() / "providers.yaml"
    if not path.exists():
        logger.warning("%s: %s missing - model choice not applied", _LABEL, path)
        return False

    changed = False

    def _apply(doc: Any) -> None:
        nonlocal changed
        entry = (
            ((doc.get("local") or {}).get("whisper") or {}).get("models") or {}
        ).get("local_whisper")
        if entry is None:
            raise KeyError("local.whisper.models.local_whisper")
        if entry.get("model") == profile.stt_model:
            return
        entry["model"] = profile.stt_model
        changed = True

    round_trip_yaml(path, _apply)
    if changed:
        logger.info("%s: stt model set to %s", _LABEL, profile.stt_model)
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        from tesseract.scripts.check_dependencies import _detect_gpu

        cfg = load_hardware_config()
        gpu = _detect_gpu()
        profile = select_profile(cfg, gpu)
        # ASCII only in this script's log lines: the shell captures them into
        # `runtime/logs/shell.log` through a pipe with the console codepage,
        # where an em dash arrives as a replacement char.
        logger.info(
            "%s: %s (%s, %s MB) -> profile %s, stt=%s",
            _LABEL, gpu.name or "no GPU", gpu.vendor, gpu.memory_mb,
            profile.name, profile.stt_model,
        )

        record = record_path()
        previous = recorded_profile()
        # Re-applied when the machine CHANGED, not merely because we ran
        # again. A box that gains a graphics card a year in should get the
        # model that card can carry — the alternative is a one-shot decision
        # taken on day one that no later hardware can revise. An unchanged
        # profile leaves the config exactly as the operator has it, so a
        # model they picked in Settings is never overwritten by a re-run.
        changed_machine = previous is not None and previous != profile.name
        if changed_machine:
            logger.info(
                "%s: this machine now profiles as %s (was %s) - revisiting the model",
                _LABEL, profile.name, previous,
            )
        # `settled` distinguishes "the model is what this machine should have"
        # from "we tried". Recording the profile on a run where the config
        # write could not happen — an unseeded tree, an unwritable file — would
        # leave the machine on the shipped default permanently, silently, with
        # no path back but a hand edit. So the record is only laid down once
        # the config half is actually true.
        settled = previous is not None and not changed_machine
        if (previous is None or changed_machine) and not args.dry_run:
            settled = apply_stt_model(profile)
        elif previous is not None:
            logger.info("%s: unchanged - leaving config as the operator has it", _LABEL)

        failures_before = previous_failures()
        # Resolved once: `ensure_packages` and the record below must agree on
        # what was wanted, and computing it twice re-read config and emitted
        # every "skipping" line a second time.
        extras = wanted_extras(profile, cfg)
        installed = ensure_packages(extras, cfg, dry_run=args.dry_run)
        ready = bool(installed) and gpu_packages_ready(extras)

        if not args.dry_run and not settled:
            logger.warning(
                "%s: model choice did not land - will retry on the next launch",
                _LABEL,
            )
        if not args.dry_run:
            # A profile that wanted no packages, or whose packages are in
            # place, must not carry a failure count forward — the breaker is
            # about install attempts that keep failing, and a success (or a
            # run with nothing to install) is what clears it.
            if not extras or ready:
                failures = 0
            else:
                failures = failures_before + 1
            # Written even when the config half did NOT land, because the
            # failure count is what stops a doomed 2.2 GB install repeating
            # at every launch, and a machine that cannot write its config is
            # exactly the machine most likely to be failing the install too.
            # `profile` is omitted in that case, so `recorded_profile()` still
            # reads "never profiled" and the config half is retried.
            body: dict[str, Any] = {
                "gpu": {
                    "vendor": gpu.vendor,
                    "name": gpu.name,
                    "memory_mb": gpu.memory_mb,
                },
                "gpu_packages_ready": ready,
                "install_failures": failures,
            }
            if settled:
                body["profile"] = profile.name
                body["stt_model"] = profile.stt_model
                body["tts_note"] = profile.tts_note
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps(body, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # Provisioning must not fail over this. The CPU path is slow, not
        # broken, and an install that stops here leaves the operator with
        # nothing at all.
        logger.warning("%s could not be applied (%s) - CPU path kept", _LABEL, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
