"""Render a GateResult for a model, compressed.

The rendering is deliberately asymmetric. A passing step is one line and no
output at all — nothing about a green test run helps a reviewer, and the tokens
spent restating it are pure loss. A failing or blocked step carries its tails,
because that is the whole reason the gate ran.

The compression pass names itself `verify_gate` to TokenJuice, which is what
makes `builtin/verify_gate.json` a live rule rather than a file that merely
exists — the two are asserted against each other in
`tests/lane_substrate/test_verify_gate_tokenjuice.py`, because a rule file
naming a tool nothing emits parses, loads, and compresses nothing.
"""

from __future__ import annotations

from .models import GateResult, StepOutcome, StepResult

_VERDICT_LINE = {
    StepOutcome.PASSED: "passed",
    StepOutcome.FAILED: "FAILED",
    StepOutcome.NOT_CONFIGURED: "skipped",
    StepOutcome.BLOCKED: "BLOCKED",
}


def _render_step(step: StepResult) -> list[str]:
    verdict = _VERDICT_LINE[step.outcome]
    head = f"- {step.name}: {verdict}"
    if step.outcome is StepOutcome.PASSED:
        return [f"{head} ({step.command})"]
    if step.outcome is StepOutcome.NOT_CONFIGURED:
        return [f"{head} — {step.skipped_reason}"]

    lines = [f"{head} — {step.command}"]
    if step.exit_code is not None:
        lines.append(f"  exit code: {step.exit_code}")
    if step.skipped_reason:
        lines.append(f"  reason: {step.skipped_reason}")
    if step.stdout_tail:
        lines.append("  stdout:")
        lines.extend(f"    {ln}" for ln in step.stdout_tail.splitlines())
    if step.stderr_tail:
        lines.append("  stderr:")
        lines.extend(f"    {ln}" for ln in step.stderr_tail.splitlines())
    return lines


def render_gate(result: GateResult) -> str:
    """Plain-text brief of a gate run. Not compressed — see `render_gate_for_model`."""
    if result.ok and result.vacuous:
        headline = "verification gate: NOT RUN — no verify commands are configured"
    elif result.ok:
        headline = "verification gate: PASSED"
    elif result.blocked_steps and not result.failed_steps:
        headline = "verification gate: BLOCKED — no step was allowed to run to completion"
    else:
        headline = "verification gate: FAILED"

    lines = [headline]
    for step in result.steps:
        lines.extend(_render_step(step))
    return "\n".join(lines)


def render_gate_for_model(result: GateResult) -> tuple[str, bool]:
    """`(text, was_compressed)` — the brief after the TokenJuice chain.

    Best-effort by construction: `compress_for_delivery` returns the text
    untouched if TokenJuice is disabled or its rules fail to load, so a broken
    rule degrades the token bill and never the verdict.
    """
    from tesseract.brain.tools import compress_for_delivery

    return compress_for_delivery(render_gate(result), "verify_gate")


__all__ = ["render_gate", "render_gate_for_model"]
