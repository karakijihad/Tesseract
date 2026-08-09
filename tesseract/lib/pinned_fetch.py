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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 120.0
_CHUNK_BYTES = 1024 * 1024


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
    except Exception as exc:  # noqa: BLE001 — offline/upstream/disk are all the same outcome
        logger.warning(
            "%s: could not download %s (%s) — it stays unavailable until this "
            "succeeds; the app continues without it.",
            label,
            dest.name,
            exc,
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
    except OSError as exc:
        logger.warning("%s: cannot create %s: %s", label, dest_dir, exc)
        return False

    complete = True
    for filename, expected in source.files.items():
        dest = dest_dir / filename
        if dest.exists() and not force:
            logger.info("%s: %s already present, skipping", label, filename)
            continue
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
