"""Vault-librarian rewire (phase 5) — vault_lint orchestration.

Locks in:
- Orphan pass flags Source pages with empty backlinks + empty related_slugs.
- Stale pass flags Source pages whose source_path is missing past grace;
  skips pages still inside the grace window.
- Contradict pass writes only 3 of the 4 verbs (reinforce is dropped).
- Contradict pair count is capped by `lint.contradiction_pair_limit`.
- Missing-hub pass requires ≥3 mentions; 2 mentions → no finding.
- Scale pass fires INDEX `lint_flags: [{kind: scale}]` past threshold.
- dry_run=True makes zero filesystem writes.
- Running lint twice back-to-back leaves the tree byte-identical.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pytest
import yaml

from tesseract.brain.boot import VaultConfig
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.vault_lint import VaultLinter, VaultLintReport
from tesseract.memory.vault_manager import VaultManager


# ── fixtures ──


def _default_config(**overrides) -> VaultConfig:
    base = dict(
        max_extract_chars=3000,
        scale_split_threshold=80,
        stale_grace_days=180,
        contradiction_pair_limit=50,
        max_seed_slugs=6,
        max_expanded_slugs=12,
        search_rrf_k=60,
        search_default_top_k=5,
    )
    base.update(overrides)
    return VaultConfig(**base)


def _seed_wiki_root(tmp_path: Path) -> Path:
    """Create an empty vault/wiki/ with a minimal INDEX.md (no frontmatter)."""
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "raw").mkdir()
    (vault / "wiki" / "INDEX.md").write_text(
        "# Vault Wiki Index\n\nTest seed.\n", encoding="utf-8"
    )
    return vault


def _write_source_page(
    vault_root: Path,
    slug: str,
    *,
    title: str | None = None,
    topic: str = "memory",
    source_path: str = "",
    date_added: str | None = None,
    entities: Iterable[str] = (),
    concepts: Iterable[str] = (),
    related_slugs: Iterable[str] = (),
    backlinks_from: Iterable[str] = (),
) -> Path:
    title = title or slug.replace("-", " ").title()
    date_added = date_added or "2026-04-22"
    src = source_path or f"raw/{slug}.md"
    fm_lines = [
        "---",
        f"title: {title}",
        "type: Source",
        f"slug: {slug}",
        f"topic: {topic}",
        f"source_path: {src}",
        f"date_added: {date_added}",
    ]
    fm_lines.append("entities:" + (" []" if not entities else ""))
    for e in entities:
        fm_lines.append(f"  - {e}")
    fm_lines.append("concepts:" + (" []" if not concepts else ""))
    for c in concepts:
        fm_lines.append(f"  - {c}")
    fm_lines.append("related_slugs:" + (" []" if not related_slugs else ""))
    for r in related_slugs:
        fm_lines.append(f"  - {r}")
    fm_lines.append("backlinks_from:" + (" []" if not backlinks_from else ""))
    for b in backlinks_from:
        fm_lines.append(f"  - {b}")
    fm_lines.append("---")
    body = f"\n# {title}\n\nTest body for {slug}.\n"
    page = vault_root / "wiki" / f"{slug}.md"
    page.write_text("\n".join(fm_lines) + body, encoding="utf-8")
    # Mirror the source file existing so stale pass does not false-fire.
    raw = vault_root / src
    raw.parent.mkdir(parents=True, exist_ok=True)
    if not raw.exists():
        raw.write_text("raw body", encoding="utf-8")
    return page


@dataclass
class _StubAdapter:
    """Duck-typed ModelAdapter for contradiction pass tests."""

    responses: list[str] = field(default_factory=list)
    call_count: int = 0

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
        idx = min(self.call_count, len(self.responses) - 1) if self.responses else 0
        self.call_count += 1
        if not self.responses:
            return '{"verdict": "reinforce", "reason": ""}'
        return self.responses[idx]

    async def stream(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _build_linter(
    vault_root: Path,
    *,
    adapter: ModelAdapter | None = None,
    config: VaultConfig | None = None,
) -> VaultLinter:
    agents_dir = Path(__file__).resolve().parents[2] / "agents"
    return VaultLinter(
        vault_manager=VaultManager(vault_root=vault_root),
        config=config or _default_config(),
        adapter=adapter,
        adapter_options=AdapterOptions(),
        agents_dir=agents_dir,
    )


# ── tests ──


async def test_lint_orphan_flags_unlinked_source(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    _write_source_page(vault, "lonely")
    linter = _build_linter(vault)

    report = await linter.run()
    assert "lonely" in report.orphans

    fm = VaultManager(vault_root=vault).read_wiki_page_frontmatter("lonely")
    kinds = [f["kind"] for f in (fm.get("lint_flags") or [])]
    assert "orphan" in kinds


async def test_lint_stale_flags_missing_source_after_grace(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    # Page references a raw path that doesn't exist; date_added 200 days ago.
    old = (date.today() - timedelta(days=200)).isoformat()
    _write_source_page(
        vault,
        "fossil",
        source_path="raw/missing.md",
        date_added=old,
        related_slugs=["some-topic"],  # avoid orphan flag to isolate stale
    )
    (vault / "raw" / "missing.md").unlink()

    report = await _build_linter(vault).run()
    assert "fossil" in report.stale
    fm = VaultManager(vault_root=vault).read_wiki_page_frontmatter("fossil")
    kinds = [f["kind"] for f in (fm.get("lint_flags") or [])]
    assert "stale" in kinds


async def test_lint_stale_skips_young_missing_source(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    recent = (date.today() - timedelta(days=5)).isoformat()
    _write_source_page(
        vault,
        "recent",
        source_path="raw/missing2.md",
        date_added=recent,
        related_slugs=["some-topic"],
    )
    (vault / "raw" / "missing2.md").unlink()

    report = await _build_linter(vault).run()
    assert "recent" not in report.stale
    fm = VaultManager(vault_root=vault).read_wiki_page_frontmatter("recent")
    kinds = [f["kind"] for f in (fm.get("lint_flags") or [])]
    assert "stale" not in kinds


async def test_lint_contradict_four_verb_only(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    # Four distinct pairs sharing a concept; adapter returns one of each verb.
    slugs = ["a1", "a2", "a3", "a4", "a5"]
    for slug in slugs:
        _write_source_page(
            vault, slug, concepts=["attention"], related_slugs=["attention"],
        )
    adapter = _StubAdapter(responses=[
        '{"verdict": "reinforce", "reason": "both agree"}',
        '{"verdict": "weaken", "reason": "hedge"}',
        '{"verdict": "qualify", "reason": "scope"}',
        '{"verdict": "contradict", "reason": "conflict"}',
    ] * 10)  # plenty to cover all pairs

    linter = _build_linter(vault, adapter=adapter)
    report = await linter.run()

    verdicts = [c.verdict for c in report.contradictions]
    assert "reinforce" not in verdicts
    assert set(verdicts).issubset({"weaken", "qualify", "contradict"})

    # Check frontmatter — reinforce must never appear as a kind.
    for slug in slugs:
        fm = VaultManager(vault_root=vault).read_wiki_page_frontmatter(slug)
        kinds = [f["kind"] for f in (fm.get("lint_flags") or [])]
        assert "reinforce" not in kinds


async def test_lint_contradict_pair_cap(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    # 20 Source pages sharing a concept → C(20, 2) = 190 pairs.
    # Cap at 5 pairs and verify the adapter is called exactly 5 times.
    for i in range(20):
        _write_source_page(
            vault,
            f"p{i:02d}",
            concepts=["shared"],
            related_slugs=["shared"],
        )
    adapter = _StubAdapter(responses=['{"verdict": "reinforce", "reason": ""}'] * 50)
    linter = _build_linter(
        vault,
        adapter=adapter,
        config=_default_config(contradiction_pair_limit=5),
    )
    await linter.run()
    assert adapter.call_count == 5


async def test_lint_missing_hub_threshold(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    # "bigdog" appears on 3 pages (flag). "smalldog" on 2 (no flag).
    # Avoid having either slug present as a hub page so missing-hub fires.
    for slug in ["s1", "s2", "s3"]:
        _write_source_page(
            vault, slug, concepts=["bigdog"], related_slugs=["bigdog"],
        )
    for slug in ["s4", "s5"]:
        _write_source_page(
            vault, slug, concepts=["smalldog"], related_slugs=["smalldog"],
        )

    report = await _build_linter(vault).run()
    terms = {f.term: f.mention_count for f in report.missing_hubs}
    assert terms.get("bigdog") == 3
    assert "smalldog" not in terms


async def test_lint_scale_alarm_fires_at_threshold(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    # Threshold 3 → 4 pages trips.
    for i in range(4):
        _write_source_page(vault, f"s{i:02d}", related_slugs=["x"])

    linter = _build_linter(vault, config=_default_config(scale_split_threshold=3))
    report = await linter.run()

    # INDEX.md contributes to the page-count, so we need > threshold.
    assert report.scale_alarm is True
    fm = VaultManager(vault_root=vault).read_wiki_page_frontmatter("INDEX")
    kinds = [f["kind"] for f in (fm.get("lint_flags") or [])]
    assert "scale" in kinds


async def test_lint_dry_run_writes_nothing(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    _write_source_page(vault, "ghost")

    manager = VaultManager(vault_root=vault)
    before = (vault / "wiki" / "ghost.md").read_text(encoding="utf-8")
    index_before = (vault / "wiki" / "INDEX.md").read_text(encoding="utf-8")

    report = await _build_linter(vault).run(dry_run=True)
    assert "ghost" in report.orphans  # still detected

    after = (vault / "wiki" / "ghost.md").read_text(encoding="utf-8")
    index_after = (vault / "wiki" / "INDEX.md").read_text(encoding="utf-8")
    assert before == after
    assert index_before == index_after


async def test_lint_idempotent(tmp_path: Path) -> None:
    vault = _seed_wiki_root(tmp_path)
    _write_source_page(vault, "lonely")

    linter = _build_linter(vault)
    await linter.run()
    snapshot_a = _snapshot_wiki(vault)
    await _build_linter(vault).run()
    snapshot_b = _snapshot_wiki(vault)
    assert snapshot_a == snapshot_b


def _snapshot_wiki(vault_root: Path) -> dict[str, str]:
    wiki = vault_root / "wiki"
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(wiki.glob("*.md"))
    }
