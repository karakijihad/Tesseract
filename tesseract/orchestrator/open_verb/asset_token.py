"""Signed asset references.

The Mirror can only render URLs, so a local file has to be reachable over the
loopback port to appear on a canvas. Serving a whole directory tree to do that
gives every process on the machine standing read access to it — and the trees
worth rendering sit next to `.env`, `config/` and `memory-store/`.

So nothing is served ambiently. `open` signs the exact path it just resolved,
and the endpoint serves only what carries a valid signature. Reach is exactly
the set of files the operator asked for, and the deny-list problem disappears.

The signature is an HMAC rather than a registry entry so a card survives a
restart: a persisted surface descriptor keeps working without the process
having to remember every path it ever opened.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from tesseract.paths import runtime_dir

_KEY_FILENAME = "asset-signing.key"
_KEY_BYTES = 32


def _key_path() -> Path:
    """Machine-local: never synced, never shipped. A new machine mints its own,
    which is correct — a signature from another install should not be honoured
    here."""
    return runtime_dir() / _KEY_FILENAME


# Keyed by key-path, not a bare module global: a test that repoints
# TESSERACT_HOME must get that install's key, not the previous one's.
_CACHE: dict[Path, bytes] = {}


def _read_key(path: Path) -> bytes | None:
    """A transient Windows sharing violation is not "no key". Treating it as
    one mints a replacement and silently invalidates every signature already
    handed out, so a read is retried before that conclusion is drawn."""
    for _ in range(3):
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            time.sleep(0.01)
            continue
        if len(existing) >= _KEY_BYTES:
            return existing
        return None
    return None


def _load_or_create_key() -> bytes:
    path = _key_path()
    cached = _CACHE.get(path)
    if cached is not None:
        return cached

    existing = _read_key(path)
    if existing is not None:
        _CACHE[path] = existing
        return existing

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_bytes(_KEY_BYTES)
    # O_EXCL on the FINAL path, not a temp file that is then renamed. A rename
    # would happily clobber a key another process had already published, and
    # every asset URL signed with it would stop verifying. Losing the race is
    # the normal case, not an error: whoever created it first wins, and we
    # adopt their key.
    #
    # Owner-only from the moment it exists — a readable key lets any local
    # account forge references and pull anything inside the read boundary.
    # On Windows the POSIX mode is advisory and the real protection is that
    # runtime/ sits inside the per-user install root; the flags still matter
    # for the non-Windows path and cost nothing here.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_key(path)
        if existing is None:
            raise
        _CACHE[path] = existing
        return existing
    try:
        os.write(fd, key)
    finally:
        os.close(fd)

    _CACHE[path] = key
    return key


def _canonical(path: str) -> bytes:
    """Windows paths are case-insensitive and accept either separator, so the
    same file can be spelled several ways. Normalise before signing or a valid
    reference fails to verify."""
    return os.path.normcase(os.path.normpath(str(path))).encode("utf-8", "surrogatepass")


def sign(path: str) -> str:
    return hmac.new(_load_or_create_key(), _canonical(path), hashlib.sha256).hexdigest()


def verify(path: str, signature: str) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(path), signature)
