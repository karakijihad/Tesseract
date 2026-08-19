"""The watchman — what actually happened in the runtime, in plain words.

A runtime problem you learn about at 23:00 is a runtime problem you lived with
all day, so this is its own loop rather than a stage of the nightly pass.

Deterministic first, model second: `sources` counts what the logs, breakers and
worker records say, and `report` turns that count into an artifact. A model is
optional and bounded — it writes the opening sentence and may not add a fact to
it.
"""

from tesseract.orchestrator.watchman.findings import Finding, SourceRead, Sweep
from tesseract.orchestrator.watchman.report import (
    fact_lines,
    is_faithful,
    read_cursor,
    render_summary,
    watchman_dir,
    write_cursor,
    write_evidence,
    write_summary,
)
from tesseract.orchestrator.watchman.sources import default_window_start, sweep

__all__ = [
    "Finding",
    "SourceRead",
    "Sweep",
    "default_window_start",
    "fact_lines",
    "is_faithful",
    "read_cursor",
    "render_summary",
    "sweep",
    "watchman_dir",
    "write_cursor",
    "write_evidence",
    "write_summary",
]
