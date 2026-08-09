"""Shared lane-package helpers.

`TESSERACT_HOME` is resolved at call time (not import) so a test-time
``monkeypatch.setenv("TESSERACT_HOME", tmp)`` reaches every writer, and
UTC timestamps share one implementation across the lane models / store /
named-lane binding modules.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def home_root() -> Path:
    """Resolve `<TESSERACT_HOME>` lazily so tests / packaging can redirect."""
    from tesseract.paths import TESSERACT_HOME

    env = os.environ.get("TESSERACT_HOME")
    return Path(env).resolve() if env else TESSERACT_HOME


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
