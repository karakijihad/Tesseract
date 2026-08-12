"""Deciding whether the files on disk are the ones this version names.

The whole of the "is it current?" question for model artifacts, and it is
smaller than it looks — because every model in the catalog is pinned to an
upstream revision plus a per-file sha256, **the app version is the pin.**
There is no upstream "latest" to poll, no version string to fetch, and no
token to hold. Drift is the catalog naming a different pin than the one a
file was verified against.

The mechanism that makes it free: `pinned_fetch` verified each file's digest
once, when it wrote it. That verdict is *recorded* rather than recomputed, so
the per-launch check is a string comparison over a handful of names.
Re-hashing instead would mean reading ~2 GB at every start — a cost
`pinned_fetch.ensure_files` already refuses to pay, for the same reason.

A file with no recorded verdict is hashed exactly once and the result kept,
so the cost is paid on the first launch after this ships and never again.
That includes a file that turns out to match nothing: its observed digest is
recorded with an empty `base_url`, meaning "hashed here, not from a fetch we
performed" — which is what stops a corrupted artifact being re-hashed on
every launch forever.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from tesseract.capability.state import DependencyState, VerifiedPin

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 1024 * 1024

#: The `base_url` recorded for a digest this module computed itself, rather
#: than one carried over from a verified download. Empty rather than a marker
#: string so it can never be mistaken for a real upstream location.
OBSERVED = ""


def file_digest(path: Path) -> str | None:
    """sha256 of `path`, or None if it could not be read.

    Blocking and potentially long — a 1.6 GB model takes seconds. Callers on
    the event loop must wrap this in `asyncio.to_thread`; nothing here does it
    for them, because the fetch scripts call this from plain sync code.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("capability: could not hash %s (%s)", path, exc)
        return None
    return digest.hexdigest()


def catalog_pin(base_url: str, sha256: str) -> VerifiedPin:
    """The pin the catalog names for one file, as a comparable value."""
    return VerifiedPin(base_url=base_url, sha256=sha256.lower())


def resolve(
    *,
    base_url: str,
    files: dict[str, str],
    dest_dir: Path,
    recorded: dict[str, VerifiedPin] | None = None,
) -> tuple[DependencyState, dict[str, VerifiedPin], str]:
    """Compare what is on disk against what the catalog names.

    Returns the state, the pins to record for next time, and one line of
    reason written for a person.

    `files` is `filename -> sha256`, exactly `PinnedSource.files`. `recorded`
    is what a previous pass wrote; absent entries are hashed once here.

    Never raises. An unreadable file is reported, not thrown — this runs
    beside a launch, and a reconciler that dies has told nobody anything.
    """
    recorded = dict(recorded or {})
    if not files:
        return (
            DependencyState.UNKNOWN,
            {},
            "the catalog names no files for this, so there is nothing to check",
        )

    # Belt to `parse_download_block`'s braces, matching what `ensure_files`
    # does before it writes. Every caller in this package feeds files the
    # parser already vetted — but `resolve` is public and takes a plain dict,
    # and a `files:` key names a file INSIDE the destination, never a path to
    # anywhere else.
    #
    # BOTH checks, because neither covers the other. The name rule is
    # `pinned_fetch`'s own — reached for rather than restated, so one
    # definition of "this is a filename" governs the fetch and the check of
    # what it fetched. It is the stronger of the two on Windows: measured,
    # `Path("C:/…/lane") / "C:model.onnx"` yields `C:\…\lane\model.onnx` when
    # the drives MATCH, so a drive-relative name passes a containment test and
    # is refused only by its shape — as is `model.onnx:stream`, an NTFS
    # alternate data stream that resolves to a contained path. The containment
    # test then catches what a name cannot show: a symlinked destination.
    from tesseract.lib.pinned_fetch import _unsafe_filename_reason

    try:
        resolved_dir = dest_dir.resolve()
    except OSError:
        return DependencyState.UNKNOWN, {}, "its download directory could not be read"

    present: list[str] = []
    missing: list[str] = []
    escaping: list[str] = []
    for name in files:
        candidate = dest_dir / name
        if _unsafe_filename_reason(name) is not None:
            escaping.append(name)
            continue
        try:
            contained = candidate.resolve().parent == resolved_dir
        except OSError:
            contained = False
        if not contained:
            escaping.append(name)
            continue
        if candidate.is_file():
            present.append(name)
        else:
            missing.append(name)

    if escaping:
        # Never treated as merely missing: a name that leaves the destination
        # is a broken or hostile pin, and reporting it as "not downloaded"
        # would invite a repair that fetches it to wherever it points.
        return (
            DependencyState.UNKNOWN,
            {},
            f"{_names(escaping)} is not a filename this may check",
        )

    if not present:
        return DependencyState.ABSENT, {}, "not downloaded"

    if missing:
        # A partial fetch is not a lighter install — it is a lane that fails at
        # first use with a worse message than "missing". Treated as absent so
        # the repair path re-fetches, which `ensure_files` does per missing
        # file rather than wholesale.
        return (
            DependencyState.ABSENT,
            {name: recorded[name] for name in present if name in recorded},
            f"{len(missing)} of {len(files)} files are missing — "
            f"the download did not finish",
        )

    pins: dict[str, VerifiedPin] = {}
    drifted: list[str] = []
    unreadable: list[str] = []

    for name in present:
        wanted = catalog_pin(base_url, files[name])
        known = recorded.get(name)

        if known is None:
            # First sight of this file: hash it once, and keep whatever the
            # answer is so no later launch pays this again.
            observed = file_digest(dest_dir / name)
            if observed is None:
                unreadable.append(name)
                continue
            if observed == wanted.sha256:
                pins[name] = wanted
            else:
                pins[name] = VerifiedPin(base_url=OBSERVED, sha256=observed)
                drifted.append(name)
            continue

        pins[name] = known
        if known.sha256 != wanted.sha256 or (
            known.base_url and known.base_url != wanted.base_url
        ):
            # A base_url difference with a matching digest is still drift: the
            # catalog has been re-pointed, and the next fetch of any sibling
            # file comes from somewhere else. Only compared when a real one was
            # recorded — an OBSERVED pin has no location to disagree about.
            drifted.append(name)

    if unreadable:
        return (
            DependencyState.UNKNOWN,
            pins,
            f"could not read {_names(unreadable)} to check {'it' if len(unreadable) == 1 else 'them'}",
        )

    if drifted:
        return (
            DependencyState.STALE,
            pins,
            f"{_names(drifted)} {'does' if len(drifted) == 1 else 'do'} not match "
            f"what this version expects",
        )

    return DependencyState.OK, pins, ""


def _names(names: list[str]) -> str:
    """Filenames for a sentence, bounded so a wide drift does not produce an
    unreadable line."""
    shown = sorted(names)
    if len(shown) > 3:
        return f"{', '.join(shown[:3])} and {len(shown) - 3} more"
    return ", ".join(shown)
