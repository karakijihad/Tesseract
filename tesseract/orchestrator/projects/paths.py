"""Call-time resolution of the project-registry location.

Both helpers read ``TESSERACT_HOME`` on every call rather than binding it at
import. A module-level constant would freeze whichever home was live when the
first importer touched this file, which is exactly the shape that leaks test
writes into the operator's tree.
"""

from __future__ import annotations

import os
from pathlib import Path

_REGISTRY_FILE_NAME = "registry.json"


def projects_dir() -> Path:
    """``<TESSERACT_HOME>/projects``, resolved at call time."""
    from tesseract.paths import TESSERACT_HOME

    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else Path(TESSERACT_HOME)
    return home / "projects"


def registry_path() -> Path:
    """``<TESSERACT_HOME>/projects/registry.json``, resolved at call time."""
    return projects_dir() / _REGISTRY_FILE_NAME


__all__ = ["projects_dir", "registry_path"]
