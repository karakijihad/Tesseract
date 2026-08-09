"""Canonical filesystem anchors for the runtime.

Two distinct roots, deliberately separated for Phase 17 portability:

- ``TESSERACT_DIR``: the source-code package directory (where this file
  lives). Always anchored via ``__file__`` so it follows the install.
  Source code, default config files, and templates live here.
- ``TESSERACT_HOME``: the user-state root. Defaults to ``TESSERACT_DIR``
  so dev checkouts behave as before. When the ``TESSERACT_HOME``
  environment variable is set (e.g. ``~/.tesseract`` on a packaged
  install), all derived user-state directories — ``memory-store/``,
  ``agents/``, ``vault/``, ``logs/``, ``sessions/`` — relocate together.

Importing from this module avoids cycles. Modules deeper in the tree
(``cost/ledger.py``, ``mirror/server/routes/...``) used to compute their
own ``Path(__file__).resolve().parents[N]`` constant and never honored
the env var; importing ``TESSERACT_HOME`` from here fixes that without
each module touching the brain stack.
"""

from __future__ import annotations

import os
from pathlib import Path

TESSERACT_DIR = Path(__file__).resolve().parent
ROOT = TESSERACT_DIR.parent
TESSERACT_HOME = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_DIR).resolve()
# Back-compat alias, frozen at import like TESSERACT_HOME above. Still used by
# consumers imported once at process start (e.g. `brain/boot.py`,
# `mirror/server/config.py`) that don't need to follow a later env change.
# New call-time code should use `config_dir()` below instead (see module
# docstring — same reasoning as `home_dir()`/`workspace_dir()`/`agents_dir()`).
CONFIG_DIR = TESSERACT_HOME / "config"


def _home_at_call_time() -> Path:
    """Resolve TESSERACT_HOME at call time, honoring a `TESSERACT_HOME` env
    override applied AFTER import (used by tests that point the runtime at
    a tmp_path, and by packaged installs where the module-level constant
    above was already frozen at import)."""
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def home_dir() -> Path:
    """Public alias for `_home_at_call_time`, for consumers outside this
    module (e.g. route handlers)."""
    return _home_at_call_time()


def workspace_dir() -> Path:
    """the assistant's writable workspace (SOUL/DIARY/...). Call-time so updates
    replacing the code tree never touch it."""
    return _home_at_call_time() / "workspace"


def agents_dir() -> Path:
    """Agent cards — operator-created agents are state, not code."""
    return _home_at_call_time() / "agents"


def config_dir() -> Path:
    """Config tree — call-time so a `TESSERACT_HOME` change is honored
    without a fresh import, unlike the frozen `CONFIG_DIR` constant above."""
    return _home_at_call_time() / "config"


def install_root() -> Path:
    """Parent of `home/`, `app/`, and `runtime/` — the READ boundary.

    In a packaged install this is `%LOCALAPPDATA%\\com.tesseract.mirror`. In a
    dev checkout `home_dir()` is the source package, so this is the repo root
    and the layout collapses to today's behaviour.
    """
    return _home_at_call_time().parent


def app_dir() -> Path:
    """The sealed application tree: code plus factory templates.

    Written only by the updater (`repo.rs`). Outside the write boundary for
    every agent, which is what makes the seal the absence of authority rather
    than a special case.
    """
    return install_root() / "app"


def runtime_dir() -> Path:
    """Machine-local machinery: venv, caches, pidfiles, ops logs, markers.

    Never synced between machines, never shipped. Outside the write boundary
    for agents, but the runtime's own writers (supervisor, janitor, circuit
    breakers) write here directly without passing through `decide.evaluate`.
    """
    return install_root() / "runtime"


# `logs/` is not one thing. Half of it is the operator's record and follows
# them between machines; half is this machine's operational output and never
# leaves it. Kept as data in one place so a new category is a decision made
# here rather than guessed at a call site.
_HOME_LOG_DIRS = frozenset(
    {
        "sessions", "observer", "conscience", "autonomy", "consolidator",
        "feedback-sweep", "skills", "schedule", "channels", "workspace",
    }
)
_RUNTIME_LOG_DIRS = frozenset(
    {
        "audit", "circuit-breakers", "supervisor", "janitor", "provider-health",
        "tokenjuice", "governor",
    }
)


