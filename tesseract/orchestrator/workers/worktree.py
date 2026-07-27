"""Optional git worktree isolation for code-changing workers.

A worker that edits source (``claude_cli``/``codex_cli`` running at
``risk_class=propose`` or ``operator_gate``) gets its own git worktree
at ``<TESSERACT_HOME>/worktrees/<worker_id>/`` branched from the live
``HEAD``. The worker runs inside that worktree; its diff against the
base commit is captured at terminal completion and the worktree is
archived to ``<TESSERACT_HOME>/worktrees-archive/<worker_id>/`` for
operator inspection. UpgradeManager consumes the captured ``diff.patch``
through the existing upgrade-proposal flow — this module owns
allocation, diff capture, and archive policy only.

Retention policy:

* ``DONE`` workers — archive auto-prunes after ``DEFAULT_RETENTION_DAYS``
  (7) so a long-running install doesn't accumulate thousands of patches.
* Every other terminal status (``FAILED`` / ``BLOCKED`` / ``INTERRUPTED``
  / ``CANCELLED``) — keep indefinitely so the operator can recover the
  worker's partial work without time pressure.

Staleness:

* The worktree records the live ``HEAD`` sha at create time as
  ``base_sha``. On finalize, ``is_stale`` compares it to the current
  live ``HEAD``; mismatch means a hot upgrade landed on the live tree
  between worktree spawn and worker completion. The captured patch may
  no longer apply cleanly — UpgradeManager surfaces ``worktree_stale``
  to the operator and lets them rebase-or-retry.

The git CLI is shelled out (``git -C <repo> worktree …``) rather than
adding a dependency. Failures are wrapped in ``WorktreeError`` with the
underlying stderr so the dashboard can show the operator what went
wrong. No log lines under ``tesseract/logs/`` — the worktree dir IS the
durable record; status surfaces via ``WorkerRecord.worktree_path`` and
the archived patch file.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.paths import (
    worktrees_archive_dir,
    worktrees_dir,
)
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)

log = logging.getLogger(__name__)


DEFAULT_RETENTION_DAYS = 7

# Branch name template — every worktree spawns a branch under this
# prefix so the operator can ``git branch --list 'autonomy/*'`` to see
# what's in flight, and `_branch_name` is the single rename point.
_BRANCH_PREFIX = "autonomy"


def _branch_name(worker_id: str) -> str:
    return f"{_BRANCH_PREFIX}/{worker_id}"


# Worker kinds that may edit source. PROPOSE/OPERATOR_GATE items
# dispatched to either of these get an isolated worktree.
_CODE_EDITING_KINDS: frozenset[WorkerKind] = frozenset(
    {WorkerKind.CLAUDE_CLI, WorkerKind.CODEX_CLI}
)

# Risk classes that warrant isolation. AUTONOMOUS items don't edit
# code; ABSOLUTE_DENY never dispatches.
_CODE_EDITING_RISK: frozenset[RiskClass] = frozenset(
    {RiskClass.PROPOSE, RiskClass.OPERATOR_GATE}
)


class WorktreeError(RuntimeError):
    """Raised when git refuses an operation (add / remove / diff)."""


def requires_worktree(risk_class: RiskClass, kind: WorkerKind) -> bool:
    """``True`` when the worker should run in an isolated worktree.

    Decision matrix: a code-editing kind (claude_cli / codex_cli)
    dispatched at PROPOSE or OPERATOR_GATE risk. Read-only kinds
    (markdown_agent / tars_self) never get a worktree; AUTONOMOUS
    items skip even if the kind edits code because the operator
    profile expects autonomous == observation, not mutation.
    """
    return kind in _CODE_EDITING_KINDS and risk_class in _CODE_EDITING_RISK


def _run_git(
    args: list[str], *, cwd: Path, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command with stdout+stderr captured. ``check=True``
    raises :class:`WorktreeError` on non-zero exit with the stderr
    payload attached — failure messages reach the dashboard verbatim."""
    result = subprocess.run(  # noqa: S603 — args list, no shell
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _resolve_head(repo_root: Path) -> str:
    """Live ``HEAD`` sha — call-time, no cache. UpgradeManager and
    AU-12 finalize both rely on this returning the *current* tip, not
    a snapshot, so a hot upgrade landing between spawn and finalize is
    visible."""
    return _run_git(["rev-parse", "HEAD"], cwd=repo_root).stdout.strip()


@dataclass(frozen=True)
class WorktreeFinalizeResult:
    """One worktree's finalize outcome. ``diff_path`` is ``None`` when
    the worker made no changes; ``stale`` is ``True`` when the live
    repo's ``HEAD`` moved between spawn and finalize."""

    archive_path: Path
    diff_path: Path | None
    stale: bool
    base_sha: str
    final_sha: str


class WorkerWorktree:
    """Lifecycle wrapper around one worker's worktree.

    Typical flow::

        wt = WorkerWorktree.create(worker_id="wk-...", repo_root=...)
        record.worktree_path = str(wt.path)
        # ... worker runs inside wt.path ...
        result = wt.finalize(status=WorkerStatus.DONE)
        # ``result.diff_path`` is the captured patch; ``result.stale``
        # tells UpgradeManager whether a rebase is needed.

    The branch name is ``autonomy/<worker_id>`` so two workers can't
    collide and the operator can ``git branch`` to see what's in flight.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        path: Path,
        base_sha: str,
        repo_root: Path,
    ) -> None:
        self.worker_id = worker_id
        self.path = path
        self.base_sha = base_sha
        self.repo_root = repo_root

    @classmethod
    def create(
        cls,
        *,
        worker_id: str,
        repo_root: Path | None = None,
        base_sha: str | None = None,
    ) -> "WorkerWorktree":
        """Spawn a new worktree. Branches from ``base_sha`` (default:
        live ``HEAD``) so the worker sees the same state that admission
        observed. Raises :class:`WorktreeError` if git refuses (e.g.
        path already taken, dirty index in the live tree)."""
        repo = (repo_root or _resolve_repo_root()).resolve()
        path = worktrees_dir() / worker_id
        if path.exists():
            raise WorktreeError(
                f"worktree path already exists: {path} "
                f"(worker_id={worker_id!r})"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Clear any orphaned ``.git/worktrees/<id>`` admin entries from a
        # crashed prior run before adding. Cheap (~1ms) and idempotent;
        # prevents `git worktree add` from refusing on stale metadata.
        _run_git(["worktree", "prune"], cwd=repo, check=False)
        sha = base_sha or _resolve_head(repo)
        _run_git(
            ["worktree", "add", "-b", _branch_name(worker_id), str(path), sha],
            cwd=repo,
        )
        return cls(
            worker_id=worker_id, path=path, base_sha=sha, repo_root=repo,
        )

    def capture_diff(self) -> Path | None:
        """Write ``<worktree>/diff.patch`` containing the worker's
        full delta against ``base_sha``. Returns the path on success or
        ``None`` if the worker made zero changes (empty diff).

        Two ranges, concatenated:

        * ``git diff <base>..HEAD`` — changes the worker committed
          inside the worktree.
        * ``git diff HEAD`` — changes still pending in the worktree
          (both staged and unstaged are visible against HEAD).

        The worker contract is "commit before exit," but the second
        range catches the bail-early case so an operator inspecting
        the archived patch sees everything the worker actually touched.
        """
        committed = _run_git(
            ["diff", f"{self.base_sha}..HEAD"], cwd=self.path,
        ).stdout
        pending = _run_git(["diff", "HEAD"], cwd=self.path).stdout
        combined = committed + pending
        if not combined.strip():
            return None
        diff_path = self.path / "diff.patch"
        diff_path.write_text(combined, encoding="utf-8")
        return diff_path

    def is_stale(self, *, current_head: str | None = None) -> bool:
        """``True`` when the live tree's ``HEAD`` differs from this
        worktree's ``base_sha`` — a hot upgrade landed mid-flight."""
        head = current_head or _resolve_head(self.repo_root)
        return head != self.base_sha

    def archive(self) -> Path:
        """Move the worktree directory into ``worktrees-archive/<worker_id>/``
        and clean up git's bookkeeping. The directory contents (including
        any captured ``diff.patch``) survive — operator inspects later.

        Order matters: we move the tree FIRST, then ``git worktree prune``
        cleans the now-orphaned ``.git/worktrees/<name>/`` admin entry.
        ``git worktree remove --force`` would delete the directory
        contents, which is exactly what we don't want.

        The companion ``autonomy/<worker_id>`` branch is left intact so
        the operator can ``git checkout`` to inspect what the worker
        built without restoring from the archive copy.
        """
        archive_path = worktrees_archive_dir() / self.worker_id
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if archive_path.exists():
            raise WorktreeError(
                f"archive path already exists: {archive_path}"
            )
        if self.path.exists():
            shutil.move(str(self.path), str(archive_path))
        else:
            archive_path.mkdir(parents=True, exist_ok=True)
        # Tell git the worktree is gone so ``git worktree list`` no
        # longer shows a stale row. ``prune`` is the metadata-only
        # cleanup — it never touches the file tree we just moved.
        _run_git(
            ["worktree", "prune"], cwd=self.repo_root, check=False,
        )
        return archive_path

    def finalize(
        self, *, status: WorkerStatus,
    ) -> WorktreeFinalizeResult:
        """Capture diff + check staleness + archive. The single call
        site the kernel / runner uses at terminal completion. Returns
        a structured result so the caller can update the record + emit
        a workspace event."""
        diff_path = self.capture_diff()
        final_sha = _resolve_head(self.repo_root)
        stale = final_sha != self.base_sha
        archive_path = self.archive()
        # If the diff was captured BEFORE the archive move, its path is
        # now stale — recompute against the archive location for the
        # caller. ``diff.patch`` is at ``<worktree-or-archive>/``.
        if diff_path is not None:
            diff_path = archive_path / "diff.patch"
        log.info(
            "worktree finalize: worker=%s status=%s stale=%s diff=%s",
            self.worker_id, status.value, stale, diff_path is not None,
        )
        return WorktreeFinalizeResult(
            archive_path=archive_path,
            diff_path=diff_path,
            stale=stale,
            base_sha=self.base_sha,
            final_sha=final_sha,
        )


def _resolve_repo_root() -> Path:
    """Walk up from cwd until ``.git`` appears. Used when the caller
    doesn't pin ``repo_root`` explicitly — production wiring should
    always pin it from config; tests pin it to a tmp git repo fixture."""
    cur = Path.cwd().resolve()
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            raise WorktreeError(
                f"no git repo found walking up from {Path.cwd()}"
            )
        cur = cur.parent


def allocate_for_record(
    record: WorkerRecord, *, repo_root: Path | None = None,
) -> WorkerWorktree | None:
    """Allocate a worktree if the record's (risk_class, kind) demands
    isolation. Returns the worktree on allocation (and mutates
    ``record.worktree_path``), or ``None`` when no worktree is needed.

    The kernel calls this in ``_dispatch_item`` after ``build_worker_record``
    but BEFORE the first ``write_record`` so the path lands on disk
    atomically with the SPAWNING transition."""
    if not requires_worktree(record.risk_class, record.kind):
        return None
    wt = WorkerWorktree.create(
        worker_id=record.id, repo_root=repo_root,
    )
    record.worktree_path = str(wt.path)
    return wt


def finalize_for_record(
    record: WorkerRecord, *, repo_root: Path | None = None,
) -> WorktreeFinalizeResult | None:
    """Mirror of :func:`allocate_for_record`. Looks up the worker's
    worktree by id (the path on the record is informational; the
    canonical address is ``worktrees_dir() / record.id``) and finalizes
    it. ``None`` when no worktree was allocated for this worker.

    Idempotent: called twice in a row, the second call returns ``None``
    because the worktree dir is already archived."""
    if record.worktree_path is None:
        return None
    path = worktrees_dir() / record.id
    if not path.exists():
        # Already finalized OR never created. Either way, nothing to do.
        return None
    repo = (repo_root or _resolve_repo_root()).resolve()
    # Rehydrate base_sha from the branch's merge-base against HEAD.
    # The branch name encodes worker_id so we don't need to persist
    # base_sha on the record.
    base = _run_git(
        ["merge-base", _branch_name(record.id), "HEAD"],
        cwd=repo, check=False,
    ).stdout.strip()
    if not base:
        # Branch missing OR merge-base failed. We still want finalize
        # to run so the worktree gets archived — but using HEAD as the
        # base would silently mask a stale-base condition (final_sha ==
        # base → stale=False) and report an empty diff. Warn the
        # operator so they know the staleness signal can't be trusted.
        log.warning(
            "worktree finalize: merge-base failed for %s; "
            "stale flag may be inaccurate",
            record.id,
        )
        base = _resolve_head(repo)
    wt = WorkerWorktree(
        worker_id=record.id, path=path, base_sha=base, repo_root=repo,
    )
    return wt.finalize(status=record.status)


def iter_archive_entries() -> Iterable[tuple[str, Path, datetime]]:
    """Yield ``(worker_id, archive_path, mtime_utc)`` for every entry
    under ``worktrees-archive/``. Used by :func:`prune_archives`. Skips
    non-directories so a stray file doesn't trip the prune."""
    root = worktrees_archive_dir()
    if not root.exists():
        return
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        mtime = datetime.fromtimestamp(
            entry.stat().st_mtime, tz=timezone.utc,
        )
        yield entry.name, entry, mtime


def prune_archives(
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    records: dict[str, WorkerRecord] | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Prune ``done``-status worktrees older than ``retention_days``.

    ``records`` maps ``worker_id`` → :class:`WorkerRecord` so the prune
    can read the worker's status without re-walking the workers tree.
    If a record is missing for an archived worktree, the entry is left
    in place — better to keep stale archives than discard a worker's
    work because of a metadata gap.

    Returns the list of pruned worker ids. Non-``done`` terminals
    (``failed`` / ``blocked`` / ``interrupted`` / ``cancelled``) are
    NEVER pruned, regardless of age — the operator may need them to
    recover lost work.
    """
    threshold_dt = (now or datetime.now(timezone.utc))
    pruned: list[str] = []
    for worker_id, entry, mtime in iter_archive_entries():
        record = (records or {}).get(worker_id)
        if record is None or record.status != WorkerStatus.DONE:
            continue
        age_days = (threshold_dt - mtime).total_seconds() / 86400.0
        if age_days < retention_days:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        pruned.append(worker_id)
    return pruned


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "WorkerWorktree",
    "WorktreeError",
    "WorktreeFinalizeResult",
    "allocate_for_record",
    "finalize_for_record",
    "iter_archive_entries",
    "prune_archives",
    "requires_worktree",
    "worktrees_archive_dir",
    "worktrees_dir",
]
