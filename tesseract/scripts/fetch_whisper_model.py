"""First-run fetch for the local Whisper (CTranslate2) speech-to-text model.

Without this, `faster-whisper` resolves a bare checkpoint name
(``large-v3-turbo``) by downloading it from HuggingFace at *first
transcription* — unpinned, unverified, and paid for by the operator as a
multi-minute stall the first time they speak. This fetches the same
snapshot during provisioning instead, from a revision-pinned URL with a
sha256 per file, into the voice model tree that `local_whisper.py` then
loads from directly.

Which snapshot is a config question, not a code one: the catalog entry's
`downloads:` map is keyed by checkpoint name, and the entry's `model:` field
picks one. Adding a size is a `providers.yaml` edit — hence no model name
appears here.

Fetches nothing unless `roles.yaml::voice.stt` names the local Whisper lane
and both the `local` tier and the `whisper` provider are enabled, so
declining speech input during first-run setup means no bytes are
downloaded, on that run or any later launch.

Usage: python -m tesseract.scripts.fetch_whisper_model [--force]
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
from tesseract.voice.model_files import configured_refs

logger = logging.getLogger(__name__)

_LABEL = "whisper model fetch"


def pinned_source(ref) -> tuple[str, PinnedSource | None]:  # noqa: ANN001
    """Resolve a Whisper catalog ref to its checkpoint name and pin."""
    model_name = str(ref.model.model)
    downloads = ref.model.fields.get("downloads") or {}
    block = downloads.get(model_name) if hasattr(downloads, "get") else None
    if block is None:
        logger.warning(
            "%s: %s names model %r, but the catalog entry's `downloads:` map has "
            "no pin for it (pinned: %s). Nothing is fetched, and faster-whisper "
            "would fall back to an unpinned HuggingFace download at first use — "
            "add a pin for %r or point the entry at a checkpoint that has one.",
            _LABEL,
            ref.ref,
            model_name,
            ", ".join(sorted(downloads)) or "none",
            model_name,
        )
        return model_name, None
    return model_name, parse_download_block(
        block, where=f"providers.yaml::{ref.ref}.downloads.{model_name}"
    )


def snapshot_present(root: Path | None = None) -> bool | None:
    """Whether the configured snapshot is on disk. None when no local STT
    lane is configured, so the Settings panel can tell "not wanted" apart
    from "wanted but missing"."""
    refs = configured_refs("stt", "local_whisper")
    if not refs:
        return None
    model_name, source = pinned_source(refs[0])
    if source is None:
        return False
    return files_present(source, model_files.whisper_snapshot_dir(model_name, root))


def ensure_whisper_model(root: Path | None = None, *, force: bool = False) -> bool:
    """Download the configured Whisper snapshot if it isn't already present.

    Returns True when there is nothing to do (no local STT lane configured)
    or when every pinned file is present afterwards. Never raises: an absent
    snapshot leaves faster-whisper's own lazy resolution in place, which is
    slower and unpinned but still works, so this is never fatal.
    """
    refs = configured_refs("stt", "local_whisper")
    if not refs:
        logger.info(
            "%s: no enabled local Whisper lane in roles.yaml::voice.stt — "
            "nothing to fetch",
            _LABEL,
        )
        return True

    model_name, source = pinned_source(refs[0])
    if source is None:
        return False
    return ensure_files(
        source, model_files.whisper_snapshot_dir(model_name, root), label=_LABEL, force=force
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()
    try:
        ensure_whisper_model(force=args.force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed: %s", _LABEL, exc)
    return 0  # a missing snapshot degrades to a lazy pull, never a failure


if __name__ == "__main__":
    sys.exit(main())
