"""Vault-librarian rewire (phase 3) — wiki compile cross-ref compounding.

Locks in:
- `compile_source()` appends the new source's slug to the `backlinks_from:`
  frontmatter of any pre-existing hub page whose slug matches an
  entity/concept/related_slug.
- Missing hub targets are logged as skipped, never raise.
- Re-running `compile_source()` on the same raw file is a no-op.
- A mid-pipeline write failure records one breaker failure + does not abort
  the Source-page landing.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from tesseract.brain.boot import VaultConfig
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.vault_librarian import VaultLibrarian
from tesseract.memory.vault_manager import VaultManager


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vault" / "sample-article.md"
_HUB_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "vault" / "hub-system-dynamics.md"


_DEFAULT_VAULT_CONFIG = VaultConfig(
    max_extract_chars=3000,
    scale_split_threshold=80,
    stale_grace_days=180,
    contradiction_pair_limit=50,
    max_seed_slugs=6,
    max_expanded_slugs=12,
    search_rrf_k=60,
    search_default_top_k=5,
)


@dataclass
class _StubAdapter:
    """Minimal duck-typed ModelAdapter for the librarian's `_get_adapter` path."""

    response_json: str
    call_count: int = 0

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
        self.call_count += 1
        return self.response_json

    async def stream(self, *args, **kwargs):  # pragma: no cover — unused by librarian
        raise NotImplementedError


def _build_librarian(
    vault_root: Path,
    adapter: ModelAdapter | None,
) -> VaultLibrarian:
    (vault_root / "raw").mkdir(parents=True, exist_ok=True)
    (vault_root / "wiki").mkdir(parents=True, exist_ok=True)
    manager = VaultManager(vault_root=vault_root)
    manager.seed_wiki_skeleton()
    return VaultLibrarian(
        vault_manager=manager,
        adapter=adapter,
        adapter_options=AdapterOptions(),
        config=_DEFAULT_VAULT_CONFIG,
        agents_dir=None,
    )


def _stage_source(vault_root: Path, fixture: Path) -> str:
    dst = vault_root / "raw" / fixture.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(fixture), str(dst))
    return f"raw/{fixture.name}"


def _seed_hub(vault_root: Path, slug: str, source_fixture: Path | None = None) -> Path:
    """Copy a hub fixture (or write a minimal stub) into vault/wiki/{slug}.md."""
    dst = vault_root / "wiki" / f"{slug}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if source_fixture is not None:
        shutil.copy2(str(source_fixture), str(dst))
    else:
        dst.write_text(
            "---\n"
            f"title: {slug.replace('-', ' ').title()}\n"
            "type: Concept\n"
            f"slug: {slug}\n"
            "entity_types:\n"
            "  - Concept\n"
            "date_added: 2026-04-22\n"
            "backlinks_from: []\n"
            "---\n\n"
            f"# {slug.replace('-', ' ').title()}\n",
            encoding="utf-8",
        )
    return dst


# ── Tests ─────────────────────────────────────────────────


async def test_compile_source_appends_backlinks_to_existing_hub(tmp_path, caplog) -> None:
    """Pre-existing hub page gains the new source slug in backlinks_from."""
    vault_root = tmp_path / "vault"
    librarian = _build_librarian(
        vault_root,
        _StubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "Discusses temporal decay.",
            "entities": [],
            "concepts": ["system dynamics"],
            "open_questions": [],
            "related_slugs": [],
        })),
    )
    hub_path = _seed_hub(vault_root, "system-dynamics", _HUB_FIXTURE)
    before = hub_path.read_text(encoding="utf-8")

    raw_rel = _stage_source(vault_root, _FIXTURE)
    with caplog.at_level(logging.INFO, logger="tesseract.memory.vault_librarian"):
        page = await librarian.compile_source(raw_rel)

    assert page is not None
    after = hub_path.read_text(encoding="utf-8")
    assert after != before, "hub should be rewritten"

    fm = VaultManager(vault_root).read_wiki_page_frontmatter("system-dynamics")
    assert fm.get("backlinks_from") == [page.slug]

    summary_msg = next(
        (r.getMessage() for r in caplog.records if "backlinks updated" in r.getMessage()),
        None,
    )
    assert summary_msg is not None, "compile_source() must emit the summary log line"
    assert "system-dynamics" in summary_msg
    assert "skipped (missing): []" in summary_msg


async def test_compile_source_skips_missing_hubs(tmp_path, caplog) -> None:
    """When a target slug has no wiki page, log as skipped and keep going."""
    vault_root = tmp_path / "vault"
    librarian = _build_librarian(
        vault_root,
        _StubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "No extant hub.",
            "entities": [],
            "concepts": ["ghost hub"],
            "open_questions": [],
            "related_slugs": [],
        })),
    )

    raw_rel = _stage_source(vault_root, _FIXTURE)
    with caplog.at_level(logging.INFO, logger="tesseract.memory.vault_librarian"):
        page = await librarian.compile_source(raw_rel)

    assert page is not None
    assert (vault_root / "wiki" / f"{page.slug}.md").exists()

    summary_msg = next(
        (r.getMessage() for r in caplog.records if "backlinks updated" in r.getMessage()),
        None,
    )
    assert summary_msg is not None
    assert "backlinks updated: []" in summary_msg
    assert "ghost-hub" in summary_msg and "skipped (missing)" in summary_msg


