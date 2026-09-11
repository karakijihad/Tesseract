"""What the runtime does about a fault, and where it stops and asks.

Two lists, and they are two halves of one answer. `repairs.py` says what the
runtime may PUT RIGHT unasked; these say what it may SAY about a fault it
cannot put right yet, and where the healing has to stop.

- `remedies.py` — one row per finding kind: what the remedy is, whether the
  runtime may run it unasked, and what is recorded about it having run. A
  standing fault whose kind is in this table earns another mention while it
  stands, because there is something to say that a person can act on.
- `stop_rule.py` — the two conditions under which the runtime stops and asks,
  declared rather than inferred from how bad a log line looks: the remedy has
  given up and the fault is unchanged (the kernel is bugged), or the remedy is
  one the runtime may not run on its own (your advice is owed).

Neither module reads the watchman and neither writes a report. The watchman
job is the one caller, which is where observing and acting already meet.
"""

from __future__ import annotations

from tesseract.orchestrator.healing.remedies import (
    REMEDIES,
    Attempts,
    Remedy,
    attempts_for,
    remedy_for,
    sentence_for,
)
from tesseract.orchestrator.healing.stop_rule import (
    ASK_REASONS,
    KERNEL_BUGGED,
    NEEDS_ADVICE,
    SEALED_TREE,
    SPENDS,
    Stop,
    UNSAFE,
    advice_owed,
    assess,
    file_card,
)

__all__ = [
    "ASK_REASONS",
    "KERNEL_BUGGED",
    "NEEDS_ADVICE",
    "REMEDIES",
    "SEALED_TREE",
    "SPENDS",
    "UNSAFE",
    "Attempts",
    "Remedy",
    "Stop",
    "advice_owed",
    "assess",
    "attempts_for",
    "file_card",
    "remedy_for",
    "sentence_for",
]
