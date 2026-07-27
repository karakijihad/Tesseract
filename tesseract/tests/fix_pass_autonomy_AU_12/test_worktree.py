"""AU-12 — worker worktree isolation tests.

Every test creates a private git repo at ``tmp_path/repo/`` (cheap —
``git init`` + one commit) and pins ``TESSERACT_HOME=tmp_path/home/``
so worktree paths land in the test sandbox. Production logs see zero
writes (verified by the project-wide ``git status -s tesseract/logs/``
gate). Test fixtures use the John/Jane Doe convention; no operator-real
names appear in any fixture content.

Tests:
* ``test_requires_worktree_matrix`` — pure decision over (risk, kind).
* ``test_create_and_archive_roundtrip`` — git worktree add → archive
  move → git worktree prune metadata cleanup.
* ``test_capture_diff_round_trips`` — edit + commit in worktree, diff
  capture contains the edit.
* ``test_is_stale_when_live_head_advances`` — base sha pinned at create,
  live HEAD advances, ``is_stale`` flips.
* ``test_finalize_writes_archived_patch`` — full lifecycle. Diff lands
  under ``worktrees-archive/<id>/diff.patch``.
* ``test_allocate_and_finalize_for_record`` — record-driven entrypoints.
* ``test_prune_archives_drops_done_after_retention`` — retention sweep
  prunes ``DONE`` but keeps ``FAILED`` indefinitely.
* ``test_worktree_create_refuses_when_path_exists`` — collision is a
  hard error, not silent overwrite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers import worktree as wt_mod
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)
from tesseract.orchestrator.workers.worktree import (
    DEFAULT_RETENTION_DAYS,
    WorkerWorktree,
    WorktreeError,
    allocate_for_record,
    finalize_for_record,
    prune_archives,
    requires_worktree,
    worktrees_archive_dir,
    worktrees_dir,
)


# --- fixtures ---------------------------------------------------------


def _git(args: list[str], *, cwd: Path) -> str:
    """Run git inside a fixture repo. Test fixtures need stable
    author identity per CLAUDE.md (no operator name); ``-c user.name``
    + ``-c user.email`` make commits deterministic regardless of the
    operator's global git config."""
    env_args = [
        "-c", "user.name=Jane Doe",
        "-c", "user.email=jane.doe@example.test",
        "-c", "init.defaultBranch=main",
    ]
    result = subprocess.run(  # noqa: S603
        ["git", *env_args, "-C", str(cwd), *args],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"test git {args} failed: {result.stderr.strip()}"
        )
    return result.stdout


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Fresh git repo with one commit on ``main``. Each test gets its
    own so worktrees + branches don't collide across runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(  # noqa: S603
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True, text=True, check=True,
    )
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    return repo


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ``TESSERACT_HOME`` to the test sandbox BEFORE any worktree
    helper resolves a path. Hard requirement per the project log-leak
    rule — workers/paths.py + worktree._home both resolve at call time
    so the env override sticks."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TESSERACT_HOME", str(home))
    return home


def _record(
    *,
    worker_id: str = "wk-2026-05-18-1200-claude_cli-aaaaaa",
    risk: RiskClass = RiskClass.OPERATOR_GATE,
    kind: WorkerKind = WorkerKind.CLAUDE_CLI,
    status: WorkerStatus = WorkerStatus.QUEUED,
    worktree_path: str | None = None,
    updated_at: datetime | None = None,
) -> WorkerRecord:
    """Minimal record fixture. Defaults to a code-editing CLI at
    OPERATOR_GATE so ``requires_worktree`` is True for the common
    happy-path test."""
    when = updated_at or datetime.now(timezone.utc)
    return WorkerRecord(
        id=worker_id,
        kind=kind,
        created_at=when,
        updated_at=when,
        agenda_item_id="ag-2026-05-18-1200-test01",
        risk_class=risk,
        role="claude_cli",
        status=status,
        worktree_path=worktree_path,
    )


