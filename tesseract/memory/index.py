"""MEMORY.md index manager.

Manages the curated hot index — add/remove/rebuild/evict entries.
Cap: 200 lines. Eviction uses 3-factor weighted score:
  importance * 0.4 + recency * 0.35 + frequency * 0.25
"""

from __future__ import annotations

import json
import logging
import threading
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.lib.atomic_replace import replace_with_retry
from tesseract.memory.types import MemoryFrontmatter, MemoryType

logger = logging.getLogger(__name__)

_HEADER = "# TESSERACT Memory Index\n"

_IMPORTANCE_WEIGHT = 0.4
_RECENCY_WEIGHT = 0.35
_FREQUENCY_WEIGHT = 0.25
_RECENCY_DECAY_DAYS = 30.0
_MAX_FREQUENCY_CAP = 10.0

# MEMORY.md has two writers on two cadences, and both are wanted: `MemoryIndex`
# writes on every save/update/promote so the file is current within the turn,
# and the librarian rewrites it nightly with counts and sections. They must not
# have two answers to how a row looks, so the row renderer lives here and the
# librarian calls it.
#
# A row is title, link and a hook — enough to decide whether to open the record
# without opening it. Nothing here is cut and no row carries an ellipsis: the
# hook is the summary, which is the memory's lead paragraph and therefore
# already the length its writer chose.
def resolve_link_path(store_dir: Path, fm: MemoryFrontmatter) -> str:
    """Store-relative POSIX path to `fm`'s real file.

    Curated sub-buckets (`user/people/`, `feedback/playbooks/`) make the
    canonical `{type}/{id}.md` guess wrong, so the file is located rather than
    assumed. Falls back to that guess only when it is genuinely absent, which
    keeps the index renderable on a half-deleted store.
    """
    hits = list(store_dir.rglob(f"{fm.id}.md"))
    if not hits:
        return f"{fm.type.value}/{fm.id}.md"
    return hits[0].relative_to(store_dir).as_posix()


def render_index_row(store_dir: Path, fm: MemoryFrontmatter, *, prefix: str = "") -> str:
    """One MEMORY.md row. The single answer to what a row looks like."""
    hook = " ".join((fm.summary or "").split())
    tail = " · ".join(part for part in (prefix, hook) if part)
    row = f"- [{fm.title}]({resolve_link_path(store_dir, fm)})"
    return f"{row} — {tail}" if tail else row


