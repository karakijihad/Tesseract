"""vault_manager.update_lint_flags — region-rewrite + prepend-on-no-frontmatter.

Scoped low-level tests for the frontmatter region writer that Phase 5's
VaultLinter depends on. Integration tests over VaultLinter itself live in
test_vault_lint.py.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tesseract.memory.vault_manager import VaultManager


def _seed_source_page(vault_root: Path, slug: str, *, with_frontmatter: bool = True) -> Path:
    wiki = vault_root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    page = wiki / f"{slug}.md"
    if with_frontmatter:
        page.write_text(
            "---\n"
            f"title: {slug.title()}\n"
            "type: Source\n"
            f"slug: {slug}\n"
            "topic: memory\n"
            "entities: []\n"
            "concepts: []\n"
            "related_slugs: []\n"
            "open_questions: []\n"
            "backlinks_from: []\n"
            "---\n\n"
            f"# {slug.title()}\n\nBody text here.\n",
            encoding="utf-8",
        )
    else:
        page.write_text(
            f"# {slug.title()}\n\nNo-frontmatter page (INDEX.md style).\n",
            encoding="utf-8",
        )
    return page


def test_update_lint_flags_writes_orphan_entry(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_source_page(vault, "alpha")
    mgr = VaultManager(vault_root=vault)

    ok = mgr.update_lint_flags("alpha", [{"kind": "orphan", "detected": "2026-04-22"}])
    assert ok is True

    fm = mgr.read_wiki_page_frontmatter("alpha")
    assert fm.get("lint_flags") == [{"kind": "orphan", "detected": "2026-04-22"}]
    # Other fields preserved.
    assert fm.get("title") == "Alpha"
    assert fm.get("type") == "Source"


def test_update_lint_flags_is_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_source_page(vault, "alpha")
    mgr = VaultManager(vault_root=vault)

    flag = {"kind": "orphan", "detected": "2026-04-22"}
    mgr.update_lint_flags("alpha", [flag])
    first = (vault / "wiki" / "alpha.md").read_text(encoding="utf-8")
    mgr.update_lint_flags("alpha", [flag])
    second = (vault / "wiki" / "alpha.md").read_text(encoding="utf-8")
    assert first == second


def test_update_lint_flags_merges_without_duplicate(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_source_page(vault, "alpha")
    mgr = VaultManager(vault_root=vault)

    mgr.update_lint_flags("alpha", [{"kind": "orphan", "detected": "2026-04-22"}])
    mgr.update_lint_flags(
        "alpha",
        [
            {"kind": "orphan", "detected": "2026-04-22"},
            {"kind": "stale", "detected": "2026-04-23"},
        ],
    )
    fm = mgr.read_wiki_page_frontmatter("alpha")
    kinds = [f["kind"] for f in fm["lint_flags"]]
    assert kinds == ["orphan", "stale"]


def test_update_lint_flags_prepends_frontmatter_when_missing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_source_page(vault, "INDEX", with_frontmatter=False)
    mgr = VaultManager(vault_root=vault)

    ok = mgr.update_lint_flags(
        "INDEX",
        [{"kind": "scale", "detected": "2026-04-22"}],
    )
    assert ok is True

    content = (vault / "wiki" / "INDEX.md").read_text(encoding="utf-8")
    assert content.startswith("---\n")
    fm = mgr.read_wiki_page_frontmatter("INDEX")
    assert fm["lint_flags"] == [{"kind": "scale", "detected": "2026-04-22"}]
    # Original body preserved.
    assert "No-frontmatter page (INDEX.md style)." in content


def test_update_lint_flags_returns_false_for_missing_page(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    mgr = VaultManager(vault_root=vault)

    ok = mgr.update_lint_flags("ghost", [{"kind": "orphan", "detected": "2026-04-22"}])
    assert ok is False


def test_update_lint_flags_contradict_with_against_and_reason(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed_source_page(vault, "alpha")
    mgr = VaultManager(vault_root=vault)

    mgr.update_lint_flags(
        "alpha",
        [{
            "kind": "contradict",
            "against": "beta",
            "reason": "alpha claims X, beta claims not-X",
            "detected": "2026-04-22",
        }],
    )
    fm = mgr.read_wiki_page_frontmatter("alpha")
    flags = fm["lint_flags"]
    assert len(flags) == 1
    assert flags[0]["kind"] == "contradict"
    assert flags[0]["against"] == "beta"
    assert "alpha claims X" in flags[0]["reason"]
