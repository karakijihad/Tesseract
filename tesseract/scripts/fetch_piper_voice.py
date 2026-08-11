"""First-run fetch for the shipped Piper TTS voice models.

A Piper voice IS its `.onnx` file plus the `.onnx.json` sidecar beside it,
both gitignored, so nothing filled that gap for a fresh install and
`piper_tts.py::_load_voice` raised and latched the lane disabled.

Each voice carries its own pin — a revision-locked upstream URL and a
sha256 per file — on its `providers.yaml` model entry, because unlike
Kokoro every Piper voice is a separate download. The pins used to live in a
dict here; they moved to the catalog so that adding a voice is a config
edit, and so this script fetches the voice the operator's config actually
names. It previously fetched a hardcoded default, which meant an operator
who selected the male voice had the female one downloaded for them.

Fetches nothing unless `roles.yaml::voice.tts` names a Piper voice and both
the `local` tier and the `piper` provider are enabled, so declining voice
during first-run setup means no bytes are downloaded.

Usage: python -m tesseract.scripts.fetch_piper_voice [--voice <id>] [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tesseract.lib.pinned_fetch import (
    ensure_files,
    files_present,
    parse_download_block,
)
from tesseract.voice import model_files
from tesseract.voice.model_files import configured_refs, migrate_legacy_models, warn_uncovered

logger = logging.getLogger(__name__)

_LABEL = "piper voice fetch"


def voices_present(target_dir: Path | None = None) -> bool | None:
    """Whether every configured voice's files are on disk. None when no
    Piper lane is configured."""
    refs = configured_refs("tts", "piper")
    if not refs:
        return None
    out_dir = target_dir or model_files.lane_dir("piper")
    for ref in refs:
        source = parse_download_block(
            ref.model.fields.get("download"), where=f"providers.yaml::{ref.ref}"
        )
        if source is None or not files_present(source, out_dir):
            return False
    return True


def _sidecar(model_filename: str) -> str:
    """Piper reads `<voice>.onnx.json` beside `<voice>.onnx`; the catalog
    only names the `.onnx`, so the sidecar is derived rather than configured
    (`app.py::_build_voice_runtime` derives it the same way)."""
    return f"{model_filename}.json"


def _fetch_ref(ref, target_dir: Path | None, *, force: bool) -> bool:  # noqa: ANN001
    source = parse_download_block(
        ref.model.fields.get("download"), where=f"providers.yaml::{ref.ref}"
    )
    if source is None:
        return False
    model_filename = str(ref.model.model)
    warn_uncovered(
        {model_filename, _sidecar(model_filename)}, set(source.files), label=_LABEL
    )
    return ensure_files(
        source, target_dir or model_files.lane_dir("piper"), label=_LABEL, force=force
    )


def ensure_configured_voices(target_dir: Path | None = None, *, force: bool = False) -> bool:
    """Download every Piper voice the config names, in chain order.

    Both the primary and the fallbacks: a fallback whose model file is
    missing is a lane that latches the moment the one ahead of it fails,
    which is exactly when the operator needs it to work.
    """
    refs = configured_refs("tts", "piper")
    if not refs:
        logger.info(
            "%s: no enabled Piper voice in roles.yaml::voice.tts — nothing to fetch",
            _LABEL,
        )
        return True
    complete = True
    for ref in refs:
        complete = _fetch_ref(ref, target_dir, force=force) and complete
    return complete


def ensure_voice(voice_id: str, target_dir: Path | None = None, *, force: bool = False) -> bool:
    """Download one catalogued Piper voice by its `providers.yaml` model id.

    The Settings panel's per-voice download action, and the path a voice
    selection takes when the operator picks one that was never fetched. An
    unknown id returns False rather than raising — it is a config typo, not
    a provisioning failure.
    """
    from tesseract.config.loader import load_config

    bundle = load_config()
    providers = (bundle.providers_raw.get("local") or {}).get("piper") or {}
    entry = (providers.get("models") or {}).get(voice_id)
    if entry is None:
        known = ", ".join(sorted((providers.get("models") or {}))) or "none"
        logger.warning(
            "%s: no catalog entry for voice %r — known voices: %s", _LABEL, voice_id, known
        )
        return False

    source = parse_download_block(
        entry.get("download"), where=f"providers.yaml::local.piper.{voice_id}"
    )
    if source is None:
        return False
    model_filename = str(entry.get("model") or "")
    if model_filename:
        warn_uncovered(
            {model_filename, _sidecar(model_filename)}, set(source.files), label=_LABEL
        )
    return ensure_files(
        source, target_dir or model_files.lane_dir("piper"), label=_LABEL, force=force
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice", help="catalog model id to fetch (default: whatever roles.yaml names)")
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
        if args.voice:
            ensure_voice(args.voice, force=args.force)
        else:
            ensure_configured_voices(force=args.force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed: %s", _LABEL, exc)
    return 0  # a missing voice model is never a provisioning failure


if __name__ == "__main__":
    raise SystemExit(main())
