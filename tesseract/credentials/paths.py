"""Where the assistant's own account credentials live.

``<TESSERACT_HOME>/credentials/credentials.json``. Both components of that
path are already in ``paths.py::_SECRET_FILENAMES``, and
``refuse_if_secret_path`` walks a path component by component, so every read
tool refuses the directory as well as the file. That is not an accident of
naming: it is why these two names were chosen over anything else.

Resolved at call time, never bound at import, for the same reason the project
registry does it — a module-level constant freezes whichever home was live
when the first importer touched this file, and that is the shape that leaks
test writes into the operator's tree.
"""

from __future__ import annotations

from pathlib import Path

_STORE_FILE_NAME = "credentials.json"


def credentials_dir() -> Path:
    """``<TESSERACT_HOME>/credentials``, resolved at call time."""
    from tesseract.paths import home_dir

    return home_dir() / "credentials"


def store_path() -> Path:
    """``<TESSERACT_HOME>/credentials/credentials.json``, resolved at call time."""
    return credentials_dir() / _STORE_FILE_NAME


__all__ = ["credentials_dir", "store_path"]
