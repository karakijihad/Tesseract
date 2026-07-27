"""Code drift watcher — detect source edits the running backend hasn't loaded.

Sibling to ``config_watcher.py``. The supervisor only watches liveness via
``/api/health``; Python source already imported into the running process
keeps serving old bytecode after the operator edits a file. This watcher
periodically diffs the working tree against a boot-time snapshot and emits
a ``code_drift_detected`` envelope when something changes.

Detection input is the local working tree (``git rev-parse HEAD`` + dirty
file hashes), not GitHub. Remote pushes from another machine are
irrelevant — what matters is what's on disk in the process's own checkout.

Classification is path-based, never commit-message based:

- ``restart_required``: any Python file under ``tesseract/`` not in
  ``tesseract/mirror/src/`` changed → process must respawn.
- ``frontend_only``: changes confined to ``tesseract/mirror/src/`` or
  ``.css`` / ``.tsx`` / ``.ts`` / ``.html`` anywhere → Vite HMR / bundle
  rebuild covers it; backend stays as-is.
- ``ignore``: docs, yaml configs (config_watcher handles those),
  markdown, lockfiles → silent.

Mixed buckets escalate to the highest tier present.

Auto-restart is opt-in via ``mirror.yaml::code_watch.auto_restart``;
default is detect-and-toast so the operator decides when to bounce a
mission-active backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from aiohttp import web

log = logging.getLogger(__name__)


Classification = Literal["restart_required", "frontend_only", "ignore"]

# Path globs (relative to repo root) for the three buckets.
# Order matters: restart_required wins over frontend_only wins over ignore.
_BACKEND_PY_PREFIX = "tesseract/"
_FRONTEND_SRC_PREFIX = "tesseract/mirror/src/"
_FRONTEND_SUFFIXES = (".css", ".tsx", ".ts", ".html", ".scss", ".jsx")
_IGNORE_PREFIXES = (
    "Docs/",
    "Research/",
    ".git/",
    ".github/",
    "Knowledge/",
)
_IGNORE_SUFFIXES = (".md", ".lock", ".gitignore")
_CONFIG_YAML_PREFIX = "tesseract/config/"


def _classify_path(rel_path: str) -> Classification:
    """Single-path classification. Public callers use ``classify_drift``."""
    norm = rel_path.replace("\\", "/").lstrip("./")

    # config_watcher owns these — silence so we don't double-toast.
    if norm.startswith(_CONFIG_YAML_PREFIX) and norm.endswith((".yaml", ".yml")):
        return "ignore"

    if any(norm.startswith(p) for p in _IGNORE_PREFIXES):
        return "ignore"
    if norm.endswith(_IGNORE_SUFFIXES):
        return "ignore"

    if norm.startswith(_FRONTEND_SRC_PREFIX):
        return "frontend_only"
    if norm.endswith(_FRONTEND_SUFFIXES):
        return "frontend_only"

    if norm.startswith(_BACKEND_PY_PREFIX) and norm.endswith(".py"):
        return "restart_required"

    # Repo-root scripts, top-level conftest, etc — treat as restart_required.
    if norm.endswith(".py"):
        return "restart_required"

    return "ignore"


def classify_drift(paths: list[str]) -> Classification:
    """Aggregate classification across a set of changed paths.

    Returns the highest tier present:
    ``restart_required > frontend_only > ignore``.
    Empty input → ``ignore``.
    """
    highest: Classification = "ignore"
    for p in paths:
        cls = _classify_path(p)
        if cls == "restart_required":
            return "restart_required"
        if cls == "frontend_only":
            highest = "frontend_only"
    return highest


@dataclass(frozen=True)
class TreeSnapshot:
    """Boot-time anchor we diff against on every tick."""

    head_sha: str
    # path → content hash. Only dirty / changed files are tracked; clean
    # files relative to HEAD are implied by ``head_sha``.
    dirty: dict[str, str]


EmitFn = Callable[[Classification, list[str], bool, bool, str | None], Awaitable[None]]
RestartFn = Callable[[list[str], str], Awaitable[None]]


@dataclass
class CodeWatcher:
    """Poll the working tree, emit ``code_drift_detected`` on change.

    Configure via ``mirror.yaml::code_watch``. Construct with the repo
    root and an async ``emit_fn`` (typically wired to fan out across all
    server sessions). Boot calls ``await start()``; shutdown calls
    ``await stop()``.

    The watcher is fail-open: a missing ``git`` binary, a non-repo tree,
    or any subprocess error logs once and disables future ticks. The
    backend never refuses to boot because the watcher couldn't snapshot.
    """

    repo_root: Path
    emit_fn: EmitFn
    interval_seconds: float = 30.0
    auto_restart: bool = False
    restart_fn: RestartFn | None = None
    path_cap: int = 25

    _snapshot: TreeSnapshot | None = field(default=None, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _disabled: bool = field(default=False, init=False)

    async def start(self) -> None:
        if self._task is not None:
            return
        # Belt-and-braces: the watcher contract is fail-open — never abort
        # backend boot because the snapshot step blew up. ``_run_git`` is
        # itself fail-open, but a future regression (or an unforeseen OSError
        # from the hash pass) shouldn't surface as "code_watcher: refused
        # to start" in the log either.
        try:
            snap = await self._snapshot_tree()
        except Exception:  # noqa: BLE001
            log.warning(
                "code_watcher: snapshot raised — watcher disabled (%s)",
                self.repo_root, exc_info=True,
            )
            self._disabled = True
            return
        if snap is None:
            log.warning(
                "code_watcher: initial snapshot failed — watcher disabled "
                "(git unavailable or not a repo at %s)",
                self.repo_root,
            )
            self._disabled = True
            return
        self._snapshot = snap
        log.info(
            "code_watcher: boot snapshot head=%s dirty=%d interval=%.0fs auto_restart=%s",
            snap.head_sha[:8] if snap.head_sha else "(none)",
            len(snap.dirty),
            self.interval_seconds,
            self.auto_restart,
        )
        self._task = asyncio.create_task(self._loop(), name="code_watcher:loop")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        log.info("code_watcher: stopped")

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
                return
            except asyncio.TimeoutError:
                pass
            if self._disabled:
                return
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("code_watcher: tick raised — continuing")

    async def _tick(self) -> None:
        prev = self._snapshot
        if prev is None:
            return
        current = await self._snapshot_tree()
        if current is None:
            # Transient git failure — keep the last snapshot, try again next tick.
            return
        head_drift = current.head_sha != prev.head_sha
        changed_paths = _diff_paths(prev, current)
        if not head_drift and not changed_paths:
            return

        # If only HEAD moved, surface the commit's changed paths so the
        # operator sees what shifted. ``_paths_between_revs`` is best-effort.
        if head_drift and not changed_paths:
            changed_paths = await self._paths_between_revs(prev.head_sha, current.head_sha)

        cls = classify_drift(changed_paths)
        if cls == "ignore":
            self._snapshot = current
            return

        dirty_drift = sorted(prev.dirty.items()) != sorted(current.dirty.items())
        capped = changed_paths[: self.path_cap]
        try:
            await self.emit_fn(cls, capped, head_drift, dirty_drift, current.head_sha or None)
        except Exception:
            log.exception("code_watcher: emit_fn raised")

        if cls == "restart_required" and self.auto_restart and self.restart_fn is not None:
            try:
                short = current.head_sha[:8] if current.head_sha else "dirty"
                await self.restart_fn(capped, short)
            except Exception:
                log.exception("code_watcher: auto restart_fn raised")

        self._snapshot = current

    async def _snapshot_tree(self) -> TreeSnapshot | None:
        head = await self._git_head_sha()
        if head is None:
            return None
        dirty = await self._dirty_hashes()
        if dirty is None:
            return None
        return TreeSnapshot(head_sha=head, dirty=dirty)

    async def _git_head_sha(self) -> str | None:
        rc, out, _ = await _run_git(["rev-parse", "HEAD"], cwd=self.repo_root)
        if rc != 0:
            return None
        return out.strip()

    async def _dirty_hashes(self) -> dict[str, str] | None:
        """``git status --porcelain`` gives us every modified/added/untracked
        path. We hash the on-disk content so save-and-revert is a no-op
        (mtime would still fire). Deleted paths get a sentinel hash so the
        diff sees them disappear.
        """
        rc, out, _ = await _run_git(
            ["status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=self.repo_root,
        )
        if rc != 0:
            return None
        result: dict[str, str] = {}
        # ``-z`` separates entries by NUL. Each entry is ``XY<space>path``.
        # For renames (``R``) or copies (``C``), the *next* NUL-delimited
        # token is the original path with no XY prefix — must be consumed
        # and skipped, not parsed as its own entry.
        tokens = out.split("\x00")
        i = 0
        while i < len(tokens):
            entry = tokens[i]
            i += 1
            if not entry:
                continue
            if len(entry) < 4:
                # Bare path token following an R/C — already consumed below.
                continue
            xy = entry[:2]
            path = entry[3:]
            if not path:
                continue
            # Consume the source path for rename/copy entries so it is not
            # re-parsed on the next iteration as an XY-prefixed path.
            if xy[0] in ("R", "C") and i < len(tokens):
                i += 1
            abs_path = (self.repo_root / path)
            try:
                if abs_path.is_file():
                    digest = _hash_file(abs_path)
                else:
                    digest = f"missing:{xy}"
            except OSError:
                digest = f"unreadable:{xy}"
            result[path.replace("\\", "/")] = digest
        return result

    async def _paths_between_revs(self, a: str, b: str) -> list[str]:
        """Files changed between two commit SHAs. Empty list on failure."""
        if not a or not b:
            return []
        rc, out, _ = await _run_git(
            ["diff", "--name-only", f"{a}..{b}"], cwd=self.repo_root,
        )
        if rc != 0:
            return []
        return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def _diff_paths(prev: TreeSnapshot, current: TreeSnapshot) -> list[str]:
    """Paths whose tracked state differs between two snapshots."""
    if prev.head_sha == current.head_sha and prev.dirty == current.dirty:
        return []
    keys = set(prev.dirty) | set(current.dirty)
    changed = [k for k in keys if prev.dirty.get(k) != current.dirty.get(k)]
    changed.sort()
    return changed


def _hash_file(path: Path) -> str:
    """SHA-1 of file contents. Small (<8 MB) files only; larger files are
    almost certainly assets we don't care about — return a size sentinel
    so they still register as "changed" if they grow/shrink."""
    try:
        size = path.stat().st_size
    except OSError:
        return "stat-error"
    if size > 8 * 1024 * 1024:
        return f"large:{size}"
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def _run_git(args: list[str], *, cwd: Path) -> tuple[int, str, str]:
    """Run ``git`` asynchronously; return (rc, stdout, stderr).

    Returns ``(-1, "", "")`` if git itself isn't on PATH, the cwd isn't a
    repo, the call times out, OR the Proactor event loop on Windows
    raises ``ProcessLookupError`` from ``proc.communicate()`` (a known
    asyncio race when the child exits during transport setup —
    cpython bpo-45034 and friends). Callers treat any non-zero return
    as fail-open, so we must NEVER raise out of here — a raised
    exception inside the code-drift snapshot path aborts watcher
    boot and surfaces as the confusing log line "code_watcher: refused
    to start — code drift will not surface."
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, FileNotFoundError):
        return -1, "", ""
    except Exception:  # noqa: BLE001
        # NotImplementedError on Selector loop on Windows, runtime
        # transport errors from the proactor, etc. Stay fail-open.
        log.debug("code_watcher: subprocess_exec raised", exc_info=True)
        return -1, "", ""
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        # Drain pipes so the proactor transport tears down cleanly and the
        # process doesn't linger as a zombie on Windows.
        try:
            await proc.communicate()
        except Exception:  # noqa: BLE001
            pass
        return -1, "", ""
    except Exception:  # noqa: BLE001
        # ProcessLookupError (child exited during transport teardown on
        # Windows proactor), ConnectionResetError on broken pipes, etc.
        log.debug("code_watcher: git communicate raised", exc_info=True)
        return -1, "", ""
    return proc.returncode or 0, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")


__all__ = [
    "CodeWatcher",
    "Classification",
    "TreeSnapshot",
    "classify_drift",
]
