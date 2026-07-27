"""Per-kind retry policy for the durable worker substrate (AU-3 S2).

Lives separately from ``tesseract/orchestrator/mission/retry.py``
because the error models are different: the mission retry policy keys
off the ``FailureKind`` enum (TIMEOUT / DENIED / WORKER_ERROR /
VERIFICATION_FAILED) on step-runs, while the durable worker substrate
classifies by per-kind error-class strings (``transient_network``,
``pty_timeout``, ``permission_denied``, ``budget_exceeded``,
``operator_cancelled``, etc.) so the YAML stays human-readable.

Config shape (per ``_shared/worker-record-schema.md §Retry policy``):

```yaml
mission:
  lanes:
    worker:
      claude_cli:
        max_concurrent: 2
        retry:
          max_retries: 1
          backoff_seconds: 30
          retry_on_classes: [transient_network, pty_timeout]
          give_up_on_classes: [permission_denied, budget_exceeded, operator_cancelled]
```

If ``retry`` is absent the kind falls through to ``DEFAULT_RETRY``
(zero retries). If ``retry_on_classes`` is empty, no class retries.
``give_up_on_classes`` short-circuits — anything in that list refuses
to retry regardless of ``retry_count`` or ``retry_on_classes``.

The decision writes the ``retry_count`` increment to the durable
record (via the caller, not here — this module returns the decision
only). AU-5's AutonomyKernel is the caller that owns that write:
when a worker fails and ``decide()`` returns ``retry=True``, the
kernel increments ``WorkerRecord.retry_count`` and persists via
``write_record`` before requeueing. S2 ships the policy decision
function; the record write lands with the rest of the dispatch
machinery in AU-5.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from tesseract.orchestrator.workers.kinds import WorkerKind

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerRetryRule:
    """Per-kind rule. Empty defaults mean "no retries"."""

    max_retries: int = 0
    backoff_seconds: float = 0.0
    retry_on_classes: frozenset[str] = field(default_factory=frozenset)
    give_up_on_classes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class WorkerRetryDecision:
    retry: bool
    backoff_seconds: float = 0.0
    reason: str = ""


DEFAULT_RETRY = WorkerRetryRule()


def _coerce_str_set(raw: Any) -> frozenset[str]:
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(v) for v in raw if isinstance(v, str))


def _coerce_rule(block: Any) -> WorkerRetryRule:
    """Parse a ``retry:`` sub-block. Unreadable shapes log + fall through
    to ``DEFAULT_RETRY`` so a config typo doesn't deadlock the lane."""
    if not isinstance(block, dict):
        return DEFAULT_RETRY
    try:
        max_retries = int(block.get("max_retries", 0))
    except (TypeError, ValueError):
        max_retries = 0
    try:
        backoff = float(block.get("backoff_seconds", 0.0))
    except (TypeError, ValueError):
        backoff = 0.0
    return WorkerRetryRule(
        max_retries=max(0, max_retries),
        backoff_seconds=max(0.0, backoff),
        retry_on_classes=_coerce_str_set(block.get("retry_on_classes")),
        give_up_on_classes=_coerce_str_set(block.get("give_up_on_classes")),
    )


class WorkerRetryPolicy:
    """Reads per-kind retry config and decides whether a failed worker
    should be re-queued. Stateless — the caller carries ``retry_count``
    on the durable record."""

    def __init__(self, rules: dict[WorkerKind, WorkerRetryRule]) -> None:
        self._rules = dict(rules)

    @classmethod
    def from_mission_lanes_block(cls, lanes_block: dict[str, Any]) -> "WorkerRetryPolicy":
        """Build from the same ``mirror.yaml::mission.lanes.worker``
        block ``WorkerLane.from_mission_lanes_block`` reads. Accepts
        both int shape (no retry → DEFAULT) and dict shape (with
        ``retry`` sub-block)."""
        rules: dict[WorkerKind, WorkerRetryRule] = {}
        for raw_key, raw_value in (lanes_block or {}).items():
            try:
                kind = WorkerKind(raw_key)
            except ValueError:
                continue  # unknown kinds logged by WorkerLane already
            if isinstance(raw_value, dict):
                rules[kind] = _coerce_rule(raw_value.get("retry"))
            else:
                rules[kind] = DEFAULT_RETRY
        return cls(rules)

    def rule_for(self, kind: WorkerKind) -> WorkerRetryRule:
        return self._rules.get(kind, DEFAULT_RETRY)

    def decide(
        self,
        *,
        kind: WorkerKind,
        error_class: str | None,
        retry_count: int,
    ) -> WorkerRetryDecision:
        """Decision precedence:
        1. ``give_up_on_classes`` short-circuits.
        2. ``retry_count >= max_retries`` → no retry.
        3. ``error_class`` in ``retry_on_classes`` → retry with backoff.
        4. Empty ``retry_on_classes`` → no retry (must opt in).
        """
        rule = self.rule_for(kind)
        ec = error_class or ""

        if ec and ec in rule.give_up_on_classes:
            return WorkerRetryDecision(retry=False, reason=f"give_up:{ec}")
        if retry_count >= rule.max_retries:
            return WorkerRetryDecision(retry=False, reason="max_retries_reached")
        if rule.retry_on_classes and ec not in rule.retry_on_classes:
            return WorkerRetryDecision(retry=False, reason=f"class_not_eligible:{ec}")
        if not rule.retry_on_classes:
            return WorkerRetryDecision(retry=False, reason="no_retry_classes_configured")
        return WorkerRetryDecision(
            retry=True,
            backoff_seconds=rule.backoff_seconds,
            reason=f"retry:{ec}",
        )


__all__ = [
    "DEFAULT_RETRY",
    "WorkerRetryDecision",
    "WorkerRetryPolicy",
    "WorkerRetryRule",
]
