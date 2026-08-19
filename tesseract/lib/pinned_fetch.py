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

Not raising is not the same as not trying again, and conflating the two is
what left a 311 MB model unfetched after one dropped connection. A transfer
that fails part-way is retried, bounded by the project-wide
``MAX_CONSECUTIVE_FAILURES``, and resumed from the bytes already on disk
whenever the server honours a ``Range`` request. What is *not* retried is
anything deterministic — a 404, the size cap, and above all a digest
mismatch: re-fetching a file that failed verification must never be allowed
to eventually succeed.
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

# The project-wide circuit breaker, spelled the way every other retry loop
# here spells it. Three attempts, not three retries: a transfer that has
# dropped three times is a link that is not going to deliver 311 MB today,
# and provisioning has other files to fetch.
MAX_CONSECUTIVE_FAILURES = 3

# Waited between attempts, doubling. Short enough that a blip costs seconds
# and long enough that a rate-limited or restarting upstream gets a moment.
_BACKOFF_SECONDS = 2.0

# Status codes worth trying again. Everything else in the 4xx range is the
# server answering the question — the file is not there, or we may not have
# it — and asking again cannot change the answer.
_RETRYABLE_STATUS = frozenset({408, 425, 429})

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
    # The cap is the last thing that can throw here, and this function's whole
    # contract is that it RETURNS None rather than raising — every caller
    # treats an unusable block as "not fetchable", which is the same degraded
    # mode as a failed download. A hand-edited `max_download_mb: "2 GB"` would
    # otherwise take the fetch script down instead of skipping one artifact.
    try:
        cap = int(max_mb)
    except (TypeError, ValueError):
        logger.warning(
            "%s: download block has a max_download_mb that is not a number (%r) — "
            "cannot fetch safely",
            where,
            max_mb,
        )
        return None
    if cap <= 0:
        logger.warning(
            "%s: download block has a max_download_mb of %d — a cap that refuses "
            "everything is not a cap", where, cap
        )
        return None
    return PinnedSource(
        base_url=base_url,
        files=files,
        max_download_mb=cap,
        sources=sources,
    )


def _discard(tmp: Path, label: str) -> None:
    """Remove a partial file, and never raise doing it.

    `missing_ok=True` swallows only `FileNotFoundError`. Every other `OSError`
    — a `.part` held open by a virus scanner, a read-only directory — still
    propagates, and these calls sit on the terminal paths of `_download_one`,
    outside any handler. The module's first invariant is that a failed fetch
    is a return value rather than an exception; an unlink that could throw put
    that in the hands of the filesystem.
    """
    try:
        tmp.unlink(missing_ok=True)
    except OSError as exc:
        logger.info("%s: could not remove %s (%s)", label, tmp.name, exc)


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


def _is_transient(exc: BaseException) -> bool:
    """Whether trying `exc` again could plausibly produce a different answer.

    The split this module got wrong for a long time. A dropped connection, a
    read timeout, a reset or a 5xx describes the link or the moment, not the
    file — those are worth another attempt. A 404, a 403 or a malformed URL
    describes the request, and repeating it just spends the operator's time
    reaching the same conclusion three times.

    An exception this does not recognise is treated as terminal on purpose:
    an unexpected failure inside a loop that retries it is how a first run
    turns into a hang. That includes httpx not being importable at all,
    which is why the import is guarded here as well as everywhere else in
    this module: a classifier that raises would defeat the never-raise
    invariant it is called from.
    """
    try:
        import httpx
    except ImportError:
        return False

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRYABLE_STATUS or 500 <= status < 600
    # Every network-level failure httpx raises — timeouts, connection and
    # read errors, and `RemoteProtocolError`, which is the exact
    # "Server disconnected without sending a response" this was built for.
    return isinstance(exc, httpx.TransportError)


