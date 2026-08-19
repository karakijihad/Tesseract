"""One manifest over everything that runs on its own.

`entry.py` is the contract, `registry.py` is the declared set, `checks.py`
refuses a set that disagrees with what ships.
"""

from tesseract.scheduler.manifest.checks import verify, verify_live
from tesseract.scheduler.manifest.entry import (
    DISPATCHED,
    Entry,
    Kind,
    ManifestError,
    Owner,
    Runs,
)
from tesseract.scheduler.manifest.registry import (
    BY_NAME,
    ENTRIES,
    entries_of,
    entry,
)

__all__ = [
    "BY_NAME",
    "DISPATCHED",
    "ENTRIES",
    "Entry",
    "Kind",
    "ManifestError",
    "Owner",
    "Runs",
    "entries_of",
    "entry",
    "verify",
    "verify_live",
]
