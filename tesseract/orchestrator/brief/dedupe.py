"""URL dedupe state for the world-digest.

State file: ``memory-store/daily/briefs/_dedupe.json`` (atomic-rewritten).
Tracked-topics schema::

    {
      "url_hashes": {"<sha256>": "YYYY-MM-DD", ...},
      "last_pruned_at": "<iso-utc>"
    }

A URL counts as "seen" when its sha256 is in ``url_hashes`` AND the stored
date is within the topic's ``dedupe_window_days``. After every brief,
:meth:`DedupeStore.prune` drops entries older than the widest window
across all topics.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path


def hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class DedupeStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._url_hashes: dict[str, str] = {}
        self._last_pruned_at: str = ""
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return
        hashes = raw.get("url_hashes")
        if isinstance(hashes, dict):
            self._url_hashes = {
                str(k): str(v) for k, v in hashes.items() if isinstance(v, str)
            }
        last = raw.get("last_pruned_at")
        if isinstance(last, str):
            self._last_pruned_at = last

    def is_seen(self, url: str, window_days: int, today: date) -> bool:
        stored = self._url_hashes.get(hash_url(url))
        if stored is None:
            return False
        try:
            stored_date = date.fromisoformat(stored)
        except ValueError:
            return False
        return (today - stored_date).days < max(1, window_days)

    def mark_seen(self, url: str, today: date) -> None:
        self._url_hashes[hash_url(url)] = today.isoformat()

    def prune(self, max_window_days: int, today: date) -> int:
        """Drop entries older than ``max_window_days``. Returns the count
        removed. Cheap to call after each brief — total entries scale with
        # of fresh URLs over the widest window."""
        cutoff = today
        keep: dict[str, str] = {}
        dropped = 0
        for digest, stamp in self._url_hashes.items():
            try:
                stored_date = date.fromisoformat(stamp)
            except ValueError:
                dropped += 1
                continue
            if (cutoff - stored_date).days < max(1, max_window_days):
                keep[digest] = stamp
            else:
                dropped += 1
        self._url_hashes = keep
        self._last_pruned_at = datetime.now(timezone.utc).isoformat()
        return dropped

    def save(self) -> None:
        """Atomic write — write to ``<path>.tmp`` then ``os.replace``."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "url_hashes": self._url_hashes,
            "last_pruned_at": self._last_pruned_at,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(self._path))


__all__ = ["DedupeStore", "hash_url"]
