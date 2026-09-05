"""What the last sweep did, left where a surface can read it.

The sweep already counts everything per tree and hands it back as the stage's
payload. A `StageReport` carries an outcome, a reason and two totals, so the
per-tree half of that payload has nowhere to go: it dies between the job and
`runs.jsonl`, and the only way to know what the machine deleted last night is
to notice something missing.

So the job writes it down. One file, overwritten every sweep, in the shape the
watchman's `latest.json` already established: a producer leaves its own record
behind and the room reads the file rather than asking the machine again.

**Writing it can never fail the sweep.** A record of what was deleted is worth
having and it is not worth refusing to delete for, so every failure here is a
warning and nothing else.

**And reading it answers three things, not two.** Absent means nothing has
swept on this machine; unreadable means something did and this cannot see what;
present means the counts. A reader that collapses the first two reports a read
failure as a producer that does not exist, which is the confusion the Autonomy
panel exists to end.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def record_path() -> Path:
    """Resolved at call time — an import-time constant freezes the path before
    a relocated home is known, and pins a test's writes to the operator's own
    tree."""
    from tesseract.paths import home_dir

    return home_dir() / "autonomy" / "retention.json"


def write(trees: dict[str, dict[str, int | str]], errors: list[str]) -> None:
    """Leave the sweep's own account of itself behind. Best effort, always."""
    path = record_path()
    payload = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "trees": trees,
        "errors": list(errors),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside and renamed over, so a reader polling this file never
        # opens a half-written one. The room reads it on a timer.
        scratch = path.with_suffix(".json.writing")
        scratch.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(scratch, path)
    except OSError as exc:
        log.warning("retention: the sweep record could not be written: %s", exc)


#: The read. `None` is "nothing has swept here"; a `str` is "something did and
#: this could not read it"; a `dict` is the record.
Record = dict[str, Any] | str | None


def read() -> Record:
    path = record_path()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"the last sweep's record could not be read: {exc.strerror or exc}"
    except ValueError:
        return "the last sweep left a record that is not readable JSON"
    if not isinstance(loaded, dict):
        return "the last sweep left a record that is not the shape a record has"
    return loaded


__all__ = ["Record", "read", "record_path", "write"]