class MemoryIndex:
    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._path = store_dir / "MEMORY.md"
        self._access_log_path = store_dir / "events" / "access.jsonl"
        self._line_cap = 200
        # `MEMORY.md` and this dict are reachable from two threads: the
        # memory tools mutate them on the event loop, while `dream_cycle`
        # rewrites them from a worker thread. Unguarded, `_write` iterating
        # `_entries` while `add` inserts raises "dictionary changed size
        # during iteration", and two `write_text` calls interleave into a
        # torn file. Reentrant because `add` holds it across
        # `_evict_if_needed` and `_write`, which take it too.
        #
        # Same reasoning, and same shape, as `MemoryStore._fm_cache_lock`.
        self._lock = threading.RLock()
        self._entries: dict[str, tuple[MemoryFrontmatter, str]] = {}
        self._access_counts: Counter[str] = Counter()
        self._last_access: dict[str, datetime] = {}
        self._load_existing()
        self._load_access_counts()

    def _load_existing(self) -> None:
        if not self._path.exists():
            return
        text = self._path.read_text(encoding="utf-8")
        for line in text.splitlines():
            match = re.match(r"^- \[.+?\]\((.+?)\)", line)
            if match:
                rel_path = match.group(1)
                mem_id = Path(rel_path).stem
                self._entries[mem_id] = (None, line)

    def _load_access_counts(self) -> None:
        if not self._access_log_path.exists():
            return
        try:
            with self._access_log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        mem_id = entry.get("memory_id")
                        if mem_id and entry.get("action") == "read":
                            self._access_counts[mem_id] += 1
                            ts = entry.get("timestamp")
                            if ts:
                                try:
                                    dt = datetime.fromisoformat(ts)
                                    existing = self._last_access.get(mem_id)
                                    if existing is None or dt > existing:
                                        self._last_access[mem_id] = dt
                                except (ValueError, TypeError):
                                    pass
                    except json.JSONDecodeError:
                        continue
        except Exception:
            logger.warning("Failed to load access counts")

    def add(self, fm: MemoryFrontmatter) -> None:
        line = render_index_row(self._store_dir, fm)
        with self._lock:
            self._entries[fm.id] = (fm, line)
            self._evict_if_needed()
            self._write()

    def remove(self, memory_id: str) -> None:
        with self._lock:
            if memory_id in self._entries:
                del self._entries[memory_id]
                self._write()

    def load_ids(self) -> list[str]:
        with self._lock:
            return list(self._entries.keys())

    def load_raw(self) -> str:
        # Under the lock like the writers. On Windows `os.replace` fails
        # outright — `PermissionError: Access is denied` — if the file it is
        # replacing is open for reading, so an unsynchronised reader here
        # does not merely see a stale file, it breaks the write.
        with self._lock:
            if self._path.exists():
                return self._path.read_text(encoding="utf-8")
            return _HEADER

    def rebuild(self) -> None:
        with self._lock:
            self._rebuild_locked()

    def _rebuild_locked(self) -> None:
        self._entries.clear()
        subdirs = ["user", "feedback", "project", "reference", "conscience"]
        all_fms: list[MemoryFrontmatter] = []

        for subdir in subdirs:
            subdir_path = self._store_dir / subdir
            if not subdir_path.exists():
                continue
            # rglob, not glob: a memory in a curated sub-bucket is still a
            # memory, and a non-recursive walk cannot see one.
            for md_file in subdir_path.rglob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8")
                    if not text.startswith("---\n"):
                        continue
                    end = text.index("---\n", 4)
                    yaml_str = text[4:end]
                    yaml_dict = yaml.safe_load(yaml_str)
                    fm = MemoryFrontmatter.from_yaml_dict(yaml_dict)
                    all_fms.append(fm)
                except Exception:
                    logger.warning("Failed to parse %s during rebuild", md_file)

        all_fms.sort(
            key=lambda f: (self._eviction_score(f.id, f), f.created_at.isoformat()),
            reverse=True,
        )

        max_entries = self._line_cap - 2
        for fm in all_fms[:max_entries]:
            line = render_index_row(self._store_dir, fm)
            self._entries[fm.id] = (fm, line)

        self._write()

    def days_since_last_access(self, mem_id: str, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        last = self._last_access.get(mem_id)
        if last is None:
            return 365.0
        delta = now - last
        return max(delta.total_seconds() / 86400.0, 0.0)

    def _eviction_score(self, mem_id: str, fm: MemoryFrontmatter | None) -> float:
        """3-factor weighted score. Higher = more valuable = evicted last.

        importance * 0.4 + recency * 0.35 + frequency * 0.25
        """
        importance = fm.importance if fm else 5
        importance_weight = importance / 10.0

        days = self.days_since_last_access(mem_id)
        recency_weight = 1.0 / (1.0 + days / _RECENCY_DECAY_DAYS)

        access_count = self._access_counts.get(mem_id, 0)
        frequency_weight = min(access_count / _MAX_FREQUENCY_CAP, 1.0)

        return (
            importance_weight * _IMPORTANCE_WEIGHT
            + recency_weight * _RECENCY_WEIGHT
            + frequency_weight * _FREQUENCY_WEIGHT
        )

    def _evict_if_needed(self) -> None:
        max_entries = self._line_cap - 2
        if len(self._entries) <= max_entries:
            return

        scored: list[tuple[str, float]] = []
        for mem_id, (fm, _line) in self._entries.items():
            scored.append((mem_id, self._eviction_score(mem_id, fm)))

        scored.sort(key=lambda x: x[1])

        while len(self._entries) > max_entries and scored:
            evict_id, _ = scored.pop(0)
            del self._entries[evict_id]
            logger.info("Evicted %s from MEMORY.md (low composite score)", evict_id)

    def _write(self) -> None:
        # Reentrant: every caller already holds the lock, but taking it here
        # too means a future direct caller cannot reintroduce the race by
        # forgetting to.
        with self._lock:
            lines = [_HEADER, ""]
            for _mem_id, (_fm, line) in self._entries.items():
                lines.append(line)
            text = "\n".join(lines) + "\n"
            # Sibling + replace, so a reader never sees a half-written file:
            # the rename is atomic on both POSIX and Windows.
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            replace_with_retry(tmp, self._path)