def _resumable_prefix(tmp: Path) -> tuple[Any, int]:
    """The digest and length of the partial bytes already written.

    Re-read from disk rather than carried over from the failed attempt: the
    handle was closed by an exception, and what is on disk is the only thing
    that can be resumed from. Hashing 300 MB costs about a second and buys
    back the 300 MB that would otherwise be fetched again.

    An unreadable partial answers `(fresh digest, 0)`, i.e. start over.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with tmp.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return hashlib.sha256(), 0
    return digest, size


def _attempt_transfer(
    url: str, tmp: Path, name: str, max_bytes: int, resume_from: int
) -> tuple[str, int]:
    """One pass at the transfer, returning `(outcome, bytes on disk)`.

    The outcome is the sha256 of everything written, or the literal
    ``"oversized"`` when the size cap was hit; a transport failure is raised
    for the caller to classify. Nothing here installs anything — the digest
    check and the rename belong to the caller, which is what keeps a retry
    from ever being able to install a file that failed verification.
    """
    import httpx

    digest = hashlib.sha256()
    received = 0
    headers: dict[str, str] = {}
    if resume_from:
        digest, received = _resumable_prefix(tmp)
        if received:
            headers["Range"] = f"bytes={received}-"

    # The whole transfer sits inside this `with`, and the rename sits
    # outside it: Windows refuses to rename a file that still has an
    # open handle, so installing from inside the writer would fail on
    # exactly the platform this ships on.
    with httpx.stream(
        "GET", url, headers=headers, follow_redirects=True, timeout=_TIMEOUT_SECONDS
    ) as resp:
        resp.raise_for_status()
        # 206 is the server agreeing to continue where we stopped. Anything
        # else — including a 200 from a server that ignored the header — is
        # the whole file arriving again, so the bytes on disk are discarded
        # rather than prepended to a second copy of themselves.
        resuming = bool(headers) and getattr(resp, "status_code", 200) == 206
        if not resuming:
            digest = hashlib.sha256()
            received = 0
        expected = _expected_bytes(resp)
        if resuming and expected is not None:
            expected += received
        report = _progress_reporter(name, expected)
        # A zero at the start so the shell can name the file it is about
        # to spend minutes on, rather than showing nothing until the
        # first threshold is crossed. On a resume it is the offset, so the
        # bar picks up where it left off instead of snapping back to zero.
        report(received, force=True)
        with tmp.open("ab" if resuming else "wb") as fh:
            for chunk in resp.iter_bytes(_CHUNK_BYTES):
                received += len(chunk)
                if received > max_bytes:
                    # Checked while streaming rather than from the
                    # Content-Length header: a wrong or absent header
                    # would let an endpoint gone wrong fill the disk
                    # long before the digest check could reject it.
                    return "oversized", received
                digest.update(chunk)
                fh.write(chunk)
                report(received)
        report(received, force=True)
    return digest.hexdigest(), received


def _download_one(url: str, dest: Path, expected_sha256: str, max_bytes: int, label: str) -> bool:
    """Stream `url` into `dest`, installing only on a digest match.

    Retries a transient failure up to `MAX_CONSECUTIVE_FAILURES` attempts,
    resuming from the partial file when the server allows it. Terminal
    failures — the size cap, a digest mismatch, a 404 — stop on the spot.

    A `.part` left over from an earlier RUN is not resumed: it may be bytes
    of a different pin published under the same filename, and while the
    digest check would catch that, it would catch it after a full download.
    Resume applies within one call, where the partial's provenance is known.
    """
    # httpx logs every request line at INFO, and a HuggingFace download
    # redirects to a signed CDN URL whose query string is ~700 characters of
    # policy and signature. These scripts run with INFO on so their own
    # progress lands in shell.log; without this the useful lines are buried
    # under one URL per file.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    tmp = dest.with_name(dest.name + ".part")
    logger.info("%s: downloading %s", label, dest.name)
    resume_from = 0
    for attempt in range(1, MAX_CONSECUTIVE_FAILURES + 1):
        try:
            outcome, _received = _attempt_transfer(
                url, tmp, dest.name, max_bytes, resume_from
            )
        except Exception as exc:  # noqa: BLE001 — offline/upstream/disk are all the same outcome
            transient = _is_transient(exc)
            retrying = transient and attempt < MAX_CONSECUTIVE_FAILURES
            logger.warning(
                "%s: %s %s on attempt %d of %d (%s)%s",
                label,
                dest.name,
                "was interrupted" if transient else "could not be downloaded",
                attempt,
                MAX_CONSECUTIVE_FAILURES,
                scrub_url_credentials(str(exc)),
                "" if retrying else " — it stays unavailable until this "
                "succeeds; the app continues without it.",
            )
            if not retrying:
                _discard(tmp, label)
                return False
            # The partial stays on disk precisely so the next attempt can
            # continue it. `_attempt_transfer` discards it itself if the
            # server will not resume.
            try:
                resume_from = tmp.stat().st_size
            except OSError:
                resume_from = 0
            if resume_from:
                logger.info(
                    "%s: retrying %s from %.1f MB already on disk",
                    label,
                    dest.name,
                    resume_from / (1024 * 1024),
                )
            time.sleep(_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            continue

        if outcome == "oversized":
            logger.warning(
                "%s: %s exceeded the catalog size cap (%d MB) — nothing installed.",
                label,
                dest.name,
                max_bytes // (1024 * 1024),
            )
            _discard(tmp, label)
            return False

        if outcome != expected_sha256:
            # Deterministic, and the one failure where retrying would be
            # actively wrong: a second fetch of a corrupted or substituted
            # artifact must never be allowed to eventually "succeed".
            logger.warning(
                "%s: checksum mismatch for %s (expected %s, got %s) — refusing to "
                "install a corrupted or tampered file. Not retried: the same "
                "bytes would fail the same check.",
                label,
                dest.name,
                expected_sha256,
                outcome,
            )
            _discard(tmp, label)
            return False

        try:
            tmp.replace(dest)
        except OSError as exc:
            logger.warning("%s: could not install %s: %s", label, dest.name, exc)
            _discard(tmp, label)
            return False
        logger.info(
            "%s: wrote %s (%d bytes, sha256 verified)", label, dest, dest.stat().st_size
        )
        return True

    return False  # unreachable: every path in the loop returns or continues


def _discard_stale_partial(dest: Path, label: str) -> None:
    """Remove the `.part` wreckage of a transfer that was killed outright.

    Every failure path inside `_download_one` cleans up after itself, but a
    process that is KILLED mid-transfer — provisioning cancelled, the machine
    shut down — has no failure path to run. What it leaves is up to 311 MB of
    a file that will never be resumed: resume is within one call by design, so
    the next attempt truncates rather than continues.

    Called on the SKIP branch, which is where it matters. Once the real file
    is installed, anything beside it under `.part` is litter, and litter of
    this size is the operator's disk.
    """
    partial = dest.with_name(dest.name + ".part")
    try:
        size = partial.stat().st_size
    except OSError:
        return
    _discard(partial, label)
    if partial.exists():
        return  # `_discard` already said why
    logger.info(
        "%s: removed %s left by an interrupted download (%.1f MB reclaimed)",
        label,
        partial.name,
        size / (1024 * 1024),
    )


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
            _discard_stale_partial(lexical, label)
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
