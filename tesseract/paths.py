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
