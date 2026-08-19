"""Rows that fire on an event instead of an hour.

`conditions.py` names the events; `evaluate.py` asks them and remembers the
answer. A row opts in by declaring `when:` where it would have written
`cadence:` — nothing else about being a row changes.
"""

from tesseract.scheduler.triggers.conditions import (
    CONDITIONS,
    Condition,
    ConditionError,
    TriggerContext,
    Verdict,
    check_row,
    condition,
)
from tesseract.scheduler.triggers.evaluate import evaluate, record_fired, watermark_key

__all__ = [
    "CONDITIONS",
    "Condition",
    "ConditionError",
    "TriggerContext",
    "Verdict",
    "check_row",
    "condition",
    "evaluate",
    "record_fired",
    "watermark_key",
]
