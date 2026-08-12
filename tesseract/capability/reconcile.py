"""One pass: run every probe, merge the verdicts, write the artifact.

This is the module that makes the phase's claim true — that the answer is
computed **once** per launch instead of eight times, concurrently, by
processes that share nothing. Every probe here is independent, so they all
run at once and the pass costs its slowest one rather than their sum.

`return_exceptions=True` throughout, and each result is inspected. A probe
that throws must cost its own dependency's verdict and nothing else: the
whole value of a reconciler is that it still reports the other eleven things
when one of them is broken.

Nothing here downloads, installs, starts or repairs. The pass decides what is
true; `DependencyRecord.may_repair_silently` decides who is allowed to act on
it, and the acting lives with the callers that already own those operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable

from tesseract.capability import consent, models, system
from tesseract.capability.state import (
    Advice,
    CapabilityState,
    DependencyRecord,
    DependencyState,
    HardwareFacts,
    VerifiedPin,
    now_iso,
    read_state,
    state_path,
    write_state,
)

logger = logging.getLogger(__name__)


"""Consent no longer travels in the artifact at all.

It used to be carried from the previous pass's record, which worked and was
fragile in two ways worth naming: a pass that failed to write lost every
answer with it, and a consent write racing a pass write meant the loser's
answer was silently dropped — and a dropped answer is a question the operator
gets asked a second time.

