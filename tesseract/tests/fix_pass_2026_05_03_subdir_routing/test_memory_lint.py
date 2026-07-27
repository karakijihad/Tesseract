"""MemoryLinter detects broken links / orphans; MemoryLintJob surfaces them.

Sister to `test_memory_save_subdir.py`. Verifies the integrity audit picks
up the same kinds of issues we just hand-fixed in memory-store.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tesseract.memory.memory_lint import MemoryLinter
from tesseract.scheduler.tasks.memory_lint import MemoryLintJob
from tesseract.scheduler.types import JobContext


def _seed_memory(
    store_dir: Path,
    *,
    subdir: str,
    mem_id: str,
    body: str,
    extra_fm: dict | None = None,
) -> Path:
    folder = store_dir / subdir
    folder.mkdir(parents=True, exist_ok=True)
    fm_lines = [
        f"id: {mem_id}",
        f"type: {subdir.split('/')[0]}",
        f"title: {mem_id}",
        "summary: seed",
        "created_at: '2026-05-03T00:00:00+00:00'",
        "updated_at: '2026-05-03T00:00:00+00:00'",
        "importance: 5",
        "tags: []",
        "entities: []",
    ]
    for k, v in (extra_fm or {}).items():
        fm_lines.append(f"{k}: {v}")
    text = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body
    path = folder / f"{mem_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_clean_store_returns_zero_findings(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(store, subdir="reference", mem_id="mem_aaaa1111", body="A real memory.")
    _seed_memory(store, subdir="reference", mem_id="mem_bbbb2222", body="Links to [[mem_aaaa1111]].")

    report = MemoryLinter(store_dir=store).lint()

    assert report.total == 0
    assert report.memory_count == 2


def test_detects_broken_body_wikilink(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(store, subdir="reference", mem_id="mem_real001", body="See [[mem_ghost999]] for context.")

    report = MemoryLinter(store_dir=store).lint()

    assert len(report.broken_wikilinks) == 1
    finding = report.broken_wikilinks[0]
    assert finding.kind == "missing_memory"
    assert "mem_ghost999" in finding.detail


def test_detects_broken_frontmatter_links(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(
        store,
        subdir="project",
        mem_id="mem_fm00",
        body="Body.",
        extra_fm={"auto_links": "[mem_alive001, mem_dead999]"},
    )
    _seed_memory(store, subdir="project", mem_id="mem_alive001", body="Sibling.")

    report = MemoryLinter(store_dir=store).lint()

    assert len(report.broken_frontmatter_links) == 1
    finding = report.broken_frontmatter_links[0]
    assert finding.kind == "auto_links"
    assert finding.detail == "mem_dead999"


def test_detects_stale_source_path(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(
        store,
        subdir="project",
        mem_id="mem_stale01",
        body="Body.",
        extra_fm={"source_path": "vault/never-existed.md"},
    )

    report = MemoryLinter(store_dir=store).lint()

    assert len(report.stale_source_paths) == 1
    assert report.stale_source_paths[0].detail == "vault/never-existed.md"


def test_skips_folder_shaped_source_path(tmp_path: Path) -> None:
    """Folder pointers (trailing `/`) are intentional — `dreaming.py`'s
    wikilink sweep also skips them. The linter must do the same."""
    store = tmp_path / "memory-store"
    _seed_memory(
        store,
        subdir="project",
        mem_id="mem_folder1",
        body="Body.",
        extra_fm={"source_path": "reference/people/"},
    )

    report = MemoryLinter(store_dir=store).lint()

    assert len(report.stale_source_paths) == 0


def test_skips_anchored_source_path_when_file_exists(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    daily = store / "daily"
    daily.mkdir(parents=True)
    (daily / "2026-04-29.md").write_text("daily note", encoding="utf-8")

    _seed_memory(
        store,
        subdir="reference",
        mem_id="mem_anchor1",
        body="Body.",
        extra_fm={"source_path": "daily/2026-04-29.md#chat_digest-2026-04-29"},
    )

    report = MemoryLinter(store_dir=store).lint()

    assert len(report.stale_source_paths) == 0


def test_detects_zero_byte_stub(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    folder = store / "reference" / "people"
    folder.mkdir(parents=True)
    (folder / "ghost_stub.md").write_text("", encoding="utf-8")

    report = MemoryLinter(store_dir=store).lint()

    assert len(report.orphan_stubs) == 1
    assert report.orphan_stubs[0].path.endswith("ghost_stub.md")


def test_skips_date_wikilinks_and_bare_cluster_anchors(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(
        store,
        subdir="conscience",
        mem_id="mem_drift1",
        body="See [[2026-04-29]] and [[circuit_breaker_open_count]].",
    )

    report = MemoryLinter(store_dir=store).lint()

    assert report.total == 0


def test_resolves_path_wikilink_outside_store(tmp_path: Path) -> None:
    project_root = tmp_path
    store = project_root / "memory-store"
    workshop = project_root / "tars-workshop" / "2026-05-01"
    workshop.mkdir(parents=True)
    (workshop / "notes.md").write_text("workshop", encoding="utf-8")

    _seed_memory(
        store,
        subdir="project",
        mem_id="mem_path01",
        body="Pointer: [[tars-workshop/2026-05-01/notes.md]].",
    )

    report = MemoryLinter(store_dir=store, project_root=project_root).lint()

    assert report.total == 0


def test_skips_missing_workspace_paths(tmp_path: Path) -> None:
    """Wikilinks pointing into gitignored operator-private workspace dirs
    (tars-workshop, workspace) must not flag as broken on machines that
    don't have the workspace synced. CLAUDE.md hard rule: workspace files
    are per-machine."""
    project_root = tmp_path
    store = project_root / "memory-store"
    # Note: NO `tars-workshop/` directory created on disk.
    _seed_memory(
        store,
        subdir="project",
        mem_id="mem_wsmiss1",
        body=(
            "Body wikilink: [[tars-workshop/archive/2026-05-01/notes.md]]. "
            "Also a workspace pointer: [[workspace/scratch.md]]."
        ),
        extra_fm={"source_path": "tars-workshop/archive/2026-05-01/notes.md"},
    )

    report = MemoryLinter(store_dir=store, project_root=project_root).lint()

    assert report.total == 0


def test_resolves_repo_rooted_path_under_repo_root(tmp_path: Path) -> None:
    """Memories often write `source_path: tesseract/tars-workshop/...` —
    paths counted from the repo checkout root, not from TESSERACT_HOME.
    The linter must resolve those against `repo_root`, otherwise it
    doubles the `tesseract/` segment and falsely flags every such entry.
    """
    repo_root = tmp_path
    project_root = repo_root / "tesseract"
    store = project_root / "memory-store"
    target_dir = project_root / "tars-workshop" / "2026-05-04" / "build"
    target_dir.mkdir(parents=True)
    (target_dir / "notes.md").write_text("real notes", encoding="utf-8")

    _seed_memory(
        store,
        subdir="project",
        mem_id="mem_repo01",
        body="Source: [[tesseract/tars-workshop/2026-05-04/build/notes.md]].",
        extra_fm={"source_path": "tesseract/tars-workshop/2026-05-04/build/notes.md"},
    )

    report = MemoryLinter(
        store_dir=store, project_root=project_root, repo_root=repo_root
    ).lint()

    assert report.total == 0, report.as_dict()


def test_skips_vault_scheme_wikilinks(tmp_path: Path) -> None:
    """`[[vault:raw/...:chunk_N]]` is a vault scheme reference, not a
    filesystem path. Vault validation lives in vault_lint."""
    store = tmp_path / "memory-store"
    _seed_memory(
        store,
        subdir="user",
        mem_id="mem_vaultref",
        body="Quoted: [[vault:raw/2026-04/some-doc.md:chunk_9]].",
    )

    report = MemoryLinter(store_dir=store).lint()

    assert report.total == 0


def test_job_returns_ok_false_when_findings_exist(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(store, subdir="reference", mem_id="mem_real99", body="See [[mem_ghost00]].")

    bundle = SimpleNamespace(store=SimpleNamespace(store_dir=store))
    app = {"memory_bundle": bundle}
    ctx = JobContext(job_name="memory_lint", app=app)

    result = asyncio.run(MemoryLintJob().run(ctx))

    assert result.ok is False
    assert "wikilinks=1" in result.detail
    assert result.payload["broken_wikilinks"][0]["detail"] == "[[mem_ghost00]]"


def test_job_returns_ok_true_for_clean_store(tmp_path: Path) -> None:
    store = tmp_path / "memory-store"
    _seed_memory(store, subdir="reference", mem_id="mem_only01", body="No links here.")

    bundle = SimpleNamespace(store=SimpleNamespace(store_dir=store))
    app = {"memory_bundle": bundle}
    ctx = JobContext(job_name="memory_lint", app=app)

    result = asyncio.run(MemoryLintJob().run(ctx))

    assert result.ok is True
    assert result.payload["total"] == 0


def test_job_handles_missing_bundle(tmp_path: Path) -> None:
    ctx = JobContext(job_name="memory_lint", app={})
    result = asyncio.run(MemoryLintJob().run(ctx))
    assert result.ok is False
    assert "memory_bundle unavailable" in result.detail
