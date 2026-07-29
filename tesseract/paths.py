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
    """TARS's writable workspace (SOUL/DIARY/...). Call-time so updates
    replacing the code tree never touch it."""
    return _home_at_call_time() / "workspace"


def agents_dir() -> Path:
    """Agent cards — operator-created agents are state, not code."""
    return _home_at_call_time() / "agents"


def config_dir() -> Path:
    """Config tree — call-time so a `TESSERACT_HOME` change is honored
    without a fresh import, unlike the frozen `CONFIG_DIR` constant above."""
    return _home_at_call_time() / "config"


def is_installed_tree() -> bool:
    """True iff this process is running from a packaged install's code
    checkout, never a dev checkout.

    Packaged layout (`mirror/src-tauri/src/provision.rs::tesseract_home` +
    `clone_app_dir`): the shell always points `TESSERACT_HOME` at the
    per-user state root and clones the production repo into
    ``<TESSERACT_HOME>/app`` — so this package's ``ROOT``
    (``TESSERACT_DIR.parent``) equals ``home_dir() / "app"``. In a dev
    checkout, ``TESSERACT_HOME`` is either unset (``home_dir() ==
    TESSERACT_DIR``, whose ``/"app"`` is a subdirectory *inside* the repo,
    never equal to the repo's own parent) or an operator-chosen override —
    neither shape coincides with the packaged equality by accident.

    Path equality alone still isn't proof: an operator could point
    ``TESSERACT_HOME`` one level above their own checkout without meaning
    to. The provisioning marker (``<home>/runtime/provisioned.json``,
    written once by ``provision.rs::write_marker`` at the end of a REAL
    first-run install, never produced by anything a dev checkout runs) must
    also be present. A false "installed" verdict is the worst outcome here
    — it would refuse the operator's own dev-checkout source edits — so
    this predicate ANDs both signals rather than trusting either alone.
    """
    try:
        root = ROOT.resolve()
        candidate = (home_dir() / "app").resolve()
    except OSError:
        return False
    if os.path.normcase(str(root)) != os.path.normcase(str(candidate)):
        return False
    return (home_dir() / "runtime" / "provisioned.json").is_file()
