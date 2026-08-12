"""Verified downloads of model artifacts from a pinned upstream source.

Shared by every model fetch script — the three voice lanes and the
reranker. Every artifact is named by a ``download:`` block in
``providers.yaml``: a ``base_url`` that already carries the upstream pin —
a commit sha for a HuggingFace repo, a release tag for a GitHub release —
and a ``files:`` map keyed by on-disk filename. Nothing here knows a model
name or a URL, so moving a pin is a config edit and never a code change.

A ``files:`` value is either a bare sha256, or a mapping of ``sha256`` plus
a ``source`` naming the upstream path when it differs from the on-disk
name. One shape covers both; there is no second fetch implementation.

Three invariants, each one load-bearing for a first run:

- **Never overwrite.** A file already on disk is the operator's, whatever
  its contents. Re-fetching is opt-in through ``force``.
- **Verify before install.** Bytes land in a ``.part`` sibling and are only
  renamed into place once the sha256 matches the pin. A mismatch removes
  the partial file and installs nothing, so a corrupted or substituted
  artifact can never become the model the runtime loads.
- **Never raise.** Every caller treats a missing model file as a supported
  degraded mode, and these run during provisioning and again on every
  launch, into a hidden console where a traceback goes nowhere. Failures
  are logged and swallowed; the return value carries the outcome.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 120.0
_CHUNK_BYTES = 1024 * 1024

# A URL up to (and not including) its query string. The trailing class excludes
# whitespace, quotes and the closing brackets a URL is usually reported inside,
# so the match ends where the URL does rather than eating the rest of a sentence.
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"<>)\]}]*?)\?[^\s'\"<>)\]}]*")

# The userinfo segment: everything between the scheme and the last `@` before
# the path starts. Greedy up to that `@`, so a password containing one of its
# own is redacted whole rather than half.
_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s'\"<>)\]}]*@")

# Set by the shell on every provisioning subprocess (`provision.rs::
# point_at_state_root`). Off by default so a hand-run
# `python -m tesseract.scripts.fetch_whisper_model` stays readable, and inert
# anywhere nobody is reading stdout.
_PROGRESS_ENV = "TESSERACT_PROVISION_PROGRESS"

# The shell's parser. Kept in one place on each side; the two are asserted
# against each other by `provision.rs::parse_progress_marker`'s unit test and
# this module's own.
_PROGRESS_MARKER = "TESSERACT_PROGRESS"

# Emit on EITHER threshold. Bytes alone would go silent on a slow link at the
# exact moment a user needs to see something moving; seconds alone would emit
# thousands of lines on a fast one. A stalled transfer therefore keeps
# re-reporting the same figure, which is the honest signal — the process is
# alive and the bytes are not arriving.
_PROGRESS_EVERY_BYTES = 4 * 1024 * 1024
_PROGRESS_EVERY_SECONDS = 1.0


def _progress_reporter(
    filename: str, expected: int | None
) -> Callable[..., None]:
    """A throttled byte reporter for one file, or a no-op when disabled.

    Writes to STDOUT, deliberately: every human-facing line in this module
    goes to the logger, which is stderr, so a 1.6 GB download cannot bury the
    four lines that say what actually happened. The shell reads the two
    streams for different purposes and never shows a marker verbatim.
    """
    if os.environ.get(_PROGRESS_ENV) != "1":
        return lambda received, force=False: None

    state: dict[str, Any] = {"bytes": -1, "at": 0.0, "off": False}

    def report(received: int, force: bool = False) -> None:
        if state["off"]:
            return
        now = time.monotonic()
        if (
            not force
            and received - state["bytes"] < _PROGRESS_EVERY_BYTES
            and now - state["at"] < _PROGRESS_EVERY_SECONDS
        ):
            return
        state["bytes"] = received
        state["at"] = now
        try:
            print(
                f"{_PROGRESS_MARKER} file={filename} received={received} "
                f"expected={expected if expected is not None else '-'}",
                flush=True,
            )
        except Exception:  # noqa: BLE001 — a closed stdout is not a failed download
            # Every call to this sits inside `_download_one`'s transfer `try`,
            # whose handler deletes the partial file and returns False. An
            # unguarded write meant a `BrokenPipeError` on the progress channel
            # — the shell exiting mid-fetch is enough — was indistinguishable
            # from a failed transfer and threw away good bytes. Reporting stops
            # for the rest of this file; the download continues.
            state["off"] = True

    return report


def _expected_bytes(response: Any) -> int | None:
    """The transfer size the server declared, or None when it did not.

    None is a supported answer and is passed through as such rather than
    guessed at: the catalog's `max_download_mb` is a refusal cap, not a size,
    and using it here would render a 1.6 GB model as a fraction of 2 GB.

    Total by construction — it cannot raise. This runs inside the download's
    own `try`, where anything thrown is reported as "could not download" and
    the model stays unavailable. A figure shown on a progress bar must never
    be able to cost the operator the artifact it was describing.
    """
    headers = getattr(response, "headers", None)
    raw = headers.get("content-length") if headers is not None else None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class PinnedSource:
    """One upstream location plus the digests of everything fetched from it.

    `files` is keyed by the name the file takes ON DISK, because that is
    what the runtime opens. `sources` carries the upstream path for any
    file whose remote name differs — the reranker's model is published as
    `onnx/model_quantized.onnx` but is loaded under the catalog's own
    `model:` filename. Absent from `sources` means the two are the same,
    which is the common case and the only one the voice lanes need.
    """

    base_url: str
    files: Mapping[str, str]
    max_download_mb: int
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def max_bytes(self) -> int:
        return self.max_download_mb * 1024 * 1024

    def url_for(self, filename: str) -> str:
        remote = self.sources.get(filename, filename)
        return f"{self.base_url.rstrip('/')}/{remote.lstrip('/')}"


def _unsafe_filename_reason(name: str) -> str | None:
    """Why `name` cannot be used as an on-disk filename, or None if it can.

    A ``files:`` key names a file INSIDE the destination directory. It is not
    a path, and the sha256 pin does not make it one: the digest guarantees the
    bytes are what the catalog promised, never that they land where the
    catalog was entitled to put them.

    Checked on the way in rather than at the join, so every consumer of a
    `PinnedSource` inherits the guarantee instead of repeating it.
    """
    if not name or name in (".", ".."):
        return "is empty or names a directory rather than a file"
    if "/" in name or "\\" in name:
        return "contains a path separator"
    if ":" in name:
        # `C:model.onnx` is drive-RELATIVE on Windows — NOT `is_absolute()`,
        # yet joining it discards the destination directory entirely. The
        # same character introduces an NTFS alternate data stream.
        return "contains a drive or stream separator"
    if "\x00" in name:
        return "contains a NUL byte"
    if PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute():
        return "is an absolute path"
    return None


def parse_download_block(raw: Any, *, where: str) -> PinnedSource | None:
    """Read a catalog ``download:`` block, or return None if it is unusable.

    Returns None rather than raising: a catalog entry with no (or a
    malformed) download block means "this artifact is not fetchable", which
    every caller already handles as the same degraded mode as a failed
    download. The reason is logged so a mis-typed pin is diagnosable
    instead of silently behaving like an absent one.
    """
    if not isinstance(raw, Mapping):
        logger.warning("%s: no download block — nothing to fetch", where)
        return None
    base_url = str(raw.get("base_url") or "").strip()
    files_raw = raw.get("files")
    max_mb = raw.get("max_download_mb")
    if not base_url:
        logger.warning("%s: download block has no base_url — cannot fetch safely", where)
        return None
    if not isinstance(files_raw, Mapping) or not files_raw:
        logger.warning(
            "%s: download block has no files map (filename -> sha256) — "
            "cannot fetch safely",
            where,
        )
        return None
    if not max_mb:
        logger.warning(
            "%s: download block has no max_download_mb cap — cannot fetch safely", where
        )
        return None
    # Two accepted value shapes per file: a bare sha256 (on-disk name is the
    # upstream name), or a mapping carrying `sha256` and an optional `source`
    # for the upstream path when it differs.
    files: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name, spec in files_raw.items():
        filename = str(name)
        unsafe = _unsafe_filename_reason(filename)
        if unsafe is not None:
            logger.warning(
                "%s: download entry %r %s — a files: key names a file inside "
                "the destination directory, never a path to anywhere else; "
                "refusing the whole block",
                where,
                filename,
                unsafe,
            )
            return None
        if isinstance(spec, Mapping):
            files[filename] = str(spec.get("sha256") or "").lower()
            remote = str(spec.get("source") or "").strip()
            if remote:
                sources[filename] = remote
        else:
            files[filename] = str(spec).lower()

    missing_digest = sorted(name for name, sha in files.items() if len(sha) != 64)
    if missing_digest:
        logger.warning(
            "%s: download entries %s have no 64-character sha256 — an unpinned "
            "download is refused outright",
            where,
            ", ".join(missing_digest),
        )
        return None
    return PinnedSource(
        base_url=base_url,
        files=files,
        max_download_mb=int(max_mb),
        sources=sources,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scrub_url_credentials(text: str) -> str:
    """Redact both places a URL can carry a secret, in every URL in `text`.

    A URL that reaches a log file can be a credential that reaches a log file,
    two different ways:

    - **the query string**, for a presigned CDN URL whose authorisation IS its
      query. HuggingFace redirects a model fetch to exactly such a URL, and an
      httpx exception carries the request URL in its message — so the failure
      path, not the happy one, is where it escapes;
    - **the userinfo segment**, for a `https://user:token@host/...` source. No
      shipped `download:` block uses one, but pointing a block at a private
      repository is precisely the case that needs a token.

    Truncating keeps the half worth reading — which host refused, which path —
    and discards the half that is a secret. This is the Python side of the rule
    the shell applies to its own log, which scrubs both.

    Query strings go first on purpose: the userinfo placeholder contains angle
    brackets, which the query pattern's URL character class excludes, so
    redacting userinfo first would stop the query pattern at the placeholder
    and leave the signature behind.
    """
    return _URL_USERINFO_RE.sub(r"\1<redacted>@", _URL_QUERY_RE.sub(r"\1?<redacted>", text))


def _download_one(url: str, dest: Path, expected_sha256: str, max_bytes: int, label: str) -> bool:
    """Stream `url` into `dest`, installing only on a digest match."""
    import httpx

    # httpx logs every request line at INFO, and a HuggingFace download
    # redirects to a signed CDN URL whose query string is ~700 characters of
    # policy and signature. These scripts run with INFO on so their own
    # progress lands in shell.log; without this the useful lines are buried
    # under one URL per file.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    received = 0
    oversized = False
    try:
        logger.info("%s: downloading %s", label, dest.name)
        # The whole transfer sits inside this `with`, and the rename sits
        # outside it: Windows refuses to rename a file that still has an
        # open handle, so installing from inside the writer would fail on
        # exactly the platform this ships on.
        with httpx.stream(
            "GET", url, follow_redirects=True, timeout=_TIMEOUT_SECONDS
        ) as resp:
            resp.raise_for_status()
            report = _progress_reporter(dest.name, _expected_bytes(resp))
            # A zero at the start so the shell can name the file it is about
            # to spend minutes on, rather than showing nothing until the
            # first threshold is crossed.
            report(0, force=True)
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(_CHUNK_BYTES):
                    received += len(chunk)
                    if received > max_bytes:
                        # Checked while streaming rather than from the
                        # Content-Length header: a wrong or absent header
                        # would let an endpoint gone wrong fill the disk
                        # long before the digest check could reject it.
                        oversized = True
                        break
                    digest.update(chunk)
                    fh.write(chunk)
                    report(received)
            report(received, force=True)
    except Exception as exc:  # noqa: BLE001 — offline/upstream/disk are all the same outcome
        logger.warning(
            "%s: could not download %s (%s) — it stays unavailable until this "
            "succeeds; the app continues without it.",
            label,
            dest.name,
            scrub_url_credentials(str(exc)),
        )
        tmp.unlink(missing_ok=True)
        return False

    if oversized:
        logger.warning(
            "%s: %s exceeded the catalog size cap (%d MB) — nothing installed.",
            label,
            dest.name,
            max_bytes // (1024 * 1024),
        )
        tmp.unlink(missing_ok=True)
        return False

    actual = digest.hexdigest()
    if actual != expected_sha256:
        logger.warning(
            "%s: checksum mismatch for %s (expected %s, got %s) — refusing to "
            "install a corrupted or tampered file.",
            label,
            dest.name,
            expected_sha256,
            actual,
        )
        tmp.unlink(missing_ok=True)
        return False

    try:
        tmp.replace(dest)
    except OSError as exc:
        logger.warning("%s: could not install %s: %s", label, dest.name, exc)
        tmp.unlink(missing_ok=True)
        return False
    logger.info(
        "%s: wrote %s (%d bytes, sha256 verified)", label, dest, dest.stat().st_size
    )
    return True


def ensure_files(
    source: PinnedSource, dest_dir: Path, *, label: str, force: bool = False
) -> bool:
    """Fetch every pinned file missing from `dest_dir`.

    Returns True iff every file is present and verified afterwards. An
    already-present file is left untouched and counts as present without
    being re-hashed — re-reading a 1.6 GB model on every launch to confirm
    a digest we already checked when we wrote it would cost more than it
    protects, and the operator is free to put their own file there.

    With `force`, present files are re-downloaded and replaced.
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = dest_dir.resolve()
    except OSError as exc:
        logger.warning("%s: cannot create %s: %s", label, dest_dir, exc)
        return False

    complete = True
    for filename, expected in source.files.items():
        # The SAME name guard the parser applies. `ensure_files` takes a
        # `PinnedSource`, which is a public frozen dataclass anyone can build
        # directly, so a containment check on the joined path is not enough on
        # its own: `model.onnx:stream` resolves to a parent that passes
        # containment while naming an NTFS alternate data stream.
        unsafe = _unsafe_filename_reason(str(filename))
        if unsafe is not None:
            logger.warning("%s: refusing %r — it %s", label, filename, unsafe)
            complete = False
            continue

        lexical = dest_dir / str(filename)
        # Presence is decided on the LEXICAL path, and BEFORE containment.
        # This module's first invariant is that a file already on disk is the
        # operator's, whatever it is — and an operator who symlinked a 1.6 GB
        # model in from another drive has a `models/model.onnx` whose target
        # is deliberately outside `dest_dir`. Checking containment first turned
        # that documented, supported setup into a refusal.
        if lexical.exists() and not force:
            logger.info("%s: %s already present, skipping", label, filename)
            continue

        # Containment governs the WRITE, which is the only thing it protects.
        # `parse_download_block` already refuses a key that could escape, but
        # `PinnedSource` is a public dataclass and nothing stops a caller
        # constructing one directly.
        try:
            dest = lexical.resolve()
            contained = dest.parent == resolved_dir
        except OSError:
            contained = False
        if not contained:
            logger.warning(
                "%s: refusing %r — it resolves outside %s",
                label,
                filename,
                resolved_dir,
            )
            complete = False
            continue
        # The RESOLVED destination is what gets written, not the lexical join.
        # Handing the unresolved path downstream would let the directory be
        # replaced by a junction after the check and before `.part` is opened,
        # so the path validated and the path written are two different places.
        if not _download_one(
            source.url_for(filename), dest, expected, source.max_bytes, label
        ):
            complete = False
    return complete


def files_present(source: PinnedSource, dest_dir: Path) -> bool:
    """Whether every pinned file already exists in `dest_dir`.

    The cheap existence check the Settings panel renders from, so a lane can
    say "model files missing" without touching the network.
    """
    return all((dest_dir / name).exists() for name in source.files)
