"""Regression tests for the five vault_lint reviewer findings (2026-05-03).

  1. Job returns ok=False when the tool result lacks `lint_report` metadata
     (no longer silently passes when the tool's contract drifts).
  2. `_is_stale` does not probe paths that escape the vault root via `..`.
  3. `_page_summary` returns the first text paragraph even when the body
     has no leading H1 (previously dropped it via a blind `paragraphs[1]`).
  4. `_append_lint_report` serialises concurrent writers via an O_EXCL
     lockfile (no read-modify-write entry loss).
  5. Schedule cadence: vault_lint fires AFTER librarian_heartbeat.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from tesseract.kernel.tools.base import ToolResult
from tesseract.memory.vault_lint import (
    MissingHubFinding,
    VaultLinter,
    _exclusive_lock,
    _is_stale,
    _page_summary,
)
from tesseract.scheduler.tasks.vault_lint import VaultLintJob
from tesseract.scheduler.types import JobContext


# ── Fix 1: scheduler guards missing lint_report ──────────────────────────


class _FakeTool:
    def __init__(self, result: ToolResult) -> None:
        self._result = result

    async def run(self, _input, _ctx):
        return self._result


def _ctx(tool: _FakeTool) -> JobContext:
    registry = SimpleNamespace(tools={"vault_lint": tool})
    return JobContext(job_name="vault_lint", app={"tool_registry": registry})


def test_job_ok_false_when_metadata_missing_lint_report() -> None:
    tool = _FakeTool(ToolResult(output="oops", metadata={}))
    result = asyncio.run(VaultLintJob().run(_ctx(tool)))
    assert result.ok is False
    assert "no lint_report" in result.detail


def test_job_ok_false_when_tool_errors_even_with_metadata() -> None:
    tool = _FakeTool(ToolResult(output="boom", is_error=True, metadata={"lint_report": {}}))
    result = asyncio.run(VaultLintJob().run(_ctx(tool)))
    assert result.ok is False
    assert "is_error=True" in result.detail


def test_job_ok_true_for_clean_lint_report() -> None:
    payload = {
        "orphans": [], "stale": [], "contradictions": [],
        "missing_hubs": [], "scale_alarm": False, "failures": [],
    }
    tool = _FakeTool(ToolResult(output="ok", metadata={"lint_report": payload}))
    result = asyncio.run(VaultLintJob().run(_ctx(tool)))
    assert result.ok is True
    assert "failures=0" in result.detail


def test_job_ok_false_when_pass_failures_present() -> None:
    payload = {
        "orphans": [], "stale": [], "contradictions": [],
        "missing_hubs": [], "scale_alarm": False,
        "failures": ["contradict: adapter error"],
    }
    tool = _FakeTool(ToolResult(output="ok", metadata={"lint_report": payload}))
    result = asyncio.run(VaultLintJob().run(_ctx(tool)))
    assert result.ok is False
    assert "failures=1" in result.detail


# ── Fix 3: _is_stale path-traversal bound to vault root ──────────────────


def test_is_stale_rejects_traversal_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("real file outside the vault", encoding="utf-8")

    fm = {"source_path": "../outside.md", "date_added": "2026-04-22"}
    # The file outside the vault DOES exist, but the linter must still
    # flag the entry as stale because the path escapes the vault root.
    assert _is_stale(vault, fm, grace_days=180) is True


def test_is_stale_resolves_real_in_vault_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    raw = vault / "raw" / "2026-04"
    raw.mkdir(parents=True)
    (raw / "doc.md").write_text("real", encoding="utf-8")

    fm = {"source_path": "raw/2026-04/doc.md", "date_added": "2026-04-22"}
    assert _is_stale(vault, fm, grace_days=180) is False


# ── Fix 4: _page_summary keeps first text paragraph ──────────────────────


class _FakeManager:
    def __init__(self, fm: dict, body: str) -> None:
        self._fm = fm
        self._body = body

    def read_wiki_page_frontmatter(self, _slug: str) -> dict:
        return self._fm

    def read_wiki_page(self, _slug: str) -> str:
        return self._body


def test_page_summary_with_leading_heading() -> None:
    body = "---\ntitle: T\n---\n\n# Title Heading\n\nFirst real paragraph.\n\nSecond paragraph."
    out = _page_summary(_FakeManager({"title": "T"}, body), "slug")
    assert "First real paragraph" in out
    assert "Title Heading" not in out


def test_page_summary_without_leading_heading_keeps_first_paragraph() -> None:
    """Pre-fix code returned paragraphs[1], dropping this entire summary."""
    body = "---\ntitle: T\n---\n\nThe one and only paragraph that summarises the source."
    out = _page_summary(_FakeManager({"title": "T"}, body), "slug")
    assert "The one and only paragraph" in out


def test_page_summary_empty_body() -> None:
    body = "---\ntitle: T\n---\n\n"
    out = _page_summary(_FakeManager({"title": "T"}, body), "slug")
    assert out == "Title: T\n"


# ── Fix 5: _append_lint_report lock serialises overlapping writers ───────


def test_exclusive_lock_blocks_overlapping_acquires(tmp_path: Path) -> None:
    target = tmp_path / "LINT-REPORT.md"
    target.touch()
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with _exclusive_lock(target, timeout_s=2.0):
            held.set()
            release.wait(timeout=2.0)

    t = threading.Thread(target=hold_lock)
    t.start()
    assert held.wait(timeout=1.0)

    # Second acquisition while the first is held → TimeoutError.
    with pytest.raises(TimeoutError):
        with _exclusive_lock(target, timeout_s=0.2):
            pass

    release.set()
    t.join(timeout=2.0)

    # Lock released → second acquisition succeeds.
    with _exclusive_lock(target, timeout_s=1.0):
        pass


def test_exclusive_lock_reclaims_stale_lockfile(tmp_path: Path) -> None:
    target = tmp_path / "LINT-REPORT.md"
    target.touch()
    lock = target.with_name(target.name + ".lock")
    # Simulate a crashed-process lock file by creating one with a very old mtime.
    lock.touch()
    import os as _os
    old = 0.0  # epoch start — definitely stale.
    _os.utime(str(lock), (old, old))

    with _exclusive_lock(target, timeout_s=0.5, stale_after_s=1.0):
        pass


def test_append_lint_report_preserves_history_under_lock(tmp_path: Path) -> None:
    """End-to-end: appending two batches keeps both entries (no overwrite)."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    manager = SimpleNamespace(wiki_dir=wiki)
    linter = VaultLinter.__new__(VaultLinter)
    linter._manager = manager  # type: ignore[attr-defined]

    batch_a = [MissingHubFinding(term="alpha", mention_count=4, suggested_slug="alpha")]
    batch_b = [MissingHubFinding(term="beta", mention_count=5, suggested_slug="beta")]
    linter._append_lint_report(batch_a, "2026-05-03")
    linter._append_lint_report(batch_b, "2026-05-04")

    text = (wiki / "LINT-REPORT.md").read_text(encoding="utf-8")
    assert "alpha" in text
    assert "beta" in text
    assert text.startswith("# Vault Lint Report")


# ── Fix 2: schedule.yaml cadence ordering ────────────────────────────────


def test_schedule_yaml_runs_vault_lint_after_librarian_heartbeat() -> None:
    schedule_path = (
        Path(__file__).resolve().parents[2] / "config" / "schedule.yaml"
    )
    data = yaml.safe_load(schedule_path.read_text(encoding="utf-8"))
    cadences = {job["name"]: job["cadence"] for job in data["jobs"]}

    def _slot(cron: str) -> tuple[int, int]:
        minute, hour, *_ = cron.split()
        return (int(hour), int(minute))

    assert _slot(cadences["vault_lint"]) > _slot(cadences["librarian_heartbeat"])
    assert _slot(cadences["vault_lint"]) > _slot(cadences["index_rebuild"])
