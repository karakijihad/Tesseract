"""First-run fetch for the Kokoro TTS model files.

Kokoro is one 311 MB ONNX model plus a 27 MB bundle of style embeddings —
every catalogued Kokoro voice is a `mix` over those same two files, so the
download is provider-level rather than per-voice, and the pin lives on the
`local.kokoro` connection block in `providers.yaml` rather than on each
voice entry.

Fetches nothing unless the operator's config actually names a Kokoro voice
in `roles.yaml::voice.tts` and both the `local` tier and the `kokoro`
provider are enabled. That is what makes declining voice during first-run
setup mean no bytes: `provision.rs` runs this script unconditionally, and
the config is what decides whether it does any work — here, on the launch
retry, and every time afterwards.

Usage: python -m tesseract.scripts.fetch_kokoro_voice [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tesseract.lib.pinned_fetch import (
    PinnedSource,
    ensure_files,
    files_present,
    parse_download_block,
)
from tesseract.voice import model_files
from tesseract.voice.model_files import configured_refs, migrate_legacy_models, warn_uncovered

logger = logging.getLogger(__name__)

_LABEL = "kokoro voice fetch"


def _pinned_source() -> PinnedSource | None:
    """The connection-level pin, or None when no Kokoro lane is configured.

    Every configured voice shares one connection, so the pin is read once.
    """
    refs = configured_refs("tts", "kokoro")
    if not refs:
        return None
    return parse_download_block(
        refs[0].connection.extra.get("download"),
        where=f"providers.yaml::{refs[0].ref.rsplit('.', 1)[0]}",
    )


def models_present(target_dir: Path | None = None) -> bool | None:
    """Whether the pinned files are on disk. None when nothing is configured.

    The cheap check the Settings panel renders from — three stats, no
    network — so a lane can say "model files missing" and offer to fetch
    them without the operator reading a log.
    """
    source = _pinned_source()
    if source is None:
        return None
    return files_present(source, target_dir or model_files.lane_dir("kokoro"))


def ensure_kokoro_models(target_dir: Path | None = None, *, force: bool = False) -> bool:
    """Download the Kokoro model + voices bundle if the config asks for them.

    Returns True when there is nothing to do (no Kokoro lane configured) or
    when every pinned file is present afterwards. Never raises — a missing
    model latches the Kokoro lane and the next TTS lane speaks, which is a
    supported degraded mode, not a provisioning failure.
    """
    refs = configured_refs("tts", "kokoro")
    if not refs:
        logger.info(
            "%s: no enabled Kokoro voice in roles.yaml::voice.tts — nothing to fetch",
            _LABEL,
        )
        return True

    source = _pinned_source()
    if source is None:
        return False

    # The catalog names its files per voice (`model`, `voices_file`); the pin
    # names them once. They have to agree or the fetch lands the wrong bytes.
    required = set()
    for ref in refs:
        required.add(str(ref.model.model))
        voices_file = ref.model.fields.get("voices_file")
        if voices_file:
            required.add(str(voices_file))
    warn_uncovered(required, set(source.files), label=_LABEL)

    return ensure_files(
        source, target_dir or model_files.lane_dir("kokoro"), label=_LABEL, force=force
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()
    # The shell spawns this script DETACHED at launch (provision.rs::
    # refresh_optional_assets) and starts the supervisor in the same breath,
    # so this process races the backend's own call to the same function. Do it
    # here too, before anything checks what is present: otherwise a fetch that
    # wins the race looks at the new location, finds it empty, and
    # re-downloads weights that are already on disk — the exact ~2 GB this
    # migration exists to save. It is idempotent, so both callers doing it is
    # free.
    migrate_legacy_models()
    try:
        ensure_kokoro_models(force=args.force)
    except Exception as exc:  # noqa: BLE001
        # Config load is the one step outside `ensure_files`' own guard, and
        # this runs into a hidden console where a traceback is invisible.
        logger.warning("%s failed: %s", _LABEL, exc)
    return 0  # a missing voice model is never a provisioning failure


if __name__ == "__main__":
    sys.exit(main())
