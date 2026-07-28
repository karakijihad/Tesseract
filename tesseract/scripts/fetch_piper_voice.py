"""First-run fetch for the shipped default Piper TTS voice model.

`provision.rs` downloads Python, dependencies, and a Chromium build on
first run, but never fetched the Piper voice model — the `.onnx` +
`.onnx.json` pair is gitignored (operator-downloaded, per
`tesseract/voice/models/piper/README.md`) and nothing filled that gap for
a fresh install. Without it, `piper_tts.py::_load_voice` raises and every
spoken reply falls back to text-only.

This module fetches exactly the voice named by
`providers.yaml::local.piper.northern_english_male.model` — the shipped
default — from a PINNED commit of the upstream `rhasspy/piper-voices`
repo, verifies each file's SHA256 before writing it, and never touches a
file that already exists (an operator's existing, working model is never
overwritten). Any failure (offline, upstream unreachable, checksum
mismatch) is logged and swallowed: a missing voice model degrades to
text-only replies exactly as it does today, and must never fail
provisioning or crash the app.

Invoked by `provision.rs` as `python -m tesseract.scripts.fetch_piper_voice`
after the venv + editable install exist (mirrors how `reinstall_deps` and
the Chromium fetch already run Python subprocesses from Rust, rather than
adding an HTTP client to the Rust shell for a single first-run download).
"""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Pinned to a specific commit of rhasspy/piper-voices — never "main". An
# upstream re-upload of the release-equivalent files must not silently
# change what a fresh install fetches; bumping this pin is a deliberate,
# reviewed edit, same discipline as scripts/fetch-uv.mjs's UV_VERSION.
# Recorded 2026-07-28 from:
#   https://huggingface.co/api/models/rhasspy/piper-voices/refs
_HF_COMMIT = "0d907f158acc877ddeebcbf827659ee13bea8bcd"
_HF_REPO_PATH = "en/en_GB/northern_english_male/medium"
_BASE_URL = f"https://huggingface.co/rhasspy/piper-voices/resolve/{_HF_COMMIT}/{_HF_REPO_PATH}"

# Matches providers.yaml::local.piper.northern_english_male.model — the
# shipped default TTS voice. sha256 recorded at pin time: the `.onnx`
# value is the HF LFS object's own `oid` (verified to match the
# downloaded bytes); the `.onnx.json` is a plain git blob, so its sha256
# was computed directly from the downloaded file.
_PINNED_FILES: dict[str, str] = {
    "en_GB-northern_english_male-medium.onnx": (
        "57a219ae8e638873db7d18893304be5069c42868f392bb95c3ff17f0690d0689"
    ),
    "en_GB-northern_english_male-medium.onnx.json": (
        "69557ed3d974463453e9b0c09dd99a7ed0e52b8b87b64b357dbeeb2540a97d47"
    ),
}

_TIMEOUT_SECONDS = 120


def _voice_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "voice" / "models" / "piper"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_one(dest: Path, filename: str, expected_sha256: str) -> bool:
    url = f"{_BASE_URL}/{filename}?download=true"
    tmp = dest.with_name(dest.name + ".part")
    try:
        logger.info("piper voice fetch: downloading %s", filename)
        with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as resp, tmp.open("wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.warning(
            "piper voice fetch: could not download %s (%s) — voice output stays "
            "unavailable until this succeeds; the app continues without it.",
            filename,
            exc,
        )
        tmp.unlink(missing_ok=True)
        return False

    actual = _sha256(tmp)
    if actual != expected_sha256:
        logger.warning(
            "piper voice fetch: checksum mismatch for %s (expected %s, got %s) — "
            "refusing to install a corrupted or tampered file; voice output stays "
            "unavailable.",
            filename,
            expected_sha256,
            actual,
        )
        tmp.unlink(missing_ok=True)
        return False

    tmp.replace(dest)
    logger.info(
        "piper voice fetch: wrote %s (%d bytes, sha256 verified)",
        dest,
        dest.stat().st_size,
    )
    return True


def ensure_default_voice(target_dir: Path | None = None) -> bool:
    """Download the pinned default Piper voice if it isn't already present.

    Never overwrites an existing file — an operator's already-working model
    on disk is left untouched, whatever its contents. Never raises: any
    failure is logged and the app continues with voice output unavailable,
    exactly as it does today when the model is simply absent.

    Returns True iff both pinned files are present on disk after the call
    (already there, or freshly fetched and verified).
    """
    out_dir = target_dir or _voice_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("piper voice fetch: cannot create %s: %s", out_dir, exc)
        return False

    all_present = True
    for filename, expected_sha256 in _PINNED_FILES.items():
        dest = out_dir / filename
        if dest.exists():
            logger.info("piper voice fetch: %s already present, skipping", filename)
            continue
        if not _fetch_one(dest, filename, expected_sha256):
            all_present = False
    return all_present


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ensure_default_voice()
    return 0  # a missing voice model is never a provisioning failure


if __name__ == "__main__":
    raise SystemExit(main())
