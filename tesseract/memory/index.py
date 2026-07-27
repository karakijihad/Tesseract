"""MEMORY.md index manager.

Manages the curated hot index — add/remove/rebuild/evict entries.
Cap: 200 lines. Eviction uses 3-factor weighted score:
  importance * 0.4 + recency * 0.35 + frequency * 0.25
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.types import MemoryFrontmatter, MemoryType

logger = logging.getLogger(__name__)

_HEADER = "# TESSERACT Memory Index\n"

_IMPORTANCE_WEIGHT = 0.4
_RECENCY_WEIGHT = 0.35
_FREQUENCY_WEIGHT = 0.25
_RECENCY_DECAY_DAYS = 30.0
_MAX_FREQUENCY_CAP = 10.0


class MemoryIndex:
    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._path = store_dir / "MEMORY.md"
        self._access_log_path = store_dir / "events" / "access.jsonl"
        self._line_cap = 200
        self._entries: dict[str, tuple[MemoryFrontmatter, str]] = {}
        self._access_counts: Counter[str] = Counter()
        self._last_access: dict[str, datetime] = {}
        self._load_existing()
        self._load_access_counts()

    def _type_to_subdir(self, mem_type: MemoryType) -> str:
        return mem_type.value

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
        subdir = self._type_to_subdir(fm.type)
        rel_path = f"{subdir}/{fm.id}.md"
        summary = fm.summary or fm.title
        line = f"- [{fm.title}]({rel_path}) — {summary}"
        self._entries[fm.id] = (fm, line)
        self._evict_if_needed()
        self._write()

    def remove(self, memory_id: str) -> None:
        if memory_id in self._entries:
            del self._entries[memory_id]
            self._write()

    def load_ids(self) -> list[str]:
        return list(self._entries.keys())

    def load_raw(self) -> str:
        if self._path.exists():
            return self._path.read_text(encoding="utf-8")
        return _HEADER

    def rebuild(self) -> None:
        self._entries.clear()
        subdirs = ["user", "feedback", "project", "reference", "conscience"]
        all_fms: list[MemoryFrontmatter] = []

        for subdir in subdirs:
            subdir_path = self._store_dir / subdir
            if not subdir_path.exists():
                continue
            for md_file in subdir_path.glob("*.md"):
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
            subdir = self._type_to_subdir(fm.type)
            rel_path = f"{subdir}/{fm.id}.md"
            summary = fm.summary or fm.title
            line = f"- [{fm.title}]({rel_path}) — {summary}"
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
        lines = [_HEADER, ""]
        for _mem_id, (_fm, line) in self._entries.items():
            lines.append(line)
        text = "\n".join(lines) + "\n"
        self._path.write_text(text, encoding="utf-8")