# --- decision matrix --------------------------------------------------


def test_requires_worktree_matrix() -> None:
    """Code-editing kinds at PROPOSE/OPERATOR_GATE → True. Everything
    else (read-only kinds, AUTONOMOUS risk, ABSOLUTE_DENY) → False."""
    assert requires_worktree(RiskClass.PROPOSE, WorkerKind.CLAUDE_CLI)
    assert requires_worktree(RiskClass.OPERATOR_GATE, WorkerKind.CODEX_CLI)
    assert requires_worktree(RiskClass.PROPOSE, WorkerKind.CODEX_CLI)

    # Read-only kinds never get a worktree.
    assert not requires_worktree(RiskClass.PROPOSE, WorkerKind.TARS_SELF)
    assert not requires_worktree(
        RiskClass.OPERATOR_GATE, WorkerKind.MARKDOWN_AGENT,
    )

    # AUTONOMOUS items don't mutate even with a code-editing kind.
    assert not requires_worktree(RiskClass.AUTONOMOUS, WorkerKind.CLAUDE_CLI)
    # ABSOLUTE_DENY never dispatches at all; defence-in-depth False.
    assert not requires_worktree(RiskClass.ABSOLUTE_DENY, WorkerKind.CLAUDE_CLI)


# --- create / archive lifecycle ---------------------------------------


def test_create_and_archive_roundtrip(repo_root: Path) -> None:
    """Worktree spawns under ``<HOME>/worktrees/<id>/``; archive moves
    it to ``<HOME>/worktrees-archive/<id>/`` and clears the live path."""
    wt = WorkerWorktree.create(
        worker_id="wk-test-roundtrip", repo_root=repo_root,
    )

    assert wt.path == worktrees_dir() / "wk-test-roundtrip"
    assert wt.path.is_dir()
    assert (wt.path / "README.md").read_text(encoding="utf-8") == "hello\n"
    # Branch name is ``autonomy/<worker_id>``.
    branches = _git(["branch", "--list", "autonomy/*"], cwd=repo_root)
    assert "autonomy/wk-test-roundtrip" in branches

    archive_path = wt.archive()

    assert archive_path == worktrees_archive_dir() / "wk-test-roundtrip"
    assert archive_path.is_dir()
    assert (archive_path / "README.md").exists()
    assert not wt.path.exists()
    # git worktree metadata is cleaned up (no stale entry).
    worktrees = _git(["worktree", "list"], cwd=repo_root)
    assert "wk-test-roundtrip" not in worktrees


def test_worktree_create_refuses_when_path_exists(repo_root: Path) -> None:
    """Two workers minted with the same id (shouldn't happen — minted
    ids carry a 6-hex suffix) MUST fail loudly. Silent reuse would
    overwrite another worker's in-flight changes."""
    WorkerWorktree.create(
        worker_id="wk-test-collide", repo_root=repo_root,
    )
    with pytest.raises(WorktreeError, match="already exists"):
        WorkerWorktree.create(
            worker_id="wk-test-collide", repo_root=repo_root,
        )


# --- diff capture -----------------------------------------------------


def test_capture_diff_round_trips(repo_root: Path) -> None:
    """Make a commit inside the worktree; capture_diff writes the
    patch and its content reflects the edit."""
    wt = WorkerWorktree.create(
        worker_id="wk-test-diff", repo_root=repo_root,
    )

    (wt.path / "feature.txt").write_text("new file\n", encoding="utf-8")
    _git(["add", "feature.txt"], cwd=wt.path)
    _git(["commit", "-m", "add feature"], cwd=wt.path)

    diff_path = wt.capture_diff()

    assert diff_path is not None
    assert diff_path == wt.path / "diff.patch"
    payload = diff_path.read_text(encoding="utf-8")
    assert "feature.txt" in payload
    assert "+new file" in payload


