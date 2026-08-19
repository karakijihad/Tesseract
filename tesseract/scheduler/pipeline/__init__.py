"""Ordered background work: one declaration, one order, one manifest.

`stage.py` is the declaration, `graph.py` checks it, `runner.py` executes it,
`manifest.py` records it, `artifacts.py` remembers where it got to, and
`checks.py` holds the boot checks. `stages/` declares the two live rows —
`capture` every five minutes and `consolidate` at the anchor — and
`scheduler/tasks/{capture,consolidate}_pipeline.py` are the schedule entries
that run them.
"""

from tesseract.scheduler.pipeline.artifacts import (
    ArtifactStore,
    ArtifactVersion,
    WatermarkStore,
    pipeline_root,
)
from tesseract.scheduler.pipeline.checks import CheckFinding, run_config_checks
from tesseract.scheduler.pipeline.graph import (
    PipelineDeclarationError,
    execution_order,
)
from tesseract.scheduler.pipeline.job_stage import job_stage
from tesseract.scheduler.pipeline.manifest import (
    ManifestStore,
    MemoryManifestStore,
    RunManifest,
    StageRow,
)
from tesseract.scheduler.pipeline.registry import Row, register_row, rows, validate_rows
from tesseract.scheduler.pipeline.row_job import run_row
from tesseract.scheduler.pipeline.runner import PipelineRunner
from tesseract.scheduler.pipeline.stage import (
    ProviderUnreachable,
    Stage,
    StageCadence,
    StageContext,
    StageKind,
    StageReport,
)

__all__ = [
    "ArtifactStore",
    "ArtifactVersion",
    "CheckFinding",
    "ManifestStore",
    "MemoryManifestStore",
    "PipelineDeclarationError",
    "PipelineRunner",
    "ProviderUnreachable",
    "Row",
    "RunManifest",
    "Stage",
    "StageCadence",
    "StageContext",
    "StageKind",
    "StageReport",
    "StageRow",
    "WatermarkStore",
    "execution_order",
    "job_stage",
    "pipeline_root",
    "register_row",
    "rows",
    "run_config_checks",
    "run_row",
    "validate_rows",
]
