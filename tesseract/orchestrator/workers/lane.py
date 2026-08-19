"""Lane admission for the durable worker substrate.

Composes with the existing ``mission.WorkQueue`` rather than replacing
it. The WorkQueue handles the asyncio plumbing; ``WorkerLane`` is the
admission gate AgendaStore (AU-4) calls BEFORE submitting — it answers
"is there headroom for one more ``claude_cli`` worker right now, and
does this item's risk class permit dispatch?"

Two checks, in order:

1. **Lane cap.** Count active records of the requested kind under
   ``<TESSERACT_HOME>/workers/active/``. Reject if at or above
   ``max_concurrent``. Pulled from ``mirror.yaml::mission.lanes.worker.
   <kind>`` (int shape) or ``mission.lanes.<kind>.max_concurrent``
   (dict shape — AU-3 S2 adds the dict form alongside ``retry``).
2. **Risk class.** Reject if the requested item's risk class is more
   permissive than the worker kind's class ceiling. ``operator_gate``
   workers can run ``operator_gate`` items; an ``autonomous`` worker
   that's handed a ``propose`` item refuses with ``risk_mismatch``.

The lane never touches the filesystem directly — it asks
``list_active_records()`` for the live count. That keeps tests fast
(monkeypatched ``TESSERACT_HOME``) and the lane stateless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    TERMINAL_STATUSES,
    RiskClass,
    iter_active_status_summary,
)

log = logging.getLogger(__name__)


class AdmissionDecision(str, Enum):
    ADMIT = "admit"
    REJECT_LANE_FULL = "reject_lane_full"
    REJECT_RISK_MISMATCH = "reject_risk_mismatch"
    REJECT_UNCONFIGURED = "reject_unconfigured"


# Canonical reason strings — promoted from inline literals per the AU-3
# S1 reviewer (R4). Tests import these rather than hard-coding the
# string, mirroring the convention in
# ``orchestrator/recovery/transitions.py``'s REASON_* block.
REASON_LANE_UNCONFIGURED = "lane_unconfigured"
REASON_LANE_FULL = "lane_full"
REASON_RISK_MISMATCH_PREFIX = "risk_mismatch"


@dataclass(frozen=True)
class AdmissionResult:
    """One admission decision. ``reason`` is canonical (matches
    ``AdmissionDecision.value`` for rejects, empty for admits). The
    dashboard renders the human-readable explanation from this."""

    decision: AdmissionDecision
    kind: WorkerKind
    reason: str = ""
    running: int = 0
    cap: int = 0

    @property
    def admitted(self) -> bool:
        return self.decision == AdmissionDecision.ADMIT


# Per the risk taxonomy: each worker kind has a class CEILING — the most
# permissive class of agenda item it can execute. An ``autonomous``
# worker may NOT run a ``propose`` or ``operator_gate`` item; an
# ``operator_gate`` worker may run anything within its envelope, because
# the operator-attended ASK still fires at the tool layer.
#
# Defaults are conservative (fail-closed): CLI workers default to
# ``propose`` (their actions land via tool calls that surface their own
# ASKs) and ``markdown_agent`` / ``agent_self`` default to ``autonomous``
# (read-only research is the default profile). Operator overrides this
# at agenda dispatch time per ``risk-class-taxonomy.md §Operator
# override per agenda item``.
_KIND_CEILING: dict[WorkerKind, RiskClass] = {
    WorkerKind.AGENT_SELF: RiskClass.PROPOSE,
    WorkerKind.MARKDOWN_AGENT: RiskClass.PROPOSE,
    WorkerKind.CODER_SEAT: RiskClass.OPERATOR_GATE,
    WorkerKind.AUDITOR_SEAT: RiskClass.OPERATOR_GATE,
    WorkerKind.TERMINAL: RiskClass.OPERATOR_GATE,
}


_RISK_RANK: dict[RiskClass, int] = {
    RiskClass.AUTONOMOUS: 0,
    RiskClass.PROPOSE: 1,
    RiskClass.OPERATOR_GATE: 2,
    RiskClass.ABSOLUTE_DENY: 3,
}


def _risk_within(item: RiskClass, ceiling: RiskClass) -> bool:
    """``True`` if ``item`` is at-or-below ``ceiling``. ``absolute_deny``
    is never within any ceiling — the agenda kernel rejects those at
    admission, never sees them here, but we still guard."""
    if item == RiskClass.ABSOLUTE_DENY:
        return False
    return _RISK_RANK[item] <= _RISK_RANK[ceiling]


def _parse_cap(raw: Any) -> int | None:
    """``mirror.yaml::mission.lanes.worker.<kind>`` accepts two shapes:
    plain int (legacy) and ``{max_concurrent: N, retry: {...}}`` (AU-3
    S2 extension)."""
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, dict):
        cap = raw.get("max_concurrent")
        if isinstance(cap, int) and cap >= 0:
            return cap
    return None


class WorkerLane:
    """Admission gate. Construct with a dict of ``{WorkerKind: cap}``
    plus an optional override map; AgendaStore calls ``admit()`` with
    the prospective item's kind + risk class to get a yes/no.

    Stateless — the count of running workers per kind comes from
    ``list_active_records()`` at decision time, so cap-changes via
    config hot-reload (AU-8) take effect immediately for the next
    admission decision.
    """

    def __init__(
        self,
        caps: dict[WorkerKind, int],
        *,
        kind_ceilings: dict[WorkerKind, RiskClass] | None = None,
    ) -> None:
        self._caps = dict(caps)
        self._ceilings = dict(kind_ceilings) if kind_ceilings else dict(_KIND_CEILING)

    @classmethod
    def from_mission_lanes_block(cls, lanes_block: dict[str, Any]) -> "WorkerLane":
        """Build from the raw ``mirror.yaml::mission.lanes.worker`` dict.

        Unknown kind keys — a lane kind a later release adds — are logged and
        skipped. A missing ``max_concurrent`` for a known kind sets cap
        to 0 — that kind cannot admit until configured, which surfaces
        as a clear rejection rather than an unbounded fan-out.
        """
        caps: dict[WorkerKind, int] = {}
        for raw_key, raw_value in (lanes_block or {}).items():
            try:
                kind = WorkerKind(raw_key)
            except ValueError:
                log.warning("worker lane: unknown kind %r in mirror.yaml", raw_key)
                continue
            cap = _parse_cap(raw_value)
            if cap is None:
                log.warning(
                    "worker lane: kind %s has unreadable cap %r — treating as 0",
                    raw_key,
                    raw_value,
                )
                cap = 0
            caps[kind] = cap
        return cls(caps)

    def cap_for(self, kind: WorkerKind) -> int | None:
        return self._caps.get(kind)

    def ceiling_for(self, kind: WorkerKind) -> RiskClass:
        return self._ceilings.get(kind, RiskClass.OPERATOR_GATE)

    def running_count(self, kind: WorkerKind) -> int:
        """Count non-terminal records of ``kind`` under
        ``workers/active/``. Terminal records are normally archived
        promptly, but if the archiver is backlogged the lane gauge MUST
        NOT inflate — so we filter ``TERMINAL_STATUSES`` explicitly.

        Uses the count-only ``iter_active_status_summary`` (raw JSON
        peek for kind+status only) rather than a full Pydantic parse,
        because this is called on the admission hot path and would
        otherwise cost N model-validations per dispatch decision once
        AU-5 wires real concurrent workers."""
        target = kind.value
        terminal = {s.value for s in TERMINAL_STATUSES}
        return sum(
            1
            for _, rec_kind, rec_status in iter_active_status_summary()
            if rec_kind == target and rec_status not in terminal
        )

    def admit(self, *, kind: WorkerKind, risk_class: RiskClass) -> AdmissionResult:
        """Two-stage check. Returns the structured decision; admission
        does NOT reserve capacity — the caller writes the WorkerRecord
        on admit, which is what makes the count tick up for the next
        ``admit()`` call."""
        cap = self.cap_for(kind)
        if cap is None:
            return AdmissionResult(
                decision=AdmissionDecision.REJECT_UNCONFIGURED,
                kind=kind,
                reason=REASON_LANE_UNCONFIGURED,
                running=0,
                cap=0,
            )

        ceiling = self.ceiling_for(kind)
        if not _risk_within(risk_class, ceiling):
            return AdmissionResult(
                decision=AdmissionDecision.REJECT_RISK_MISMATCH,
                kind=kind,
                reason=(
                    f"{REASON_RISK_MISMATCH_PREFIX}:item_class={risk_class.value};"
                    f"kind_ceiling={ceiling.value}"
                ),
                running=0,
                cap=cap,
            )

        running = self.running_count(kind)
        if running >= cap:
            return AdmissionResult(
                decision=AdmissionDecision.REJECT_LANE_FULL,
                kind=kind,
                reason=REASON_LANE_FULL,
                running=running,
                cap=cap,
            )

        return AdmissionResult(
            decision=AdmissionDecision.ADMIT,
            kind=kind,
            running=running,
            cap=cap,
        )


__all__ = [
    "AdmissionDecision",
    "AdmissionResult",
    "REASON_LANE_FULL",
    "REASON_LANE_UNCONFIGURED",
    "REASON_RISK_MISMATCH_PREFIX",
    "WorkerLane",
]
