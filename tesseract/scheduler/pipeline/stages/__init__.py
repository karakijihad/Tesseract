"""The declared rows, one module each.

Importing this package registers them, so every entry point — the schedule
handlers, `python -m tesseract.scheduler.pipeline`, the boot checks — sees the
same graph.

Registration only. `validate_rows()` is deliberately NOT called here: the
engine imports this package while resolving the two row handlers, and
`_resolve_handler` catches only ImportError/AttributeError, so a
`PipelineDeclarationError` raised at import propagated out of the engine's
constructor and left the machine with NO scheduler at all — janitor, alarms
and the autonomy jobs included, none of which the pipeline touches. The
validation now runs in `SchedulerEngine._run_boot_checks`, where a bad
declaration disables the pipeline and nothing else.
"""

from tesseract.scheduler.pipeline.stages.capture import CAPTURE_ROW
from tesseract.scheduler.pipeline.stages.consolidate import CONSOLIDATE_ROW

__all__ = ["CAPTURE_ROW", "CONSOLIDATE_ROW"]