# The state paths a tool-written artifact can land in. `file_write` anchors
# every relative path at the state root, so a path the runtime hands back
# ("Written to <home>/downloads/paper.pdf") names one of these; the read tools
# follow them there when the code tree has no such entry.
#
# Deliberately NOT "every directory under home". `.env`, `config/` and the
# runtime trees also live there, and read tools carry no `path_overrides` —
# `permissions.yaml` scopes paths for `file_write` only. An unbounded second
# anchor would make `file_read(".env")` resolve to the operator's API keys by
# a bare relative path that resolves to nothing today.
#
# Prefixes, not bare directory names, because the write side is not uniform.
# `permissions.yaml` grants `file_write` AUTO on `logs/sessions/` but on no
# other part of `logs/`, and on `vault/raw/` but not the wiki; `workspace/`
# carries thirteen DENY rules over the operator's own identity files. Matching
# whole segments off a directory name would either miss the one writable log
# surface — leaving exactly the write-then-read asymmetry this list exists to
# remove — or hand the read tools paths the write side explicitly refuses.
#
# Two rules for adding an entry, both load-bearing:
#   1. It must be somewhere `file_write` can legitimately land an artifact —
#      AUTO for `workshop/`, `vault/raw/` and `logs/sessions/`; the default
#      ASK posture for `downloads/` and `uploads/`, which are artifact sinks
#      the operator is handed paths to.
#   2. It must contain no path carrying a DENY or narrower override. This is
#      why `workspace` is absent: `workspace/SOUL.md`, `IDENTITY.md`,
#      `USER.md`, `VOICE.md` and nine more are DENY for write, and a read
#      allowlist entry would reach every one of them.
#
# Kept as data here, beside the log-category split above, for the same reason:
# a new state path is a decision made in this file rather than guessed at a
# call site.
# `memory-store` is deliberately ABSENT despite being an AUTO `file_write`
# prefix. `memory_get` exists to be the read path for it — its docstring says
# so in as many words ("instead of widening `file_read` to cover the memory
# store") and it enforces markdown-only plus an identity-file block on
# `MEMORY.md` / `WHAT_NOT_TO_SAVE.md`. Listing it here would hand the generic
# read tools the access that tool was written to withhold, and `glob`/`grep`
# would enumerate and search the same files. The write-then-read asymmetry
# therefore stands for memory-store on purpose: the read half has an owner.
READABLE_STATE_PREFIXES: tuple[str, ...] = (
    "downloads",
    "uploads",
    "workshop",
    "vault/raw",
    "logs/sessions",
)


def readable_state_prefix(relative_posix: str) -> str | None:
    """The `READABLE_STATE_PREFIXES` entry covering `relative_posix`, or None.

    Case-insensitive: the target filesystem is NTFS, where `Downloads/x.md`
    and `downloads/x.md` are the same file, so an exact-case membership test
    would refuse a read of a path `file_write` had just accepted.
    """
    candidate = relative_posix.replace("\\", "/").casefold().strip("/")
    for prefix in READABLE_STATE_PREFIXES:
        folded = prefix.casefold()
        if candidate == folded or candidate.startswith(folded + "/"):
            return prefix
    return None


def home_logs_root() -> Path:
    """`home/logs` — the half of the log tree that follows the operator."""
    return home_dir() / "logs"


def runtime_logs_root() -> Path:
    """`runtime/logs` — machine ops output, never synced."""
    return runtime_dir() / "logs"


def log_dir(category: str) -> Path:
    """Resolve one log category to whichever half owns it.

    Raises on an unknown category rather than defaulting: a silent default
    would put operator history on the wrong side of the sync boundary, and
    that is invisible until the second machine is missing it.
    """
    if category in _HOME_LOG_DIRS:
        return home_logs_root() / category
    if category in _RUNTIME_LOG_DIRS:
        return runtime_logs_root() / category
    raise KeyError(
        f"unknown log category {category!r} — add it to _HOME_LOG_DIRS "
        "(follows the operator) or _RUNTIME_LOG_DIRS (machine-local) in "
        "tesseract/paths.py; do not guess at the call site."
    )


def is_installed_tree() -> bool:
    """True iff this process is running from a packaged install's code
    checkout, never a dev checkout.

    Packaged layout (`mirror/src-tauri/src/provision.rs::tesseract_home` +
    `clone_app_dir`): the shell points `TESSERACT_HOME` at the ``home/``
    sibling and clones the production repo into ``app/`` beside it — so this
    package's ``ROOT`` (``TESSERACT_DIR.parent``) equals ``app_dir()``. In a
    dev checkout, ``TESSERACT_HOME`` is either unset (``home_dir() ==
    TESSERACT_DIR``, so ``app_dir()`` is the repo's sibling and never equals
    the repo's own parent) or an operator-chosen override — neither shape
    coincides with the packaged equality by accident.

    Path equality alone still isn't proof: an operator could point
    ``TESSERACT_HOME`` somewhere that happens to line up. The provisioning
    marker (``runtime/provisioned.json``, written once by
    ``provision.rs::write_marker`` at the end of a REAL first-run install,
    never produced by anything a dev checkout runs) must also be present. A
    false "installed" verdict is the worst outcome here — it would refuse the
    operator's own dev-checkout source edits — so this predicate ANDs both
    signals rather than trusting either alone.
    """
    try:
        root = ROOT.resolve()
        candidate = app_dir().resolve()
    except OSError:
        return False
    if os.path.normcase(str(root)) != os.path.normcase(str(candidate)):
        return False
    return (runtime_dir() / "provisioned.json").is_file()
