"""Everything the app checks and repairs at launch, in one process, in order.

Replaces the six concurrent subprocesses the shell used to spawn on every
start (`provision.rs::LAUNCH_REFRESH_ASSETS`). That arrangement had two
defects this module exists to remove, and both were in its own comments:

- **Nothing shared a result.** Each fetcher re-derived which lane it wanted,
  from config, in its own interpreter — six imports of the runtime to answer
  one question six ways.
- **A race the shell acknowledged and could not fix.** `provision_hardware`
  WRITES the speech model into `providers.yaml` while the fetchers READ it to
  decide what to download, and spawning them together meant a first run could
  fetch the outgoing model. Ordering inside one process is what closes it.

The shell still spawns this and never waits, so launch pays no latency for it.

**What may be repaired without asking is `Consent`'s answer, not this
module's.** Today that resolves to "the live config asks for it", which is the
contract the fetch scripts have always honoured — declining a lane at setup
writes `enabled: false` and nothing is downloaded for it. When the first-run
form starts recording real answers, this code does not change: it already asks
the record rather than the config.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Callable

from tesseract.capability.state import DependencyRecord, DependencyState

logger = logging.getLogger(__name__)

_LABEL = "launch refresh"


def _fetch_whisper(*, force: bool) -> bool:
    from tesseract.scripts.fetch_whisper_model import ensure_whisper_model

    return ensure_whisper_model(force=force)


def _fetch_kokoro(*, force: bool) -> bool:
    from tesseract.scripts.fetch_kokoro_voice import ensure_kokoro_models

    return ensure_kokoro_models(force=force)


def _fetch_reranker(*, force: bool) -> bool:
    from tesseract.scripts.fetch_reranker_model import main as fetch_reranker

    return fetch_reranker(["--force"] if force else []) == 0


def _ensure_ollama(*, force: bool) -> bool:
    from tesseract.scripts.ensure_ollama import ensure_ollama

    # `allow_install=False`, exactly as the old `--no-install` entry did.
    # Re-running the vendor installer download on every launch would re-fetch
    # hundreds of megabytes for a machine where the install was declined; the
    # cheap halves — starting a stopped daemon, pulling a model whose first
    # attempt was interrupted — are the ones worth repeating.
    return ensure_ollama(allow_install=False)


#: Dependency id -> what repairs it. Absent from this map means nothing here
#: can fix it: `venv` is the shell's, `gpu-acceleration` is handled by the
#: hardware stage below (which has to run before the pass anyway), and
#: `browser-engine` is fetched by the Mirror's own boot warm-up —
#: `orchestrator/browser/provision.py::ensure_browsers_if_wanted`, which checks
#: both the `services.browser` switch AND, on an install whose setup never ran,
#: the consent ledger. It is the one optional artifact this pass does not
#: repair, so its gate has to live where it is fetched rather than here.
REPAIRS: dict[str, Callable[..., bool]] = {
    "whisper": _fetch_whisper,
    "kokoro": _fetch_kokoro,
    "reranker": _fetch_reranker,
    "ollama": _ensure_ollama,
    "ollama-models": _ensure_ollama,
}


def _hardware_stage() -> None:
    """Migrate any weights still inside the code tree, then profile.

    Before the pass, not beside it, and that ordering is the whole point:
    `provision_hardware` decides WHICH speech model this machine should have
    and writes it into `providers.yaml`, which is what the pass then reads to
    decide whether that model is present. Run them together — as six spawned
    processes did — and a first run can fetch the model it is in the middle of
    replacing.

    The migration runs first for the same reason it does inside each fetch
    script: without it, a launch after the P9 move looks at the new location,
    finds it empty, and re-downloads ~2 GB already on disk.
    """
    from tesseract.voice.model_files import migrate_legacy_models

    try:
        migrate_legacy_models()
    except Exception as exc:  # noqa: BLE001 — a failed move must not stop the pass
        logger.warning("%s: could not migrate legacy model weights (%s)", _LABEL, exc)

    try:
        from tesseract.scripts.provision_hardware import main as provision_hardware

        provision_hardware()
    except SystemExit:
        # `main()` is also an argparse entry point; a parse failure there must
        # not take the launch pass with it.
        logger.warning("%s: the hardware stage exited early", _LABEL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: the hardware stage failed (%s) — CPU path kept", _LABEL, exc)


def repairable(record: DependencyRecord) -> bool:
    """Whether this pass may act on `record` without asking anyone."""
    return record.id in REPAIRS and record.may_repair_silently


async def _repair(record: DependencyRecord) -> tuple[str, bool]:
    """Run one repair off the event loop.

    `force` for a stale artifact and only then: `ensure_files` leaves a present
    file untouched — deliberately, it is the operator's whatever its contents —
    so a lane whose files are the WRONG version would otherwise be skipped by
    the very call meant to fix it. It re-fetches every file in the lane rather
    than only the drifted one, which is the simpler trade and rare enough to be
    worth it.
    """
    force = record.state is DependencyState.STALE
    action = "re-downloading" if force else "downloading"
    logger.info("%s: %s %s (%s)", _LABEL, action, record.id, record.reason or record.state.value)
    try:
        ok = await asyncio.to_thread(REPAIRS[record.id], force=force)
    except Exception as exc:  # noqa: BLE001 — one lane failing must not stop the rest
        logger.warning("%s: %s could not be repaired (%s)", _LABEL, record.id, exc)
        return record.id, False
    return record.id, bool(ok)


async def refresh() -> int:
    """The pass. Returns how many dependencies were repaired.

    Hardware stage, then decide, then repair what consent already covers, then
    record the result — so the artifact on disk describes the machine AFTER
    this pass rather than before it.
    """
    from tesseract.capability.reconcile import run

    await asyncio.to_thread(_hardware_stage)

    state = await run()
    wanted = [record for record in state.dependencies.values() if repairable(record)]
    if not wanted:
        logger.info("%s: nothing to fetch", _LABEL)
        return 0

    # Independent lanes, so they run together — which is also what the six
    # spawned processes got right and is worth keeping.
    results = await asyncio.gather(*[_repair(record) for record in wanted])
    repaired = [dep_id for dep_id, ok in results if ok]
    failed = [dep_id for dep_id, ok in results if not ok]
    if failed:
        logger.info(
            "%s: %s still unavailable — the next launch retries",
            _LABEL, ", ".join(sorted(failed)),
        )

    # A second pass, so the artifact reflects what is true now. Cheap: every
    # file a repair just wrote has its pin recorded, so this is a string
    # comparison rather than a re-hash.
    await run()
    return len(repaired)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="reconcile and report, repairing nothing",
    )
    args = parser.parse_args(argv)

    # Spawned into a hidden console, so anything worth knowing reaches the log.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        if args.check_only:
            from tesseract.capability.reconcile import run

            asyncio.run(run())
        else:
            asyncio.run(refresh())
    except Exception as exc:  # noqa: BLE001 — never fail a launch over maintenance
        logger.warning("%s did not complete (%s)", _LABEL, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
