"""The file contract between the supervisor and the backend it watches.

The supervisor cannot inspect another Python process portably, so it asks: it
writes a request file, and the backend's watcher thread answers with a dump of
every thread's stack. That dump is unreadable on its own, being every stack
with no ranking and no way to tell which thread is the loop, so the backend
writes its own account of the loop thread at the top of it.

This module is the one place that says how. Both processes import it, which is
the point: a writer and a reader in two processes agreeing by eye is how they
stop agreeing. Stdlib only, and nothing here reaches into either runtime, so
the supervisor keeps the independence its own module docstring claims.
"""

from __future__ import annotations

import time
from pathlib import Path

# The header line the backend writes and the supervisor reads back.
LOOP_WAS_DOING = "loop_was_doing="


def read_loop_report(output_path: Path, *, wait_s: float) -> str:
    """What the backend said its loop thread was in, waiting for the answer.

    Read by the declared line rather than by position, so a header that gains
    a field does not silently start returning the wrong one.

    Every return is a sentence, including the failures, because this is a
    reason a person reads. Not answering inside the window is itself the
    finding and says so: it means the interpreter could not schedule a plain
    thread either, so nothing Python-level was holding the loop.
    """
    deadline = time.monotonic() + wait_s
    while True:
        try:
            for line in output_path.read_text(encoding="utf-8").splitlines():
                if line.startswith(LOOP_WAS_DOING):
                    return line[len(LOOP_WAS_DOING):].strip() or "it said nothing"
            # The file is here and carries no such line, so it was written by a
            # backend older than this contract. Nothing more will arrive.
            return "the backend did not say, so it predates this record"
        except OSError:
            if time.monotonic() >= deadline:
                return (
                    f"the backend did not answer within {wait_s:g}s, which means "
                    f"it could not schedule a plain thread either"
                )
            time.sleep(0.25)


__all__ = ["LOOP_WAS_DOING", "read_loop_report"]
