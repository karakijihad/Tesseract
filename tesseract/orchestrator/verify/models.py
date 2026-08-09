"""Result shapes for the tier-0 verification gate.

Four outcomes, not two. A command that was never configured, a command the
permission layer refused, and a command that ran and failed are three
different facts, and collapsing any of them into "not passing" loses the one
distinction the relay needs: whether a model turn can be skipped. A refusal in
particular must never read as a pass — the gate exists so the compiler's answer
is trusted, and a step that never executed has no answer to trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# The verify commands the gate knows how to run, in the order it runs them.
# Cheapest-and-most-specific first: a type error makes a test failure's stack
# trace noise, so surfacing it first keeps the eventual auditor brief small.
STEP_ORDER: tuple[str, ...] = ("typecheck", "lint", "test")


class StepOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"
    # Refused before execution — permission policy, a security check, or an
    # executor that could not start the command. Never a pass.
    BLOCKED = "blocked"


@dataclass(frozen=True)
class StepResult:
    name: str
    command: str | None
    outcome: StepOutcome
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    # Human-readable why, for NOT_CONFIGURED and BLOCKED. Empty otherwise.
    skipped_reason: str = ""

    @property
    def ran(self) -> bool:
        return self.outcome in (StepOutcome.PASSED, StepOutcome.FAILED)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    steps: list[StepResult] = field(default_factory=list)

    @property
    def failed_steps(self) -> list[StepResult]:
        return [s for s in self.steps if s.outcome is StepOutcome.FAILED]

    @property
    def blocked_steps(self) -> list[StepResult]:
        return [s for s in self.steps if s.outcome is StepOutcome.BLOCKED]

    @property
    def passed_steps(self) -> list[StepResult]:
        return [s for s in self.steps if s.outcome is StepOutcome.PASSED]

    @property
    def vacuous(self) -> bool:
        """``ok`` with nothing actually executed.

        A project with no verify commands configured, or one whose every command
        was refused, produces ``ok`` by vacuous truth. A caller that treats this
        as "verified" is claiming evidence it does not have, so the fact is
        surfaced rather than folded into ``ok`` — the relay decides whether an
        unverifiable project still skips the auditor, and it should have to say
        so out loud.
        """
        return not self.passed_steps


__all__ = [
    "STEP_ORDER",
    "GateResult",
    "StepOutcome",
    "StepResult",
]
