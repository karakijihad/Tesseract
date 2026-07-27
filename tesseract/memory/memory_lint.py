"""Memory-store integrity linter.

Detects (does not repair):

  - Body wikilinks `[[mem_xxx]]` that don't resolve to a real memory file.
  - Path-style wikilinks `[[some/path.md]]` that don't resolve under the
    store or under the project root (vault / tars-workshop pointers).
  - Frontmatter `auto_links` / `links` IDs referencing missing memories.
  - Stale `source_path` values pointing at files that no longer exist.
  - Zero-byte stub files (Obsidian artifacts from following bogus links).

What this DOES NOT touch — those belong to other jobs:

  - Adding missing wikilinks for entries with `source_path` —
    `dreaming.sweep_missing_wikilinks` already does that.
  - Promoting daily-capture entries — `librarian.run_pass` does that.
  - Pruning daily notes — `dreaming.prune_stale_daily_notes` does that.

Counterpart to `memory/vault_lint.py`. Like it, this linter only reports;
repairs are operator-driven (audit the report, decide per-finding).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Frontmatter keys whose value is a list of memory IDs.
_LINK_FIELDS = ("auto_links", "links")
# Per-machine operator-private directories (gitignored). Wikilinks pointing
# inside them must not be flagged as broken on machines that don't have
# the operator's workspace synced — see CLAUDE.md "Workspace files are
# gitignored — operator-private per-machine."
_WORKSPACE_PREFIXES = ("tars-workshop", "workspace")


@dataclass(frozen=True)
class Finding:
    path: str
    kind: str
    detail: str


@dataclass(frozen=True)
class LintReport:
    files_scanned: int
    memory_count: int
    broken_wikilinks: list[Finding] = field(default_factory=list)
    broken_frontmatter_links: list[Finding] = field(default_factory=list)
    stale_source_paths: list[Finding] = field(default_factory=list)
    orphan_stubs: list[Finding] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.broken_wikilinks)
            + len(self.broken_frontmatter_links)
            + len(self.stale_source_paths)
            + len(self.orphan_stubs)
        )

    def as_dict(self) -> dict:
        def _dump(items: list[Finding]) -> list[dict]:
            return [{"path": f.path, "kind": f.kind, "detail": f.detail} for f in items]

        return {
            "files_scanned": self.files_scanned,
            "memory_count": self.memory_count,
            "total": self.total,
            "broken_wikilinks": _dump(self.broken_wikilinks),
            "broken_frontmatter_links": _dump(self.broken_frontmatter_links),
            "stale_source_paths": _dump(self.stale_source_paths),
            "orphan_stubs": _dump(self.orphan_stubs),
        }


class MemoryLinter:
    """Read-only integrity scan over a memory-store directory.

    Two anchors are tried when resolving `source_path` / path-style
    wikilinks that live outside the store:

      - ``project_root`` — TESSERACT_HOME equivalent (parent of
        `memory-store/`). Catches `tars-workshop/...` and `vault/...`
        references that are written rooted at TESSERACT_HOME.
      - ``repo_root`` — repo checkout root. Catches `tesseract/...`-
        prefixed paths the model writes when copying from the prompt's
        workshop layout (e.g. `tesseract/tars-workshop/2026-05-04/...`).

    Falling back across both lets a single store survive both
    conventions without false positives. Default ``repo_root`` is
    ``project_root.parent``, which matches dev checkouts; packaged
    installs that relocate TESSERACT_HOME should pass it explicitly.
    """

    def __init__(
        self,
        store_dir: Path,
        *,
        project_root: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self._store_dir = store_dir
        self._project_root = project_root or store_dir.parent
        self._repo_root = repo_root or self._project_root.parent

    def lint(self) -> LintReport:
        all_md = list(self._store_dir.rglob("*.md"))
        all_paths = {self._rel(p) for p in all_md}
        all_stems = {p.stem for p in all_md}

        broken_wikilinks: list[Finding] = []
        broken_fm: list[Finding] = []
        stale_sources: list[Finding] = []
        orphan_stubs: list[Finding] = []

        for path in all_md:
            rel = self._rel(path)

            # Zero-byte stub detection. Real memory files always have a
            # frontmatter block. An empty file inside the store is almost
            # always an Obsidian artifact from clicking a broken wikilink.
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size == 0:
                orphan_stubs.append(Finding(rel, "zero_byte", "empty file"))
                continue

            text = path.read_text(encoding="utf-8", errors="replace")
            fm, body = _split_frontmatter(text)

            for match in _WIKILINK_RE.finditer(body):
                target = match.group(1).strip()
                if _DATE_RE.match(target):
                    continue
                if target.startswith("mem_"):
                    if target not in all_stems:
                        broken_wikilinks.append(
                            Finding(rel, "missing_memory", f"[[{target}]]")
                        )
                    continue
                # Scheme references (`vault:raw/...:chunk_N`, future schemes)
                # aren't filesystem paths — vault validation lives in
                # `vault_lint`, not here.
                if ":" in target.split("/", 1)[0]:
                    continue
                if "/" in target or target.endswith(".md"):
                    if not self._path_target_exists(target, all_paths, all_stems):
                        broken_wikilinks.append(
                            Finding(rel, "missing_path", f"[[{target}]]")
                        )
                    continue
                # Bare token — drift writer cluster anchors. Skip.

            if isinstance(fm, dict):
                for key in _LINK_FIELDS:
                    refs = fm.get(key) or []
                    if not isinstance(refs, list):
                        continue
                    for ref in refs:
                        if isinstance(ref, str) and ref.startswith("mem_") and ref not in all_stems:
                            broken_fm.append(Finding(rel, key, ref))

                source_path = fm.get("source_path")
                if isinstance(source_path, str) and source_path.strip():
                    if not self._source_path_exists(source_path, all_paths):
                        stale_sources.append(Finding(rel, "source_path", source_path))

        return LintReport(
            files_scanned=len(all_md),
            memory_count=sum(1 for s in all_stems if s.startswith("mem_")),
            broken_wikilinks=broken_wikilinks,
            broken_frontmatter_links=broken_fm,
            stale_source_paths=stale_sources,
            orphan_stubs=orphan_stubs,
        )

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self._store_dir)).replace("\\", "/")

    def _path_target_exists(
        self, target: str, all_paths: set[str], all_stems: set[str]
    ) -> bool:
        normalized = target if target.endswith(".md") else target + ".md"
        if normalized in all_paths or target in all_stems:
            return True
        return self._resolves_outside_store(target)

    def _source_path_exists(self, source_path: str, all_paths: set[str]) -> bool:
        cleaned = source_path.strip().replace("\\", "/")
        # Strip Obsidian-style heading anchors (`...md#section`) — the
        # anchor isn't part of the filesystem path.
        cleaned = cleaned.split("#", 1)[0]
        # Folder-shaped pointers are intentional (cf. `dreaming.py`'s
        # sweep_missing_wikilinks, which skips them). Treat as resolved.
        if not cleaned or cleaned.endswith("/"):
            return True
        if cleaned in all_paths:
            return True
        return self._resolves_outside_store(cleaned)

    def _resolves_outside_store(self, target: str) -> bool:
        cleaned = target.replace("\\", "/").lstrip("/")
        for base in self._candidate_roots():
            base_resolved = base.resolve()
            candidate = (base / cleaned).resolve()
            try:
                candidate.relative_to(base_resolved)
            except ValueError:
                continue
            if candidate.exists():
                return True
            if not cleaned.endswith(".md"):
                md_candidate = (base / (cleaned + ".md")).resolve()
                if md_candidate.exists():
                    return True
        # Operator-private workspace directories aren't synced across
        # machines. Don't flag a missing file inside one as broken — the
        # operator can verify locally if needed. Match a workspace
        # segment ANYWHERE in the path so `tesseract/tars-workshop/...`
        # is bypassed too.
        if any(seg in _WORKSPACE_PREFIXES for seg in cleaned.split("/")):
            return True
        return False

    def _candidate_roots(self) -> tuple[Path, ...]:
        if self._repo_root.resolve() == self._project_root.resolve():
            return (self._project_root,)
        return (self._project_root, self._repo_root)


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None, text
    try:
        end = text.index("---\n", 4)
    except ValueError:
        return None, text
    try:
        fm = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None, text[end + 4:]
    return fm, text[end + 4:]
