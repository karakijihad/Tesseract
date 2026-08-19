"""Canonical filesystem anchors for the runtime.

Two distinct roots, deliberately separated so state can move between
machines without the code coming with it:

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
# docstring — same reasoning as `home_dir()`/`workspace_dir()`/
# `user_agents_dir()`).
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


def system_agents_dir() -> Path:
    """Shipped agent cards, read from the sealed app tree and never copied.

    An update replaces `app/`, so a card improved here reaches every install
    on the next update. Copying it into `home/` once — which is what the
    runtime used to do — froze every shipped card at whatever the operator
    installed, forever.

    Anchored on `TESSERACT_DIR` rather than `app_dir()`: this package IS the
    shipped tree, so the anchor holds in a dev checkout, in a packaged
    install, and in a test that points `TESSERACT_HOME` somewhere else.
    """
    return TESSERACT_DIR / "agents"


def user_agents_dir() -> Path:
    """Agent cards the assistant built for the operator — state, not code.

    In a dev checkout `home_dir()` IS `TESSERACT_DIR`, so this and
    `system_agents_dir()` are one directory. Callers that merge the two must
    compare resolved paths rather than assume they are distinct.
    """
    return _home_at_call_time() / "agents"


def config_dir() -> Path:
    """Config tree — call-time so a `TESSERACT_HOME` change is honored
    without a fresh import, unlike the frozen `CONFIG_DIR` constant above."""
    return _home_at_call_time() / "config"


def system_config_dir() -> Path:
    """The shipped config tree, in the sealed app tree.

    Most of `config/` is seeded into `home/` and merged key-by-key from here,
    which works. `schedule.yaml::jobs` is the exception the merge cannot
    reach — it is a LIST, and `migrate_config_keys` copies a list whole or not
    at all, so a job added in a release lands on no existing install. The
    scheduler therefore reads its system rows from here directly, the same way
    agent cards are read from `system_agents_dir()`.
    """
    return TESSERACT_DIR / "config"


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
        # Which tools actually get called, and in how many sessions. The
        # operator's own working habits rather than a machine's — the answer
        # decides which schemas ride every turn, and it should be the same
        # answer on the second PC.
        "usage",
    }
)
_RUNTIME_LOG_DIRS = frozenset(
    {
        "audit", "circuit-breakers", "supervisor", "janitor", "provider-health",
        "tokenjuice", "governor",
        # One file per boot, named for its boot id. Machine ops: which run of
        # this process on this machine said what. Never synced — a per-launch
        # file travelling to the other PC is noise, not history.
        "backend",
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
# carries a DENY rule over every one of the operator's own documents. Matching
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
#      why `workspace` is absent: every document in it — `SOUL.md`,
#      `USER.md`, `OPERATING.md`, `WORKSHOP.md`, `DIARY.md` — is DENY for
#      write, and a read allowlist entry would reach all five.
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
    # The runtime's own account of itself: the watchman's hourly reports, the
    # evidence files behind them, and `WHAT-RUNS.md`. Added when the tracker
    # was built (AR-7b item 11), for a reason the other entries share — the
    # runtime writes a path and then tells the assistant to read it, and a
    # pointer to a file the read tools cannot open is worse than no pointer.
    # Nothing under it is a secret or an operator document; it is derived
    # output, regenerated every pass, and the write side is at the default
    # posture rather than DENY.
    "autonomy",
)


# Filenames the read tools refuse outright, wherever on disk they sit.
#
# Containment cannot cover this and it is worth being exact about why:
# `tesseract/.env` lives INSIDE the code tree, so it is reachable with zero
# traversal by a path the runtime itself prints in error messages. Bounding
# reads to `workspace_root` leaves it fully readable. `permissions.yaml`
# carries `path_overrides` for `file_write` only, so no policy layer scopes a
# read underneath either — which leaves exactly one place for the refusal to
# live, and this is it.
#
# Templates stay readable on purpose: `.env.example` carries key NAMES and no
# values, and the first-run setup form is built by parsing it.
_SECRET_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".mcp.json",
        "trusted_dirs.json",
        "github_token",
        "secrets.yaml",
        "secrets.yml",
        # SSH private keys. The `.pub` halves are deliberately absent — a
        # public key is public, and refusing it would be theatre.
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        # Credential stores the wider toolchain writes into a home directory,
        # every one of which sits in the same tree the read tools can reach.
        # Named individually rather than by pattern: each is a real file with
        # a known name, and a pattern broad enough to catch them all would
        # also catch ordinary configuration.
        ".netrc",
        "_netrc",  # the Windows spelling
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".htpasswd",
        "credentials",  # `~/.aws/credentials`, `~/.config/gcloud/credentials`
        "credentials.json",
    }
)
_SECRET_SUFFIXES: tuple[str, ...] = (".pem", ".pfx", ".p12", ".keystore")
_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".template", ".sample", ".dist")


def _effective_name(name: str) -> str:
    """The name Windows will actually open, given `name`.

    Two normalisations, both measured against the real filesystem rather than
    assumed, because each one opens `.env` while spelling it differently:

    - everything from the first `:` is a stream specifier, so `.env::$DATA`
      reads the DEFAULT stream — the file's own bytes;
    - a trailing dot or space is stripped by the filesystem, so `.env ` and
      `.env.` are both `.env`.

    Matching the raw string refuses the documented spelling and admits three
    that reach the same bytes, which is worse than no check at all: it reads
    as a control while behaving as a gap.
    """
    return name.split(":", 1)[0].rstrip(". ")


def is_secret_filename(name: str) -> bool:
    """Whether `name` is a credential-bearing file a read tool must refuse.

    Case-insensitive for the same reason `readable_state_prefix` is: the
    target filesystem is NTFS, where `.ENV` and `.env` are one file, so an
    exact-case test would refuse the documented spelling and pass the other.
    """
    folded = _effective_name(name).casefold()
    if folded.endswith(_TEMPLATE_SUFFIXES):
        return False
    if folded in _SECRET_FILENAMES:
        return True
    if folded.startswith(".env."):
        return True
    return folded.endswith(_SECRET_SUFFIXES)


def secret_exclusion_globs() -> tuple[str, ...]:
    """`is_secret_filename` as glob patterns, for tools that hand the walk to
    an external searcher and so cannot filter file by file.

    `.env.*` sweeps up `.env.example` too, which `is_secret_filename` allows.
    Accepted rather than worked around: a template holds key names and no
    values, so excluding it from a content search costs a caller nothing,
    while a re-include rule would be one more thing to keep in step.
    """
    return (
        *sorted(_SECRET_FILENAMES),
        ".env.*",
        *(f"*{suffix}" for suffix in _SECRET_SUFFIXES),
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
