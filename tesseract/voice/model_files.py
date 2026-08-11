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
import os
import shutil
from pathlib import Path

from tesseract.paths import TESSERACT_DIR, install_root, runtime_dir

logger = logging.getLogger(__name__)


def models_root() -> Path:
    """The voice model tree — ``runtime/models/voice``.

    ~~Lives beside the code because these are large, immutable artifacts and
    ``repo.rs`` never removes untracked files, so they survive updates.~~
    True, and it missed what the update actually costs. `app_swap` advances by
    copying the WHOLE live tree to staging before the git move, untracked
    files included, so 2 GB of weights inside `app/` meant every update was a
    2 GB file-by-file copy with ~4 GB peak disk — against a production tree of
    8.7 MB. Surviving the update was never in question; paying for it on every
    launch was.

    They are machine state, not app code. The code tree updates from the
    production repo; downloaded dependencies update when what this machine
    needs changes — adding a GPU should fetch new artifacts without a code
    update, and a code update should not re-copy the weights. `runtime/`
    already holds exactly this kind of thing (`onnx-cache`, `venv`,
    `hardware-profile.json`).
    """
    return runtime_dir() / "models" / "voice"


def legacy_models_root() -> Path:
    """Where the weights lived before they moved out of the swapped tree.

    Kept so `migrate_legacy_models` can find them once; nothing else may
    resolve a model path through this.
    """
    return TESSERACT_DIR / "voice" / "models"


def migrate_legacy_models() -> list[str]:
    """Move any weights still inside the code tree out to `runtime/`.

    Idempotent and called once per process at startup. Without it the first
    launch after this change would re-download ~2 GB that is already on disk.

    Moves lane by lane and only into a lane that does not already exist, so a
    half-finished migration resumes rather than clobbering. A failure is
    logged and skipped, never raised: the fetch scripts can always re-acquire
    a lane, and refusing to boot over a file move would be the worse outcome.
    """
    legacy = legacy_models_root()
    if not legacy.is_dir():
        return []

    # Never move files out of a tree that is not part of the install we are
    # operating on. `legacy_models_root()` is anchored at TESSERACT_DIR — the
    # real source package — which `TESSERACT_HOME` does NOT relocate, while
    # `models_root()` follows the home. Any process pointed at a different
    # home (every test, and any tooling that isolates state) would otherwise
    # physically move the developer's 2 GB of weights out of the checkout and
    # into that temporary tree. Observed exactly once, in a test run, before
    # this guard existed. In production and in a plain dev checkout the two
    # roots share an install and this passes.
    try:
        legacy.relative_to(install_root())
    except ValueError:
        logger.debug(
            "voice models: legacy tree %s is outside install root %s — not migrating",
            legacy,
            install_root(),
        )
        return []

    moved: list[str] = []
    destination_root = models_root()
    for lane in sorted(p for p in legacy.iterdir() if p.is_dir()):
        target = destination_root / lane.name
        if target.exists():
            continue
        # A scaffold-only lane (README/.gitignore, no weights) is not worth
        # moving — the fetch scripts recreate it.
        if not any(child.is_file() and child.suffix not in (".md", "") for child in lane.rglob("*")):
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # `os.rename` first, deliberately. It is atomic on one filesystem
            # (which these always are — both roots are siblings under the
            # install root) and, unlike `shutil.move`, it FAILS when the target
            # exists instead of silently moving the source *inside* it and
            # producing `.../voice/kokoro/kokoro/`. That matters because the
            # fetch scripts can run concurrently with this: two processes can
            # both pass the `target.exists()` check above.
            os.rename(lane, target)
        except OSError:
            # Cross-device, or another process won the race. Only fall back
            # when the target is still absent, so the nesting footgun stays
            # unreachable; a lost race needs no work from us.
            if target.exists():
                continue
            try:
                shutil.move(str(lane), str(target))
            except OSError:
                logger.warning(
                    "voice models: could not move %s to %s", lane, target, exc_info=True
                )
                continue
        moved.append(lane.name)

    if moved:
        logger.info(
            "voice models: moved %s out of the swapped app tree into %s — "
            "updates no longer copy them",
            ", ".join(moved),
            destination_root,
        )
    return moved


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
