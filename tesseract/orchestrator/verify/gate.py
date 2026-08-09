"""Tier-0 verification gate — run a project's `verify` commands, structurally.

The gate answers "does this tree still build, typecheck and pass its tests"
without spending a model turn to find out. It never shells out itself: the
injected executor owns that, and the production executor
(`policy_executor.py`) routes every command through `permissions/decide.py`,
so a verify command is subject to exactly the same security checks, allowlist
and operator ASK as any other bash call.

Steps run serially, and deliberately so. They share one working directory, and
each one may surface an operator approval prompt — three concurrent modals for
one gate run is worse than the seconds saved. Every configured step runs even
after an earlier one fails: with the relay capped at two refinement rounds, a
coder that learns about the type error and the test failure in the same round
is the difference between converging and burning the cap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from tesseract.config.cockpit import VerifyConfig, load_verify_config
from tesseract.kernel.tokenjuice.reducers import cap_chars, head_tail
from tesseract.orchestrator.projects.models import VerifyCommands

from .models import STEP_ORDER, GateResult, StepOutcome, StepResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecOutcome:
    """One command's result as reported by an executor.

    A non-empty ``blocked_reason`` means the command never ran — refused by
    policy, by a security check, or by the executor failing to start it. It is
    the executor's job to say so explicitly rather than returning a synthetic
    non-zero exit code, because "refused" and "failed" drive different
    decisions and a fabricated exit code erases the difference.
    """

    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    blocked_reason: str = ""


class StepExecutor(Protocol):
    """Runs one verify command and reports its outcome.

    Injected so tests never spawn a real subprocess, and so the policy path is
    a swappable collaborator rather than an import the gate cannot avoid.
    """

    async def __call__(
        self, command: str, *, cwd: str, timeout_s: float
    ) -> ExecOutcome: ...


def _bound(text: str, config: VerifyConfig) -> str:
    """Head+tail elision, then a hard character ceiling.

    ``head_tail`` counts lines, so it is blind to a single multi-megabyte line
    — a minified stack trace or a binary blob echoed into stdout stays whole
    and reaches the model intact. ``cap_chars`` is the backstop that makes the
    bound hold regardless of how the output is shaped.
    """
    if not text:
        return ""
    reduced = head_tail(
        text,
        head=config.output_head_lines,
        tail=config.output_tail_lines,
    )
    return cap_chars(reduced, n=config.output_max_chars)


async def _run_step(
    name: str,
    command: str | None,
    *,
    cwd: str,
    executor: StepExecutor,
    config: VerifyConfig,
) -> StepResult:
    if command is None or not command.strip():
        return StepResult(
            name=name,
            command=None,
            outcome=StepOutcome.NOT_CONFIGURED,
            skipped_reason="not configured",
        )

    try:
        outcome = await executor(command, cwd=cwd, timeout_s=config.step_timeout_s)
    except Exception as exc:  # noqa: BLE001 — an executor fault is a blocked step, not a pass
        logger.warning("verify step %s raised in the executor: %s", name, exc)
        return StepResult(
            name=name,
            command=command,
            outcome=StepOutcome.BLOCKED,
            skipped_reason=f"executor error: {exc}",
        )

    if outcome.blocked_reason:
        return StepResult(
            name=name,
            command=command,
            outcome=StepOutcome.BLOCKED,
            skipped_reason=outcome.blocked_reason,
            stdout_tail=_bound(outcome.stdout, config),
            stderr_tail=_bound(outcome.stderr, config),
        )

    # An executor that reports neither a refusal nor an exit code has told us
    # nothing. Treating that as success is the exact failure this gate exists
    # to prevent, so it is blocked.
    if outcome.exit_code is None:
        return StepResult(
            name=name,
            command=command,
            outcome=StepOutcome.BLOCKED,
            skipped_reason="executor returned no exit code and no refusal reason",
            stdout_tail=_bound(outcome.stdout, config),
            stderr_tail=_bound(outcome.stderr, config),
        )

    return StepResult(
        name=name,
        command=command,
        outcome=StepOutcome.PASSED if outcome.exit_code == 0 else StepOutcome.FAILED,
        exit_code=outcome.exit_code,
        stdout_tail=_bound(outcome.stdout, config),
        stderr_tail=_bound(outcome.stderr, config),
    )


async def run_gate(
    commands: VerifyCommands,
    *,
    cwd: str,
    executor: StepExecutor,
    config: VerifyConfig | None = None,
) -> GateResult:
    """Run every configured verify command and fold the results into a verdict.

    ``ok`` is True only when nothing failed and nothing was blocked. An
    all-unconfigured project therefore returns ``ok=True`` — check
    ``GateResult.vacuous`` before reading that as evidence of anything.
    """
    cfg = config or load_verify_config()
    steps = [
        await _run_step(
            name,
            getattr(commands, name),
            cwd=cwd,
            executor=executor,
            config=cfg,
        )
        for name in STEP_ORDER
    ]
    ok = not any(
        s.outcome in (StepOutcome.FAILED, StepOutcome.BLOCKED) for s in steps
    )
    return GateResult(ok=ok, steps=steps)


__all__ = ["ExecOutcome", "StepExecutor", "run_gate"]
