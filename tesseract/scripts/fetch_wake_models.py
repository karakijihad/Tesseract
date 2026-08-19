"""First-run fetch for the wake-word keyword model.

One archive, pinned in ``mirror.yaml::identity.wake_word.download`` and
verified against its sha256 before anything is unpacked. It holds the
transducer the gate decodes through, and its own README declares it
Apache-2.0.

The release publishes no per-file downloads, so 18 MB arrives to keep the
~5.5 MB the gate loads: the int8 encoder/decoder/joiner plus the two
vocabulary files. Everything else in the archive — the fp32 duplicates,
sample audio, the publisher's own keyword lists — is discarded, and the
archive with it. Extraction is by an explicit five-name allowlist writing
bytes to paths this module builds, never ``extractall``: a verified digest
says the bytes are what the catalog promised, and says nothing about where
a member inside them asked to be written.

Fetches nothing unless the wake word is enabled in config. That is what
makes leaving it off mean zero bytes: `provision.rs` runs this script
unconditionally and the config decides whether it does any work — here, on
the launch retry, and every time afterwards.

Usage: python -m tesseract.scripts.fetch_wake_models [--force]
"""

from __future__ import annotations

import argparse
import logging
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

from tesseract.lib.pinned_fetch import (
    PinnedSource,
    ensure_files,
    parse_download_block,
)

logger = logging.getLogger(__name__)

_LABEL = "wake model fetch"
_WHERE = "mirror.yaml::identity.wake_word.download"


def _wake_block() -> dict[str, Any] | None:
    """The `identity.wake_word` block, or None if it cannot be read.

    Read straight off the YAML rather than through `load_server_config`: this
    runs during provisioning, before there is a server, and a wake fetch must
    not be able to fail because some unrelated part of the config is
    incomplete.
    """
    import yaml

    from tesseract.mirror.server.config import MIRROR_YAML

    try:
        # Parsed here rather than through `config.py`'s private helper: this
        # runs during provisioning and only needs one block, so it should not
        # depend on a symbol that module is free to rename.
        mirror = yaml.safe_load(MIRROR_YAML.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - unreadable config is "nothing to do"
        logger.warning("%s: could not read %s: %s", _LABEL, MIRROR_YAML, exc)
        return None
    if not isinstance(mirror, dict):
        logger.warning("%s: %s is not a mapping — nothing to fetch", _LABEL, MIRROR_YAML)
        return None
    identity = mirror.get("identity") or {}
    block = identity.get("wake_word")
    return block if isinstance(block, dict) else None


def _pinned_source() -> PinnedSource | None:
    block = _wake_block()
    if block is None:
        logger.warning("%s: no identity.wake_word block — nothing to fetch", _LABEL)
        return None
    return parse_download_block(block.get("download"), where=_WHERE)


def wake_enabled() -> bool:
    """Whether config permits the wake word at all.

    Permission, not readiness — the gate additionally needs a calibration
    before it arms. This is only the question "may we spend the bytes".
    """
    block = _wake_block()
    return bool(block and block.get("enabled") is True)


def models_present(target_dir: Path | None = None) -> bool | None:
    """Whether the extracted model files are on disk. None when the pin is
    unreadable.

    The cheap check Settings renders from — five stats, no network — so the
    Voice panel can say "wake model missing" and offer to fetch it without
    the operator reading a log.

    Asks the spotter, not the pin: what is pinned is an archive that is
    deleted after unpacking, so `files_present` on the catalog block would
    report missing on every launch of a perfectly good install.
    """
    from tesseract.voice.wake_spotter import models_dir, models_present as present

    if _pinned_source() is None:
        return None
    return present(target_dir or models_dir())


def _extract(archive: Path, dest_dir: Path) -> bool:
    """Unpack the five files the gate loads, and nothing else.

    Members are matched on their BASENAME against the allowlist and written
    to a path built here, so a member naming `../` or an absolute path lands
    exactly where every other one does or not at all.
    """
    from tesseract.voice.wake_spotter import MODEL_FILES

    wanted = set(MODEL_FILES)
    written: set[str] = set()
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar:
                name = PurePosixPath(member.name).name
                if not member.isfile() or name not in wanted or name in written:
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                (dest_dir / name).write_bytes(source.read())
                written.add(name)
    except (OSError, tarfile.TarError) as exc:
        logger.warning("%s: could not unpack %s: %s", _LABEL, archive.name, exc)
        return False

    missing = sorted(wanted - written)
    if missing:
        logger.warning(
            "%s: %s did not contain %s — the pin may name a different build",
            _LABEL,
            archive.name,
            ", ".join(missing),
        )
        return False
    logger.info("%s: unpacked %d files into %s", _LABEL, len(written), dest_dir)
    return True


def ensure_wake_models(target_dir: Path | None = None, *, force: bool = False) -> bool:
    """Download and unpack the keyword model if config asks for it.

    Returns True when there is nothing to do (wake word off, or the files are
    already there) or when every file is present afterwards. Never raises — a
    missing model leaves the gate open, which is a supported degraded mode and
    not a provisioning failure. A wake word that cannot load must never become
    a mute.
    """
    from tesseract.voice.wake_spotter import models_dir, models_present as present

    if not wake_enabled():
        logger.info("%s: wake word disabled in config — nothing to fetch", _LABEL)
        return True

    source = _pinned_source()
    if source is None:
        return False

    dest = target_dir or models_dir()
    if present(dest) and not force:
        logger.info("%s: model files already present, skipping", _LABEL)
        return True

    if not ensure_files(source, dest, label=_LABEL, force=force):
        return False

    ok = True
    for filename in source.files:
        archive = dest / filename
        if not _extract(archive, dest):
            ok = False
        # The archive is 18 MB of which 12 is duplicates the gate never opens,
        # and leaving it would also make the next launch look like a completed
        # fetch while the files it needs are absent.
        archive.unlink(missing_ok=True)
    return ok and present(dest)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()
    try:
        ensure_wake_models(force=args.force)
    except Exception as exc:  # noqa: BLE001
        # Config load is the one step outside `ensure_files`' own guard, and
        # this runs into a hidden console where a traceback is invisible.
        logger.warning("%s failed: %s", _LABEL, exc)
    return 0  # a missing wake model is never a provisioning failure


if __name__ == "__main__":
    sys.exit(main())