`capability/consent.py` owns it now, in its own small file, and the pass reads
it. A `CONFIG`-derived answer is still computed here every pass, because it is
derived from the live config and must not persist past a switch being turned
off; a real answer from the ledger overlays it.
"""


def _recorded_pins(
    previous: CapabilityState | None,
) -> dict[str, dict[str, VerifiedPin]]:
    """Last pass's verified pins, keyed by dependency.

    These are the whole reason a launch costs no hashing: a file whose pin was
    recorded is judged by comparing two strings.
    """
    if previous is None:
        return {}
    return {dep_id: dict(rec.pins) for dep_id, rec in previous.dependencies.items()}


async def _gather(
    label: str, jobs: Iterable[Awaitable[list[DependencyRecord]]]
) -> list[DependencyRecord]:
    """Run independent probe groups concurrently, keeping what succeeded."""
    out: list[DependencyRecord] = []
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            # Named, not swallowed. A probe group that vanishes silently is
            # indistinguishable from one that found nothing wrong, which is
            # the failure mode this whole phase exists to remove.
            logger.warning("capability: a %s probe failed (%s)", label, result)
            continue
        out.extend(result)
    return out


async def _sync_group(fn: Callable[[], DependencyRecord]) -> list[DependencyRecord]:
    """One blocking probe, off the event loop.

    `gpu_packages_ready` loads DLLs and `pins.resolve` may hash a 1.6 GB file.
    Either on the loop would stall health checks, WS heartbeats and any turn
    in flight — the exact class of stall P1 spent a phase measuring.
    """
    return [await asyncio.to_thread(fn)]


async def _models_group(
    recorded: dict[str, dict[str, VerifiedPin]],
) -> list[DependencyRecord]:
    return await asyncio.to_thread(models.check_all, recorded)


def _collect_hardware() -> HardwareFacts:
    """Machine facts, folded in from the old snapshot's collector.

    Blocking — it shells out to `nvidia-smi` and enumerates audio devices —
    so it runs in a thread with everything else.
    """
    from tesseract.scripts.check_dependencies import collect

    snap = collect()
    profile: str | None = None
    try:
        from tesseract.scripts.provision_hardware import recorded_profile

        profile = recorded_profile()
    except Exception as exc:  # noqa: BLE001 — an unread profile is not a failed pass
        logger.info("capability: could not read the hardware profile (%s)", exc)

    tts_note: str | None = None
    try:
        from tesseract.scripts.provision_hardware import _read_record

        tts_note = _read_record().get("tts_note") or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("capability: could not read the profile's tts note (%s)", exc)

    return HardwareFacts(
        tts_note=tts_note,
        gpu_vendor=snap.gpu.vendor,
        gpu_name=snap.gpu.name,
        gpu_memory_mb=snap.gpu.memory_mb,
        gpu_cuda=snap.gpu.cuda,
        ram_total_gb=snap.ram_total_gb,
        disk_free_gb=snap.disk_free_gb,
        mic_devices=snap.mic_devices,
        python_version=snap.python_version,
        node_version=snap.node_version,
        pnpm_version=snap.pnpm_version,
        platform=dict(snap.platform),
        profile=profile,
    )


def legacy_system_payload(hardware: HardwareFacts) -> dict:
    """`HardwareFacts` in the shape `GET /api/settings/system` has always
    returned.

    The System tab renders these exact keys, and the frontend does not reach
    an install through update (`tauri.conf.json` compiles it into the `.exe`),
    so changing the shape here would break the installed UI until a new
    installer ships. The artifact is the superset; this is the projection.
    """
    return {
        "python_version": hardware.python_version,
        "node_version": hardware.node_version,
        "pnpm_version": hardware.pnpm_version,
        "gpu": {
            "vendor": hardware.gpu_vendor,
            "name": hardware.gpu_name,
            "memory_mb": hardware.gpu_memory_mb,
            "cuda": hardware.gpu_cuda,
        },
        "ram_total_gb": hardware.ram_total_gb,
        "disk_free_gb": hardware.disk_free_gb,
        "mic_devices": hardware.mic_devices,
        "platform": dict(hardware.platform),
    }


async def _hardware() -> HardwareFacts:
    try:
        return await asyncio.to_thread(_collect_hardware)
    except Exception as exc:  # noqa: BLE001
        logger.warning("capability: hardware facts could not be collected (%s)", exc)
        return HardwareFacts()


def _remove_legacy_snapshot() -> bool:
    """Delete the artifact this one replaced, once.

    An install that has already run carries `runtime/logs/capability-
    snapshot.json`, and after this phase nothing reads it. Leaving it is
    exactly the kind of litter this project forbids — worse than ordinary
    litter,
    because a file that LOOKS like current machine state is one a future
    reader trusts.

    Best-effort and silent about absence: on a fresh install there is nothing
    to remove, which is the common case.
    """
    from tesseract.paths import runtime_logs_root

    legacy = runtime_logs_root() / "capability-snapshot.json"
    try:
        legacy.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.info("capability: could not remove the superseded %s (%s)", legacy, exc)
        return False
    logger.info(
        "capability: removed %s — its contents are in %s now", legacy, state_path()
    )
    return True


#: What each profile's `tts_note` means, in words a person would use. The note
#: itself has been written to `hardware-profile.json` since P1.5 and read by
#: nobody — so a machine that lost its graphics card went on recommending the
#: voice it could no longer keep up with, silently.
_TTS_ADVICE = {
    "kokoro-gpu": "This machine can now run the Natural voice comfortably.",
    "piper-preferred": (
        "The Light voice will keep up better than Natural on this machine now."
    ),
}


def profile_advice(
    previous: CapabilityState | None, hardware: HardwareFacts
) -> list[Advice]:
    """One line when the machine's profile CHANGED, and nothing otherwise.

    The detection rule is `provision_hardware`'s own and it is the right one:
    act on a changed *detected profile*, never on merely having run again. That
    is what lets a box which gains an external graphics card a year in pick up
    the faster model, while leaving a value the operator set by hand alone.

    What was missing is that it happened in silence. Nothing told anyone the
    machine had got faster, or slower.

    Returns nothing on a first-ever pass: everything is "new" then, and
    greeting a fresh install with a change notice is noise.
    """
    if previous is None or not previous.hardware.profile or not hardware.profile:
        return []
    if previous.hardware.profile == hardware.profile:
        return []

    detail = _TTS_ADVICE.get(hardware.tts_note or "", "")
    return [
        Advice(
            id="profile-changed",
            text=(
                f"This machine now looks different to TESSERACT "
                f"({previous.hardware.profile} → {hardware.profile}). {detail}"
            ).strip(),
            at=now_iso(),
        )
    ]


async def reconcile() -> CapabilityState:
    """Judge every dependency and return the merged verdict.

    Does not write. `run()` is the entry that persists, so a caller wanting an
    answer without touching disk — a route handler, a test — has one.
    """
    previous = read_state()
    recorded = _recorded_pins(previous)

    # Every group is independent, so they all start together. The hardware
    # collection rides along rather than running before: nothing in the probe
    # set needs its result.
    hardware_task = asyncio.create_task(_hardware())
    records = await _gather(
        "capability",
        [
            _models_group(recorded),
            system.check_ollama(),
            *[_sync_group(fn) for fn in system.SYNC_CHECKS],
        ],
    )
    hardware = await hardware_task

    # Fresh verdicts, then the operator's own answers laid over the top. The
    # ledger read is one file and happens once for the whole pass.
    merged = consent.apply_all({record.id: record for record in records})

    try:
        from tesseract.bootid import current_boot_id

        boot_id = current_boot_id()
    except Exception:  # noqa: BLE001 — an id is a convenience, never a reason to fail
        boot_id = ""

    return CapabilityState(
        checked_at=now_iso(),
        boot_id=boot_id,
        dependencies=merged,
        hardware=hardware,
        advice=profile_advice(previous, hardware),
    )


async def run() -> CapabilityState:
    """Reconcile and persist. The entry every launch calls."""
    state = await reconcile()
    written = write_state(state)
    if written is not None:
        # Only after the replacement is safely on disk. Removing it first
        # would lose the machine's facts entirely on a pass whose write failed.
        await asyncio.to_thread(_remove_legacy_snapshot)
    attention = state.attention
    if attention:
        logger.info(
            "capability: %d of %d dependencies need attention (%s)",
            len(attention),
            len(state.dependencies),
            ", ".join(f"{d.id}={d.state.value}" for d in attention),
        )
    else:
        # Said at debug, not info. A line every launch announcing that nothing
        # is wrong is what teaches an operator to stop reading these.
        logger.debug(
            "capability: all %d dependencies are as this version expects",
            len(state.dependencies),
        )
    if written is None:
        logger.warning(
            "capability: the pass completed but its result could not be saved — "
            "the surfaces that report it will show the previous answer"
        )
    return state


def summarise(state: CapabilityState | None) -> str:
    """One line for a log or a status row. Empty when there is nothing to say."""
    if state is None:
        return ""
    attention = state.attention
    if not attention:
        return ""
    stale = [d for d in attention if d.state is DependencyState.STALE]
    if stale:
        return (
            f"{len(stale)} of {len(state.dependencies)} components are not the "
            f"version this build expects"
        )
    return f"{len(attention)} components are switched on but not downloaded"
