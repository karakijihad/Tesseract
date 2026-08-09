"""Where local voice model files live, and which ones the config asks for.

One definition of the voice model tree, shared by the runtime
(``mirror/server/app.py::_build_voice_runtime``), the three fetch scripts,
and the Settings status routes. It used to be spelled
``Path(__file__).resolve().parents[N] / "voice" / "models" / <lane>`` at
each site — the kind of duplication that drifts silently the first time one
of them moves.

`configured_refs` is the other half: a fetch script must download exactly
what the operator's config names and nothing else, so that declining a lane
during first-run setup means no bytes are fetched for it, on that run or on
any later launch. The enabled filter mirrors
``brain/boot.py::load_voice_config`` — a lane whose tier or provider switch
is off is not "configured" for this purpose either.
"""

from __future__ import annotations

import logging
from pathlib import Path

from tesseract.paths import TESSERACT_DIR

logger = logging.getLogger(__name__)


def models_root() -> Path:
    """The voice model tree.

    Lives beside the code rather than under ``TESSERACT_HOME`` because these
    are large, immutable, checksum-pinned artifacts rather than operator
    state: nothing edits them, they are identical on every install, and
    ``repo.rs`` never removes untracked files, so they survive updates.
    """
    return TESSERACT_DIR / "voice" / "models"


def lane_dir(lane: str) -> Path:
    """The model directory for one engine (``piper`` / ``kokoro`` /
    ``whisper``)."""
    return models_root() / lane


# The two files faster-whisper cannot construct a model without. A directory
# holding both is a usable CTranslate2 snapshot; anything less is a partial
# fetch, and handing that to `WhisperModel` fails at load with a far worse
# message than simply falling back to the checkpoint name.
_WHISPER_REQUIRED = ("config.json", "model.bin")


def whisper_snapshot_dir(model_name: str, root: Path | None = None) -> Path:
    """Where one Whisper checkpoint's CTranslate2 snapshot lives.

    Keyed by checkpoint name so several coexist: switching the catalog from
    `large-v3-turbo` to `base` and back must not re-download either, and the
    Settings panel offers exactly that swap.
    """
    return (root or lane_dir("whisper")) / model_name


def whisper_model_source(model_name: str, root: Path | None = None) -> str:
    """What to hand `WhisperModel` for `model_name`.

    A fetched snapshot directory when one is complete on disk, otherwise the
    bare checkpoint name — which is what faster-whisper resolves by
    downloading from HuggingFace, unpinned, at first transcription. That
    fallback is deliberately kept: an install whose fetch was declined or
    failed still transcribes, just slowly the first time.
    """
    snapshot = whisper_snapshot_dir(model_name, root)
    if all((snapshot / name).exists() for name in _WHISPER_REQUIRED):
        return str(snapshot)
    return model_name


def configured_refs(kind: str, adapter: str) -> list:
    """Resolved catalog refs for `adapter` in the ``stt`` / ``tts`` lane.

    Returns them in chain order (primary first), skipping any whose tier or
    provider ``enabled`` switch is off. An empty list means the config does
    not ask for this engine — which every fetch script treats as "nothing to
    download", not as a failure.
    """
    # `load_config` rather than `brain.boot.load_bundle` (which only wraps
    # it): a fetch script runs as its own subprocess during provisioning and
    # has no reason to import the whole brain to read two yaml files.
    from tesseract.config.loader import load_config

    bundle = load_config()
    if bundle.voice is None:
        return []
    chain = getattr(bundle.voice, kind, None)
    if chain is None:
        return []
    kept = []
    for provider in chain.chain():
        conn = provider.ref.connection
        if conn.adapter != adapter:
            continue
        if not conn.tier_enabled or not conn.enabled:
            logger.info(
                "voice models: skipping ref=%s (tier_enabled=%s, enabled=%s)",
                provider.ref.ref,
                conn.tier_enabled,
                conn.enabled,
            )
            continue
        kept.append(provider.ref)
    return kept


def warn_uncovered(required: set[str], covered: set[str], *, label: str) -> None:
    """Warn when the config names a file the download pin does not cover.

    A pin that fetches successfully but misses the filename the catalog
    entry actually names produces the worst outcome available: provisioning
    reports success, the files land, and the lane still fails at first
    synthesis with a missing-model error. Naming the gap at fetch time is
    what makes that a diagnosable config error instead of a silent one.
    """
    gap = sorted(required - covered)
    if gap:
        logger.warning(
            "%s: the catalog names %s but the download pin does not cover "
            "%s — that file will still be missing after a successful fetch.",
            label,
            "them" if len(gap) > 1 else "it",
            ", ".join(gap),
        )
