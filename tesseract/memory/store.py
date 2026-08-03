"""Canonical memory file store.

Read/write/delete/list .md files in memory-store/. Every file has YAML
frontmatter validated by MemoryFrontmatter. Access and write events are
logged to events/*.jsonl.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.related_block import RelatedItem, replace_related_block
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.memory.what_not_to_save import WhatNotToSave

logger = logging.getLogger(__name__)

# Layer A — operator-directives section in the system prompt.
# Floor 6 keeps load-bearing rules and drops trivial corrections.
DIRECTIVES_IMPORTANCE_FLOOR = 6


def extract_wikilinks(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def _publish_bus_event(event: str, payload: dict) -> None:
    """Publish to the orchestrator background bus, log (don't re-raise) on failure.

    The orchestrator is optional — if the module fails to import, no-op.
    Runtime errors in publish() are demoted to debug-level so a transient
    bus issue can't break memory writes, but they do show up in logs for
    triage instead of being silently swallowed.
    """
    try:
        from tesseract.orchestrator.background_event_bus import get_background_bus
    except ImportError:
        return
    try:
        get_background_bus().publish(event, payload)
    except Exception:
        logger.debug("background_bus publish %r failed", event, exc_info=True)


def _inject_kind_tag(frontmatter: MemoryFrontmatter) -> MemoryFrontmatter:
    """Return a copy of ``frontmatter`` whose ``tags`` list is led by
    the record's MemoryType value (``feedback`` / ``user`` / ``project`` /
    ``reference`` / ``conscience``).

    The kind tag is the load-bearing addition for AU-16 — Obsidian's
    graph view color groups key off ``tag:#<kind>``. Operator-set tags
    survive after the kind tag. The function is idempotent — calling
    twice yields the same frontmatter.
    """
    kind = frontmatter.type.value
    existing = list(frontmatter.tags or [])
    if existing and existing[0] == kind:
        return frontmatter
    deduped: list[str] = [kind]
    seen: set[str] = {kind}
    for tag in existing:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return frontmatter.model_copy(update={"tags": deduped})


RECORD_SUBDIRS = ("user", "feedback", "project", "reference", "conscience")


def list_frontmatter(
    store_dir: Path, type_filter: MemoryType | None = None
) -> list[MemoryFrontmatter]:
    """Every parseable memory record under ``store_dir``, frontmatter only.

    A module-level function rather than a method because constructing a
    ``MemoryStore`` calls ``_ensure_dirs``, which creates the store tree. Read-
    only callers — anything answering "what is in here" rather than writing to
    it — must be able to ask without bringing the tree into existence.
    """
    subdirs = (
        (type_filter.value,) if type_filter else RECORD_SUBDIRS
    )
    results: list[MemoryFrontmatter] = []
    for subdir in subdirs:
        subdir_path = store_dir / subdir
        if not subdir_path.exists():
            continue
        # rglob walks operator sub-buckets (`reference/people/`, etc.)
        # so files dropped into a new folder are picked up without a
        # schema change. The frontmatter `type` still routes writes;
        # sub-buckets are organizational only.
        for md_file in subdir_path.rglob("*.md"):
            # Skip pure operator docs (README.md, INDEX.md) silently —
            # they live alongside memory records as folder-level
            # documentation and intentionally carry no frontmatter.
            # Files WITH a malformed frontmatter still log a warning
            # via the except below.
            try:
                with md_file.open("r", encoding="utf-8") as f:
                    first = f.readline()
                if first.strip() != "---":
                    continue
                text = MemoryStore._read_frontmatter_block(md_file)
                results.append(MemoryStore._parse_frontmatter_only(text))
            except Exception:
                logger.warning("Failed to parse %s", md_file)
    return results


class MemoryStore:
    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._wnts = WhatNotToSave(store_dir=store_dir)
        self._ensure_dirs()

    @property
    def store_dir(self) -> Path:
        """Public read-only view of the store root. Callers that need to
        write forensic events (memory_save type_mismatch guard, librarian
        heartbeat log) derive paths from here.
        """
        return self._store_dir

    def _ensure_dirs(self) -> None:
        # tars-reboot memory types: user / feedback / project / reference /
        # conscience. derived/ holds FAISS + FTS artifacts; events/ holds the
        # write/access audit log. daily/ (F1 2026-04-20) is the raw capture
        # layer — the librarian promotes entries from daily/ into the
        # canonical subdirs on heartbeat. Phase-1 identity/ and memory/
        # (retired 2026-04-17) are left alone; MemoryType enum has no
        # entries for them.
        for subdir in [
            "user", "feedback", "project", "reference", "conscience",
            "daily", "derived", "events",
        ]:
            (self._store_dir / subdir).mkdir(parents=True, exist_ok=True)

    def _type_to_subdir(self, mem_type: MemoryType) -> str:
        return mem_type.value

    def find_file(self, memory_id: str) -> Path | None:
        # Recursive lookup so operator-curated sub-buckets (e.g.
        # `reference/people/`, `project/sprints/`) are discoverable
        # without code changes — drop a folder in, files inside become
        # readable/searchable on the next call.
        target = f"{memory_id}.md"
        for subdir in ["user", "feedback", "project", "reference", "conscience"]:
            base = self._store_dir / subdir
            if not base.exists():
                continue
            direct = base / target
            if direct.exists():
                return direct
            for path in base.rglob(target):
                if path.is_file():
                    return path
        return None

    def log_event(self, filename: str, entry: dict) -> None:
        """Append a forensic event to `events/<filename>` with an auto timestamp.

        Public so external callers (memory_save's type_mismatch guard) can
        route through the same JSONL sink instead of writing directly.
        """
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        events_dir = self._store_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / filename
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def write(
        self,
        frontmatter: MemoryFrontmatter,
        body: str,
        *,
        subdir_override: str | None = None,
        skip_wnts_check: bool = False,
    ) -> bool:
        # `skip_wnts_check` is the trusted-internal-caller escape hatch.
        # Cascade / scrub rewrite *existing* entries to keep frontmatter
        # consistent after a delete; their new body may legitimately fall
        # below the trivial-body floor (Related block stripped, operator
        # prose was already terse) and WhatNotToSave must not block the
        # repair. Operator-facing save paths leave the default in place.
        if not skip_wnts_check and not self._wnts.should_save(body):
            self.log_event("writes.jsonl", {
                "memory_id": frontmatter.id,
                "type": frontmatter.type.value,
                "title": frontmatter.title,
                "status": "blocked",
                "reason": self._wnts.last_reason or "what_not_to_save",
            })
            logger.info("Memory %s blocked by %s", frontmatter.id, self._wnts.last_reason or "what_not_to_save")
            return False

        if subdir_override is not None:
            target_subdir = self._validate_relative_path(subdir_override)
            target_subdir.mkdir(parents=True, exist_ok=True)
            path = target_subdir / f"{frontmatter.id}.md"
        else:
            # Preserve existing location so updates/auto-link/librarian
            # rewrites to a memory living in a sub-bucket (e.g.
            # `reference/people/`) don't relocate it back to the type root.
            # Only falls back to the type default when the file is brand-new.
            existing_path = self.find_file(frontmatter.id)
            if existing_path is not None:
                path = existing_path
            else:
                subdir = self._type_to_subdir(frontmatter.type)
                path = self._store_dir / subdir / f"{frontmatter.id}.md"

        # AU-16 — every memory record gets a leading `kind` tag matching
        # its MemoryType so the Obsidian graph view's color groups fire
        # on the canonical store directly (no separate wiki mirror).
        # Operator-set tags survive AFTER the kind tag. Idempotent on
        # repeat writes — set semantics enforced by `_inject_kind_tag`.
        frontmatter = _inject_kind_tag(frontmatter)
        yaml_dict = frontmatter.to_yaml_dict()
        content = "---\n" + yaml.dump(yaml_dict, default_flow_style=False, sort_keys=False) + "---\n\n" + body

        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

        self.log_event("writes.jsonl", {
            "memory_id": frontmatter.id,
            "type": frontmatter.type.value,
            "title": frontmatter.title,
            "status": "written",
        })
        logger.info("Memory %s written to %s", frontmatter.id, path)
        _publish_bus_event("memory_written", {"id": frontmatter.id, "source": frontmatter.type.value})
        return True

    def update_body(self, memory_id: str, new_body: str) -> bool:
        """Refresh an existing memory's body + `updated_at`.

        Returns False when the id is unknown or `write()` blocks on
        WhatNotToSave. Logs a `status: "updated", reason: "cosine_merge"`
        event alongside the atomic-write event so the forensic log shows
        both the body replace and the dedupe-driven rationale.
        """
        existing = self.read(memory_id, log_access=False)
        if existing is None:
            return False
        fm, _ = existing
        fm = fm.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        ok = self.write(fm, new_body)
        if ok:
            self.log_event("writes.jsonl", {
                "memory_id": memory_id,
                "type": fm.type.value,
                "title": fm.title,
                "status": "updated",
                "reason": "cosine_merge",
            })
        return ok

    def read(self, memory_id: str, log_access: bool = True) -> tuple[MemoryFrontmatter, str] | None:
        path = self.find_file(memory_id)
        if path is None:
            return None

        text = path.read_text(encoding="utf-8")
        fm, body = self._parse_file(text)

        if log_access:
            self.log_event("access.jsonl", {
                "memory_id": memory_id,
                "action": "read",
            })
        return fm, body

    def list_all(self, type_filter: MemoryType | None = None) -> list[MemoryFrontmatter]:
        return list_frontmatter(self._store_dir, type_filter)

    def list_active_directives(
        self,
        *,
        importance_floor: int = DIRECTIVES_IMPORTANCE_FLOOR,
        types: tuple[MemoryType, ...] = (MemoryType.FEEDBACK, MemoryType.USER),
    ) -> list[MemoryFrontmatter]:
        """Active operator-directive records ranked for the system prompt.

        Includes ``feedback`` and ``user`` records by default — the operator
        often saves a durable preference under ``user`` (e.g.
        ``mem_66d4e50b`` "Inline preview by default") rather than ``feedback``.
        Both shapes describe rules TARS should obey across sessions, so both
        flow into the Operator Directives section.

        Filter: ``type in types``, ``stability == active``, ``importance >= floor``.
        Sort: importance desc, then created_at desc (newest tiebreak).
        Dedup: walk the sorted list — a record is dropped when its id is
        already kept (exact duplicate) OR when it has no slug AND its
        ``auto_links`` set intersects the kept id-set (librarian-merged
        variant of a rule already represented). Slug-keyed records describe
        distinct operator decisions and survive the auto_links check
        unconditionally — cross-references between two slug-keyed records
        are nearly always topical, not duplicate-markers. If two records
        share the same slug (no write-side uniqueness enforcement), both
        survive — duplication is preferable to silent eviction.
        """
        records: list[MemoryFrontmatter] = []
        for mt in types:
            records.extend(
                fm for fm in self.list_all(mt)
                if fm.stability == Stability.ACTIVE and fm.importance >= importance_floor
            )
        records.sort(
            key=lambda fm: (-fm.importance, -fm.created_at.timestamp()),
        )
        kept: list[MemoryFrontmatter] = []
        kept_ids: set[str] = set()
        for fm in records:
            if fm.id in kept_ids:
                continue
            related = set(fm.auto_links or [])
            # Only no-slug records can be auto-deduped via auto_links — a
            # slug marks a distinct operator decision (unique-by-construction)
            # and auto_links from it are cross-refs, not duplicate markers.
            if not fm.slug and related & kept_ids:
                continue
            kept.append(fm)
            kept_ids.add(fm.id)
            # Propagate outbound auto_links into kept_ids only for no-slug
            # records — when a slug-keyed record is kept, its auto_links
            # point at other distinct decisions that must remain eligible.
            if not fm.slug:
                kept_ids.update(related)
        return kept

    @staticmethod
    def _read_frontmatter_block(path: Path) -> str:
        """Read only the YAML frontmatter block, not the full file body."""
        with path.open("r", encoding="utf-8") as f:
            lines: list[str] = []
            first_line = f.readline()
            if first_line.strip() != "---":
                raise ValueError("Memory file missing YAML frontmatter")
            lines.append(first_line)
            for line in f:
                lines.append(line)
                if line.strip() == "---":
                    break
            return "".join(lines)

    def delete(self, memory_id: str) -> bool:
        path = self.find_file(memory_id)
        if path is None:
            return False

        path.unlink()
        self.log_event("writes.jsonl", {
            "memory_id": memory_id,
            "status": "deleted",
        })
        logger.info("Memory %s deleted", memory_id)
        cascaded = self._cascade_deleted_id(memory_id)
        if cascaded:
            logger.info("Memory %s cascade cleaned %d entries", memory_id, cascaded)
        _publish_bus_event(
            "memory_deleted", {"id": memory_id, "cascaded": cascaded}
        )
        return True

    def _cascade_deleted_id(self, deleted_id: str) -> int:
        """Strip `deleted_id` from every other entry's frontmatter link lists
        and re-render its auto-managed ``## Related`` block.

        Closes the dangling-ref class of `memory_lint` findings: deleting a
        memory leaves stale `auto_links`/`links` IDs and broken wikilinks
        inside the Related block of any entry that pointed at it. We touch
        only entries that actually reference the deleted id and only the
        block boundaries owned by the auto-linker — operator-written body
        wikilinks outside the markers stay intact so the lint can still
        surface them for an operator decision.

        Returns the number of entries rewritten.
        """
        touched = 0
        for fm in self.list_all():
            if fm.id == deleted_id:
                continue
            auto_links = list(fm.auto_links or [])
            links = list(fm.links or [])
            if deleted_id not in auto_links and deleted_id not in links:
                continue

            new_auto_links = [x for x in auto_links if x != deleted_id]
            new_links = [x for x in links if x != deleted_id]

            read_result = self.read(fm.id, log_access=False)
            if read_result is None:
                continue
            _, body = read_result

            items: list[RelatedItem] = []
            for lid in new_auto_links:
                nbr = self.read(lid, log_access=False)
                title = nbr[0].title if nbr is not None else ""
                items.append((lid, title))

            updated_fm = fm.model_copy(
                update={"auto_links": new_auto_links, "links": new_links}
            )
            new_body = replace_related_block(body, items)
            if self.write(updated_fm, new_body, skip_wnts_check=True):
                touched += 1
                self.log_event("writes.jsonl", {
                    "memory_id": fm.id,
                    "status": "cascaded",
                    "removed_ref": deleted_id,
                })
        return touched

    def _validate_relative_path(self, relative_path: str) -> Path:
        """Resolve a relative path and verify it stays within store_dir."""
        path = (self._store_dir / relative_path).resolve()
        store_resolved = self._store_dir.resolve()
        if not str(path).startswith(str(store_resolved) + os.sep) and path != store_resolved:
            raise ValueError(f"Path escapes store boundary: {relative_path}")
        return path

    def read_file(self, relative_path: str) -> str | None:
        """Read an arbitrary file relative to store_dir. Returns None if missing."""
        path = self._validate_relative_path(relative_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def append_to_file(self, relative_path: str, content: str) -> None:
        """Append content to a file relative to store_dir. Creates if missing."""
        path = self._validate_relative_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

    def list_daily_notes(self) -> list[Path]:
        """Return daily note paths sorted newest-first.

        Reads from `daily/` — the F1 raw-capture layer (`_ensure_dirs`
        creates it). Pre-reboot `memory/` is retired.
        """
        daily_dir = self._store_dir / "daily"
        if not daily_dir.exists():
            return []
        return sorted(daily_dir.glob("????-??-??.md"), reverse=True)

    def archive_file(self, src: str, dest: str) -> bool:
        """Move a file from src to dest (both relative to store_dir). Returns True on success."""
        src_path = self._validate_relative_path(src)
        if not src_path.exists():
            return False
        dest_path = self._validate_relative_path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dest_path))
        return True

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict, str]:
        """Return `(yaml_dict, body)` for a memory-file text. Body may be empty
        if the caller only needs frontmatter."""
        text = text.replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("Memory file missing YAML frontmatter")
        end = text.index("---\n", 4)
        yaml_dict = yaml.safe_load(text[4:end])
        body = text[end + 4:].strip()
        return yaml_dict, body

    @classmethod
    def _parse_file(cls, text: str) -> tuple[MemoryFrontmatter, str]:
        yaml_dict, body = cls._split_frontmatter(text)
        return MemoryFrontmatter.from_yaml_dict(yaml_dict), body

    @classmethod
    def _parse_frontmatter_only(cls, text: str) -> MemoryFrontmatter:
        yaml_dict, _ = cls._split_frontmatter(text)
        return MemoryFrontmatter.from_yaml_dict(yaml_dict)