def test_capture_diff_returns_none_when_no_changes(repo_root: Path) -> None:
    """Empty diff → ``None``. We don't want a zero-byte ``diff.patch``
    cluttering archives for workers that did nothing (rare, but the
    runner contract permits early bail)."""
    wt = WorkerWorktree.create(
        worker_id="wk-test-empty", repo_root=repo_root,
    )
    assert wt.capture_diff() is None
    assert not (wt.path / "diff.patch").exists()


def test_capture_diff_includes_uncommitted_changes(repo_root: Path) -> None:
    """A worker that bails before committing still leaves a trail —
    ``git diff HEAD`` in the worktree picks up both staged and
    unstaged changes against HEAD. Regression test for the AU-12
    reviewer finding that the original `git diff` (working-tree only)
    silently dropped staged-but-uncommitted edits."""
    wt = WorkerWorktree.create(
        worker_id="wk-test-pending", repo_root=repo_root,
    )
    # Staged but never committed.
    (wt.path / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(["add", "staged.txt"], cwd=wt.path)
    # Unstaged edit of the existing tracked file.
    (wt.path / "README.md").write_text("changed\n", encoding="utf-8")

    diff_path = wt.capture_diff()

    assert diff_path is not None
    payload = diff_path.read_text(encoding="utf-8")
    assert "staged.txt" in payload
    assert "+staged" in payload
    assert "README.md" in payload
    assert "+changed" in payload


# --- staleness --------------------------------------------------------


def test_is_stale_when_live_head_advances(repo_root: Path) -> None:
    """Live ``HEAD`` shifts forward (hot upgrade lands) → ``is_stale``
    flips to True. The worker's diff may no longer apply cleanly."""
    wt = WorkerWorktree.create(
        worker_id="wk-test-stale", repo_root=repo_root,
    )
    assert not wt.is_stale()

    # Hot upgrade lands on the live tree.
    (repo_root / "hotfix.txt").write_text("urgent\n", encoding="utf-8")
    _git(["add", "hotfix.txt"], cwd=repo_root)
    _git(["commit", "-m", "hotfix"], cwd=repo_root)

    assert wt.is_stale()


# --- finalize end-to-end ---------------------------------------------


def test_finalize_writes_archived_patch(repo_root: Path) -> None:
    """Full lifecycle — create → edit → finalize. Result captures
    diff path under the archive, base_sha + final_sha pin the bracket,
    stale flag matches reality (False here — live HEAD didn't move)."""
    wt = WorkerWorktree.create(
        worker_id="wk-test-finalize", repo_root=repo_root,
    )
    (wt.path / "out.txt").write_text("done\n", encoding="utf-8")
    _git(["add", "out.txt"], cwd=wt.path)
    _git(["commit", "-m", "result"], cwd=wt.path)

    result = wt.finalize(status=WorkerStatus.DONE)

    assert result.stale is False
    assert result.base_sha == result.final_sha
    assert result.archive_path == worktrees_archive_dir() / "wk-test-finalize"
    assert result.archive_path.is_dir()
    assert result.diff_path is not None
    assert result.diff_path.exists()
    assert "out.txt" in result.diff_path.read_text(encoding="utf-8")


# --- record entrypoints -----------------------------------------------


def test_allocate_for_record_skips_non_code_editing(
    repo_root: Path,
) -> None:
    """A ``markdown_agent`` record gets no worktree, no path mutation,
    and crucially no ``git worktree add`` call. ``None`` return is the
    signal to the kernel that this dispatch is regular."""
    record = _record(
        worker_id="wk-test-skip",
        risk=RiskClass.AUTONOMOUS,
        kind=WorkerKind.MARKDOWN_AGENT,
    )
    out = allocate_for_record(record, repo_root=repo_root)
    assert out is None
    assert record.worktree_path is None
    # No worktree dir was created.
    assert not (worktrees_dir() / record.id).exists()


def test_allocate_for_record_provisions_for_code_editing(
    repo_root: Path,
) -> None:
    """A claude_cli@operator_gate record gets a worktree, its path
    written back to ``record.worktree_path``, and the directory exists
    on disk."""
    record = _record(worker_id="wk-test-alloc")
    out = allocate_for_record(record, repo_root=repo_root)

    assert out is not None
    assert record.worktree_path == str(worktrees_dir() / "wk-test-alloc")
    assert Path(record.worktree_path).is_dir()


def test_finalize_for_record_idempotent(repo_root: Path) -> None:
    """Finalize twice — second call is a no-op (already archived)."""
    record = _record(worker_id="wk-test-final-idem")
    allocate_for_record(record, repo_root=repo_root)
    (Path(record.worktree_path) / "x.txt").write_text(
        "x\n", encoding="utf-8",
    )

    first = finalize_for_record(record, repo_root=repo_root)
    second = finalize_for_record(record, repo_root=repo_root)

    assert first is not None
    assert second is None
    # Worktree gone; archive present.
    assert not (worktrees_dir() / record.id).exists()
    assert (worktrees_archive_dir() / record.id).is_dir()


# --- retention --------------------------------------------------------


def test_prune_archives_drops_done_after_retention(
    repo_root: Path,
) -> None:
    """A ``DONE`` worktree older than retention gets pruned;
    a same-aged ``FAILED`` worktree stays put forever."""
    done_record = _record(
        worker_id="wk-test-prune-done",
        status=WorkerStatus.DONE,
    )
    failed_record = _record(
        worker_id="wk-test-prune-failed",
        status=WorkerStatus.FAILED,
    )

    # Allocate + archive both — each is now an aged archive entry.
    for record in (done_record, failed_record):
        allocate_for_record(record, repo_root=repo_root)
        finalize_for_record(record, repo_root=repo_root)

    archive_root = worktrees_archive_dir()
    # Reference wall-clock — ``st_atime`` conflates atime with mtime on
    # Windows when atime tracking is enabled and could compute a
    # threshold that masks a real retention bug.
    import time
    aged_ts = time.time() - (DEFAULT_RETENTION_DAYS + 2) * 86400.0
    for entry in archive_root.iterdir():
        os.utime(entry, (aged_ts, aged_ts))

    pruned = prune_archives(
        records={
            done_record.id: done_record,
            failed_record.id: failed_record,
        }
    )

    assert pruned == [done_record.id]
    assert not (archive_root / done_record.id).exists()
    assert (archive_root / failed_record.id).exists()


def test_prune_archives_keeps_done_within_retention(
    repo_root: Path,
) -> None:
    """A freshly-archived ``DONE`` worktree is kept until the retention
    threshold elapses — no eager deletion."""
    record = _record(
        worker_id="wk-test-prune-young",
        status=WorkerStatus.DONE,
    )
    allocate_for_record(record, repo_root=repo_root)
    finalize_for_record(record, repo_root=repo_root)

    pruned = prune_archives(records={record.id: record})

    assert pruned == []
    assert (worktrees_archive_dir() / record.id).exists()


def test_prune_archives_skips_entries_without_record(
    repo_root: Path,
) -> None:
    """Missing record (metadata gap) → entry preserved. Better to keep
    stale archives than lose a worker's output to a bookkeeping miss."""
    record = _record(
        worker_id="wk-test-orphan",
        status=WorkerStatus.DONE,
    )
    allocate_for_record(record, repo_root=repo_root)
    finalize_for_record(record, repo_root=repo_root)

    import time
    aged_ts = time.time() - (DEFAULT_RETENTION_DAYS + 2) * 86400.0
    os.utime(worktrees_archive_dir() / record.id, (aged_ts, aged_ts))

    pruned = prune_archives(records={})  # no record for this worker

    assert pruned == []
    assert (worktrees_archive_dir() / record.id).exists()
