"""Vault manager — filing suggestions, immutable storage, catalog maintenance.

The vault stores raw source material (PDFs, articles, data files, web snapshots).
Files in the vault are never modified by the system. Memories in the memory store
link back to vault entries via source_path.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePath

import yaml

logger = logging.getLogger(__name__)

CATALOG_FILENAME = "CATALOG.md"

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_DEFAULT_SLUG_MAX = 60


def _find_section_header_case_insensitive(content: str, wanted: str) -> str | None:
    """Return the exact section-header line in `content` that case-insensitively
    matches `wanted`, preserving the existing casing so the caller can
    `content.replace(existing, ...)` without rewriting unrelated chars."""
    wanted_lc = wanted.lower()
    for line in content.splitlines():
        if line.lower() == wanted_lc:
            return line
    return None


def slugify(text: str, *, max_length: int = _DEFAULT_SLUG_MAX) -> str:
    """Canonical vault slug: NFKD-normalize → strip non-ASCII → lowercase →
    collapse non-alphanumerics to `-` → trim edges → cap length.

    Shared by `vault_librarian` (compile-time hub slugs) and `vault_lint` /
    `vault_query` (lookup-side slugification). Keeping one implementation
    prevents lint from generating ghost missing-hub findings for entities
    whose ingest slug was NFKD-collapsed (e.g. ``"résumé" → "resume"``).
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return _SLUG_RE.sub("-", normalized.lower().strip()).strip("-")[:max_length]

_CATALOG_HEADER = """\
# TESSERACT Vault Catalog

## Research

## Articles

## Uploads

## Data

## Snapshots

## Media

## Recent Additions
"""

SECTIONS = {
    "research": "## Research",
    "articles": "## Articles",
    "uploads": "## Uploads",
    "data": "## Data",
    "snapshots": "## Snapshots",
    "media": "## Media",
}

EXTENSION_MAP: dict[str, str] = {
    ".pdf": "research",
    ".md": "articles",
    ".txt": "articles",
    ".csv": "data",
    ".json": "data",
    ".tsv": "data",
    ".xlsx": "data",
    ".png": "media/images",
    ".jpg": "media/images",
    ".jpeg": "media/images",
    ".gif": "media/images",
    ".svg": "media/images",
    ".mp3": "media/audio",
    ".wav": "media/audio",
    ".mp4": "media/video",
    ".docx": "uploads",
    ".pptx": "uploads",
}


_WIKI_INDEX_HEADER = """\
# Vault Wiki Index

Topics are grouped by subject. Each entry links to a wiki page summarizing a source.
Use [[wikilinks]] to navigate between related pages.
Browse in Obsidian for graph view.

"""

_INGEST_LOG_HEADER = "# Vault Ingest Log\n\n"