async def test_compile_source_idempotent_second_run_is_noop(tmp_path) -> None:
    """Second ingest of the same raw path = no hub mutation, no duplicate backlinks."""
    vault_root = tmp_path / "vault"
    hub_path = _seed_hub(vault_root, "system-dynamics", _HUB_FIXTURE)

    def _adapter() -> _StubAdapter:
        return _StubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "Discusses temporal decay.",
            "entities": [],
            "concepts": ["system dynamics"],
            "open_questions": [],
            "related_slugs": [],
        }))

    librarian = _build_librarian(vault_root, _adapter())
    raw_rel = _stage_source(vault_root, _FIXTURE)

    first = await librarian.compile_source(raw_rel)
    assert first is not None
    after_first = hub_path.read_text(encoding="utf-8")
    fm = VaultManager(vault_root).read_wiki_page_frontmatter("system-dynamics")
    assert fm.get("backlinks_from") == [first.slug]

    # Fresh librarian + fresh adapter — emulates a re-run.
    librarian2 = _build_librarian(vault_root, _adapter())
    librarian2._manager = VaultManager(vault_root)  # share the existing vault tree
    second = await librarian2.compile_source(raw_rel)
    assert second is None, "second ingest on same raw file must return None"
    assert hub_path.read_text(encoding="utf-8") == after_first, "hub must not be mutated by noop re-run"

    updated, missing = await librarian2._append_backlinks(first.slug, ["system-dynamics"])
    assert updated == [] and missing == []
    fm2 = VaultManager(vault_root).read_wiki_page_frontmatter("system-dynamics")
    assert fm2.get("backlinks_from") == [first.slug], "direct re-append must stay idempotent"


async def test_compile_source_partial_failure_does_not_abort(tmp_path, monkeypatch) -> None:
    """A mid-loop write raise records one breaker failure, Source page still lands."""
    vault_root = tmp_path / "vault"
    for hub_slug in ("alpha-hub", "beta-hub", "gamma-hub"):
        _seed_hub(vault_root, hub_slug)

    librarian = _build_librarian(
        vault_root,
        _StubAdapter(response_json=json.dumps({
            "topic": "memory",
            "summary": "Three hubs.",
            "entities": ["alpha hub", "beta hub", "gamma hub"],
            "concepts": [],
            "open_questions": [],
            "related_slugs": [],
        })),
    )

    original = librarian._manager.update_wiki_backlinks
    call_counter = {"n": 0}

    def _flaky(slug: str, new_backlinks: list[str]) -> bool:
        call_counter["n"] += 1
        if call_counter["n"] == 3:
            raise OSError("simulated disk failure on third hub")
        return original(slug, new_backlinks)

    monkeypatch.setattr(librarian._manager, "update_wiki_backlinks", _flaky)

    raw_rel = _stage_source(vault_root, _FIXTURE)
    page = await librarian.compile_source(raw_rel)

    assert page is not None, "Source page must still land when a backlink write raises"
    assert (vault_root / "wiki" / f"{page.slug}.md").exists()
    assert librarian._breaker.failure_count == 1, "one backlink failure → one breaker tick"

    # First two hubs updated, third untouched.
    alpha_fm = librarian._manager.read_wiki_page_frontmatter("alpha-hub")
    beta_fm = librarian._manager.read_wiki_page_frontmatter("beta-hub")
    gamma_fm = librarian._manager.read_wiki_page_frontmatter("gamma-hub")
    assert alpha_fm.get("backlinks_from") == [page.slug]
    assert beta_fm.get("backlinks_from") == [page.slug]
    assert gamma_fm.get("backlinks_from") in (None, [])


# ── Unit-level frontmatter tests ───────────────────────────


def test_update_wiki_backlinks_preserves_other_fields(tmp_path) -> None:
    """Rewriting backlinks_from must not touch title, type, slug, body, or tail fields."""
    manager = VaultManager(vault_root=tmp_path)
    (tmp_path / "wiki").mkdir(parents=True)
    page = tmp_path / "wiki" / "hub-x.md"
    page.write_text(
        "---\n"
        "title: Hub X\n"
        "type: Concept\n"
        "slug: hub-x\n"
        "backlinks_from: []\n"
        "date_added: 2026-04-22\n"
        "---\n\n"
        "# Hub X\n\nBody content.\n",
        encoding="utf-8",
    )
    ok = manager.update_wiki_backlinks("hub-x", ["src-a", "src-b"])
    assert ok
    out = page.read_text(encoding="utf-8")
    assert "title: Hub X" in out
    assert "type: Concept" in out
    assert "slug: hub-x" in out
    assert "date_added: 2026-04-22" in out
    assert "# Hub X" in out and "Body content." in out
    assert "backlinks_from:\n  - src-a\n  - src-b" in out

    # Second call with overlap dedupes.
    assert manager.update_wiki_backlinks("hub-x", ["src-b", "src-c"])
    out2 = page.read_text(encoding="utf-8")
    assert out2.count("- src-a") == 1
    assert out2.count("- src-b") == 1
    assert out2.count("- src-c") == 1


def test_read_wiki_page_frontmatter_returns_empty_on_missing(tmp_path) -> None:
    manager = VaultManager(vault_root=tmp_path)
    (tmp_path / "wiki").mkdir(parents=True)
    assert manager.read_wiki_page_frontmatter("nope") == {}
