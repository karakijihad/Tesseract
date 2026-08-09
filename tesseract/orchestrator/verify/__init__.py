"""Tier-0 verification: run the compiler before paying for a reviewer.

Two halves that meet in the relay. `gate` executes a project's registered
`verify` commands through the normal bash policy path and reports a structured
result; `verdict` defines the schema-enforced shape an auditor must answer in,
so "did the reviewer find anything new" is a set comparison instead of prose to
re-read every round.
"""

from .gate import ExecOutcome, StepExecutor, run_gate
from .models import STEP_ORDER, GateResult, StepOutcome, StepResult
from .render import render_gate, render_gate_for_model
from .verdict import (
    Finding,
    Verdict,
    VerdictError,
    parse_verdict,
    verdict_schema,
    verdict_schema_path,
)

__all__ = [
    "STEP_ORDER",
    "ExecOutcome",
    "Finding",
    "GateResult",
    "StepExecutor",
    "StepOutcome",
    "StepResult",
    "Verdict",
    "VerdictError",
    "parse_verdict",
    "render_gate",
    "render_gate_for_model",
    "run_gate",
    "verdict_schema",
    "verdict_schema_path",
]