def _safe_load_mapping(text: str) -> dict:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_lint_flags(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """Concat + dedupe by (kind, against, reason). Preserves order."""
    seen: set[tuple] = set()
    merged: list[dict] = []
    for flag in existing + incoming:
        if not isinstance(flag, dict):
            continue
        key = (flag.get("kind"), flag.get("against"), flag.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(flag)
    return merged


def _render_lint_flags_region(flags: list[dict]) -> list[str]:
    """Render a ``lint_flags:`` YAML region as frontmatter lines (no trailing blank)."""
    if not flags:
        return ["lint_flags: []"]
    dumped = yaml.safe_dump(
        {"lint_flags": flags},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return [line for line in dumped.rstrip("\n").split("\n") if line]


class VaultManager:
    def __init__(self, vault_root: Path) -> None:
        self._root = vault_root

    @property
    def root(self) -> Path:
        return self._root

    @property
    def raw_dir(self) -> Path:
        return self._root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self._root / "wiki"

    def seed_wiki_skeleton(self) -> None:
        """Create vault/wiki/INDEX.md and vault/wiki/ingest-log.md if missing."""
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        index = self.wiki_dir / "INDEX.md"
        if not index.exists():
            index.write_text(_WIKI_INDEX_HEADER, encoding="utf-8")
        log = self.wiki_dir / "ingest-log.md"
        if not log.exists():
            log.write_text(_INGEST_LOG_HEADER, encoding="utf-8")

    def suggest_raw_filing_path(self, filename: str) -> str:
        """Suggest a path inside vault/raw/{YYYYMMDD}/{slug}.ext for new uploads.

        AU-22 (2026-05-18): daily granularity. Operator drops research into
        `vault/raw/<YYYYMMDD>/` and the AU-22 watcher scans only date-named
        subfolders matching `^\\d{8}$`.
        """
        date_folder = datetime.now(timezone.utc).strftime("%Y%m%d")
        slug = Path(filename).stem.lower().replace(" ", "-").replace("_", "-")
        ext = Path(filename).suffix.lower()
        return f"raw/{date_folder}/{slug}{ext}"

    def read_wiki_index(self) -> str:
        """Read vault/wiki/INDEX.md content. Returns empty string if missing."""
        index = self.wiki_dir / "INDEX.md"
        if not index.exists():
            return ""
        return index.read_text(encoding="utf-8")

    def _wiki_page_path(self, slug: str) -> Path | None:
        """Resolve ``slug`` to a path inside ``wiki_dir``, or None if it escapes.

        Slugs are not all self-minted. `vault_query`'s expand pass walks
        `related_slugs:` / `backlinks_from:` straight out of page frontmatter,
        and a page's frontmatter is written from a model's JSON over document
        text the operator did not author — so a slug reaching here can be
        attacker-influenced. `wiki_dir / f"{slug}.md"` on `../../secrets` or an
        absolute path silently leaves the vault, and `vault_query` then reads
        the result into an answer.

        Two gates rather than one. First the slug must be a bare filename:
        `PurePath(slug).name == slug` rejects every separator form at once —
        `../x`, `a/b`, `/abs`, `C:\\x` — without a charset rule that would also
        reject the legitimately uppercase bookkeeping pages (`INDEX`). Then the
        resolved path must still sit under `wiki_dir`, which catches symlinks
        and anything the first gate would ever be loosened to admit.
        """
        if not slug or slug.startswith(".") or PurePath(slug).name != slug:
            return None
        page = (self.wiki_dir / f"{slug}.md").resolve()
        try:
            page.relative_to(self.wiki_dir.resolve())
        except ValueError:
            return None
        return page

    def write_wiki_page(self, slug: str, content: str) -> Path:
        """Write a wiki page to vault/wiki/{slug}.md. Overwrites if exists."""
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        page = self._wiki_page_path(slug)
        if page is None:
            raise ValueError(f"refusing to write wiki page outside the vault: {slug!r}")
        tmp = page.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(page)
        return page

    def read_wiki_page(self, slug: str) -> str | None:
        """Read a wiki page by slug. Returns None if missing or out of bounds."""
        page = self._wiki_page_path(slug)
        if page is None or not page.exists():
            return None
        return page.read_text(encoding="utf-8")

    def wiki_page_exists(self, slug: str) -> bool:
        page = self._wiki_page_path(slug)
        return page is not None and page.exists()

    def read_wiki_page_frontmatter(self, slug: str) -> dict:
        """Return YAML frontmatter as a dict, or {} if missing/malformed."""
        content = self.read_wiki_page(slug)
        if content is None or not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        try:
            parsed = yaml.safe_load(parts[1])
        except yaml.YAMLError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def update_wiki_backlinks(self, slug: str, new_backlinks: list[str]) -> bool:
        """Merge entries into the ``backlinks_from:`` region of a wiki page."""
        return self._update_wiki_list_field(slug, "backlinks_from", new_backlinks)

    def update_wiki_related_slugs(self, slug: str, new_slugs: list[str]) -> bool:
        """Merge entries into the ``related_slugs:`` region of a wiki page."""
        return self._update_wiki_list_field(slug, "related_slugs", new_slugs)

    def _update_wiki_list_field(
        self, slug: str, field: str, new_items: list[str]
    ) -> bool:
        """Rewrite only one list-valued frontmatter region of a wiki page.

        Merges with any existing entries, deduping while preserving order.
        Every other field + the body are preserved byte-for-byte. Returns
        ``False`` if the page is missing, lacks a well-formed frontmatter,
        or has no ``{field}:`` header to anchor on.
        """
        page = self._wiki_page_path(slug)
        if page is None or not page.exists():
            return False

        lines = page.read_text(encoding="utf-8").split("\n")
        if not lines or lines[0] != "---":
            return False

        end = None
        for idx in range(1, len(lines)):
            if lines[idx] == "---":
                end = idx
                break
        if end is None:
            return False

        header_idx = None
        for idx in range(1, end):
            if lines[idx].startswith(f"{field}:"):
                header_idx = idx
                break
        if header_idx is None:
            return False

        header_line = lines[header_idx]
        existing: list[str] = []
        if header_line.strip() == f"{field}:":
            region_end = header_idx + 1
            while region_end < end and lines[region_end].startswith("  - "):
                existing.append(lines[region_end][4:].strip())
                region_end += 1
        else:
            inline = header_line.split(":", 1)[1].strip()
            if inline and inline != "[]":
                existing = [s.strip() for s in inline.strip("[]").split(",") if s.strip()]
            region_end = header_idx + 1

        merged: list[str] = list(existing)
        for item in new_items:
            if item not in merged:
                merged.append(item)

        if merged:
            new_region = [f"{field}:"] + [f"  - {s}" for s in merged]
        else:
            new_region = [f"{field}: []"]

        new_content = "\n".join(lines[:header_idx] + new_region + lines[region_end:])
        tmp = page.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(page)
        return True

    _WIKI_BOOKKEEPING = frozenset({"INDEX", "TAXONOMY", "LINT-REPORT", "ingest-log"})

    def list_wiki_slugs(self) -> list[str]:
        """Slugs of every content wiki page (bookkeeping pages excluded)."""
        if not self.wiki_dir.exists():
            return []
        return sorted(
            p.stem
            for p in self.wiki_dir.glob("*.md")
            if p.stem not in self._WIKI_BOOKKEEPING
        )

    def update_lint_flags(self, slug: str, new_flags: list[dict]) -> bool:
        """Merge lint findings into a wiki page's ``lint_flags:`` frontmatter region.

        Each flag is a dict with ``kind`` + ``detected`` (required) and
        optionally ``against`` + ``reason`` (per `_shared/wiki-page-frontmatter.md`).
        Dedupe key: ``(kind, against, reason)``. Pages with no frontmatter (e.g.
        ``INDEX.md`` before the scale alarm fires) get one synthesized containing
        only ``lint_flags:``. Returns ``False`` if the page is missing or the
        existing frontmatter is malformed (no closing ``---``).
        """
        page = self._wiki_page_path(slug)
        if page is None or not page.exists():
            return False

        content = page.read_text(encoding="utf-8")
        lines = content.split("\n")
        has_fm = bool(lines) and lines[0] == "---"

        if has_fm:
            end = None
            for idx in range(1, len(lines)):
                if lines[idx] == "---":
                    end = idx
                    break
            if end is None:
                return False

            header_idx = None
            for idx in range(1, end):
                if lines[idx].startswith("lint_flags:"):
                    header_idx = idx
                    break

            existing: list[dict] = []
            if header_idx is not None:
                header_line = lines[header_idx]
                if header_line.strip() == "lint_flags:":
                    region_end = header_idx + 1
                    while region_end < end and (
                        lines[region_end].startswith("  ")
                        or lines[region_end].startswith("- ")
                    ):
                        region_end += 1
                    block_text = "\n".join(lines[header_idx:region_end])
                    parsed = _safe_load_mapping(block_text).get("lint_flags")
                    if isinstance(parsed, list):
                        existing = [f for f in parsed if isinstance(f, dict)]
                else:
                    parsed = _safe_load_mapping(header_line).get("lint_flags")
                    if isinstance(parsed, list):
                        existing = [f for f in parsed if isinstance(f, dict)]
                    region_end = header_idx + 1
            else:
                region_end = end  # insert before closing ---

            merged = _merge_lint_flags(existing, new_flags)
            new_region = _render_lint_flags_region(merged)

            insert_at = header_idx if header_idx is not None else end
            new_lines = lines[:insert_at] + new_region + lines[region_end:]
            new_content = "\n".join(new_lines)
        else:
            new_region = _render_lint_flags_region(new_flags)
            header_block = ["---", *new_region, "---", ""]
            new_content = "\n".join(header_block) + content

        tmp = page.with_suffix(".tmp")
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(page)
        return True

    def update_wiki_index(self, topic: str, slug: str, title: str, summary: str) -> None:
        """Add or update a topic section entry in vault/wiki/INDEX.md.

        Case-insensitive section-header match (audit-1 m7, 2026-04-24): a
        topic re-ingested with a different title-casing (`"machine learning"`
        vs `"Machine Learning"`) must reuse the existing `##` section rather
        than appending a duplicate.
        """
        index = self.wiki_dir / "INDEX.md"
        content = index.read_text(encoding="utf-8") if index.exists() else _WIKI_INDEX_HEADER

        entry = f"- [[{slug}]] — {summary}"
        section_header = f"## {topic.replace('-', ' ').title()}"
        existing_header = _find_section_header_case_insensitive(content, section_header)

        if existing_header is not None:
            if f"[[{slug}]]" not in content:
                content = content.replace(
                    existing_header,
                    f"{existing_header}\n{entry}",
                    1,
                )
        else:
            content = content.rstrip() + f"\n\n{section_header}\n{entry}\n"

        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        tmp = index.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(index)

    def append_ingest_log(self, entry: str) -> None:
        """Prepend a new entry to vault/wiki/ingest-log.md (newest first)."""
        log = self.wiki_dir / "ingest-log.md"
        existing = log.read_text(encoding="utf-8") if log.exists() else _INGEST_LOG_HEADER
        # Keep header, insert after it
        if existing.startswith(_INGEST_LOG_HEADER):
            content = _INGEST_LOG_HEADER + entry + "\n" + existing[len(_INGEST_LOG_HEADER):]
        else:
            content = existing + "\n" + entry + "\n"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        tmp = log.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(log)

    def list_raw_files(self) -> list[Path]:
        """List all files in vault/raw/ recursively."""
        if not self.raw_dir.exists():
            return []
        return sorted(p for p in self.raw_dir.rglob("*") if p.is_file())

    def _validate_vault_path(self, vault_rel_path: str) -> Path:
        """Resolve vault-relative path and verify it stays within the vault root.

        Raises ValueError if the resolved path escapes the vault tree.
        """
        dest = (self._root / vault_rel_path).resolve()
        vault_resolved = self._root.resolve()
        if not dest.is_relative_to(vault_resolved):
            raise ValueError(f"Vault path escapes vault root: {vault_rel_path}")
        return dest

    def suggest_filing_path(
        self,
        filename: str,
        source_url: str | None = None,
    ) -> str:
        """Suggest a vault-relative filing path based on extension and date."""
        ext = Path(filename).suffix.lower()
        date_folder = datetime.now(timezone.utc).strftime("%Y-%m")

        if source_url and ext in (".md", ".html", ".htm", ".txt"):
            category = "snapshots"
        else:
            category = EXTENSION_MAP.get(ext, "uploads")

        slug = Path(filename).stem.lower().replace(" ", "-").replace("_", "-")
        return f"{category}/{date_folder}/{slug}{ext}"

    def file_to_vault(
        self,
        source_path: Path,
        vault_rel_path: str,
    ) -> Path:
        """Copy a source file into the vault. Returns the absolute vault path.

        Raises FileExistsError if the target already exists (vault is append-only).
        Raises ValueError if the path escapes the vault root.
        """
        dest = self._validate_vault_path(vault_rel_path)
        if dest.exists():
            raise FileExistsError(f"Vault file already exists: {vault_rel_path}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_path), str(dest))

        try:
            dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass  # Windows read-only attribute may not map exactly

        return dest

    def write_content_to_vault(
        self,
        content: str | bytes,
        vault_rel_path: str,
    ) -> Path:
        """Write content directly into the vault (e.g. web snapshots).

        Raises FileExistsError if the target already exists.
        """
        dest = self._validate_vault_path(vault_rel_path)
        if dest.exists():
            raise FileExistsError(f"Vault file already exists: {vault_rel_path}")

        dest.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if isinstance(content, str) else "wb"
        with open(dest, mode) as f:
            f.write(content)

        try:
            dest.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass

        return dest

    def write_meta_sidecar(self, vault_rel_path: str, meta: dict) -> Path:
        """Write a .meta.yaml sidecar alongside a vault file.

        Uses the full filename (including extension) so foo.pdf and foo.txt
        produce distinct foo.pdf.meta.yaml / foo.txt.meta.yaml sidecars.
        """
        self._validate_vault_path(vault_rel_path)
        name = Path(vault_rel_path).name
        parent = (self._root / vault_rel_path).parent
        meta_path = parent / f"{name}.meta.yaml"
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        with open(meta_path, "w", encoding="utf-8") as f:
            yaml.dump(meta, f, default_flow_style=False, allow_unicode=True)

        return meta_path

    def read_meta_sidecar(self, vault_rel_path: str) -> dict | None:
        """Read a .meta.yaml sidecar if it exists."""
        name = Path(vault_rel_path).name
        parent = (self._root / vault_rel_path).parent
        meta_path = parent / f"{name}.meta.yaml"

        if not meta_path.exists():
            return None

        with open(meta_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def ensure_catalog(self) -> Path:
        """Create CATALOG.md with default header if it doesn't exist."""
        catalog = self._root / CATALOG_FILENAME
        if not catalog.exists():
            self._root.mkdir(parents=True, exist_ok=True)
            with open(catalog, "w", encoding="utf-8") as f:
                f.write(_CATALOG_HEADER)
        return catalog

    def update_catalog(
        self,
        vault_rel_path: str,
        title: str,
        summary: str,
        category: str,
        source_url: str | None = None,
    ) -> None:
        """Add an entry to CATALOG.md under the appropriate section."""
        catalog = self.ensure_catalog()

        with open(catalog, "r", encoding="utf-8") as f:
            content = f.read()

        entry = f"- [{title}]({vault_rel_path}) — {summary}"
        if source_url:
            entry += f"\n  - URL: {source_url}"

        # Determine section — strip sub-paths like media/images -> media
        section_key = category.split("/")[0]
        section_header = SECTIONS.get(section_key, "## Other")

        if section_header in content:
            content = content.replace(
                section_header,
                f"{section_header}\n{entry}",
                1,
            )
        else:
            content += f"\n\n{section_header}\n{entry}"

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        recent_entry = f"- {date_str}: Added {title} ({category})"
        if "## Recent Additions" in content:
            content = content.replace(
                "## Recent Additions",
                f"## Recent Additions\n{recent_entry}",
                1,
            )
        else:
            content += f"\n\n## Recent Additions\n{recent_entry}"

        with open(catalog, "w", encoding="utf-8") as f:
            f.write(content)

    def list_vault_files(self) -> list[Path]:
        """List all content files in the vault (excludes .meta.yaml and CATALOG.md)."""
        if not self._root.exists():
            return []

        files = []
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            if path.name == CATALOG_FILENAME:
                continue
            if path.suffix == ".yaml" and path.stem.endswith(".meta"):
                continue
            files.append(path)

        return sorted(files)

    def vault_rel_path(self, abs_path: Path) -> str:
        """Convert an absolute vault path to a vault-relative path."""
        return str(abs_path.relative_to(self._root)).replace("\\", "/")

    def category_from_path(self, vault_rel_path: str) -> str:
        """Extract the category from a vault-relative path (first path component)."""
        return vault_rel_path.split("/")[0]
