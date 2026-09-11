"""``recovery_summary`` workspace event builder.

Single envelope per boot. Shape locked in
`_shared/recovery-state-machine.md §Recovery summary envelope`.
The Mirror dashboard's Recovery pane consumes this directly;
the Telegram nudge renders a one-line digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tesseract.workspace_events.events import WorkspaceEvent


@dataclass(frozen=True)
class AttentionItem:
    """One entry in ``operator_attention`` — drawn from any scan that
    decided the operator should look. Kept tiny so the envelope stays
    scannable; deep detail lives in the underlying records."""

    kind: str  # "agenda" | "worker"
    id: str
    reason: str


@dataclass
class RecoverySummary:
    """Mutable accumulator; ``to_event`` snapshots into an immutable
    workspace event. Per-scan counters use `int` so each scan can call
    ``inc_*`` without worrying about default-mutable pitfalls."""

    boot_id: str
    started_at: datetime
    downtime_seconds: float = 0.0
    scans: dict[str, dict[str, int]] = field(default_factory=dict)
    operator_attention: list[AttentionItem] = field(default_factory=list)

    def section(self, name: str) -> dict[str, int]:
        block = self.scans.get(name)
        if block is None:
            block = {}
            self.scans[name] = block
        return block

    def inc(self, scan: str, bucket: str, by: int = 1) -> None:
        block = self.section(scan)
        block[bucket] = block.get(bucket, 0) + by

    def flag(self, *, kind: str, id: str, reason: str) -> None:
        self.operator_attention.append(AttentionItem(kind=kind, id=id, reason=reason))

    def to_payload(self) -> dict[str, Any]:
        return {
            "boot_id": self.boot_id,
            "downtime_seconds": round(self.downtime_seconds, 3),
            "scans": {k: dict(v) for k, v in self.scans.items()},
            "operator_attention": [
                {"kind": a.kind, "id": a.id, "reason": a.reason}
                for a in self.operator_attention
            ],
        }


def empty_scan_counts() -> dict[str, dict[str, int]]:
    """Canonical empty-shape for a no-state boot — keeps the dashboard
    rendering even when every scan has nothing to do.

    ``schedule.completed`` / ``schedule.failed`` count what landed in
    runs.jsonl during the lookback window — these are the rows that
    DID complete (engine writes them post-completion). Crash-interrupted
    firings leave no row at all; a started-row log marker would give
    the bucket true ``interrupted`` semantics.
    """
    return {
        "workers": {"preserved": 0, "interrupted": 0, "failed": 0},
        "turns": {"interrupted": 0, "preserved": 0, "unreadable": 0},
        "schedule": {"completed": 0, "failed": 0},
        "agenda": {"resume_queued": 0, "blocked": 0, "preserved": 0},
        # What was in the air rather than what was on disk: a call the last
        # process made and never recorded the end of. `resumable` is safe
        # to run again by its own declaration, `check_first` has to be
        # asked about before anything is repeated, and `asked_you` is one
        # question waiting in the inbox.
        "effects": {"resumable": 0, "check_first": 0, "asked_you": 0},
    }


#: One card, for the life of the machine. It used to be `recovery-<boot_id>`,
#: which is a new card on every boot: 266 of them in 27 days, cleared by
#: deleting them in bulk, and a pile that size is one nobody reads. The store
#: keys by `event_id` and the newest row wins, so a fixed id makes each boot
#: REWRITE the card rather than add one. Which boot wrote it is in the title
#: and in the payload, where it always was.
EVENT_ID = "recovery-summary"


def build_recovery_event(summary: RecoverySummary) -> WorkspaceEvent:
    """Snapshot the summary into the `recovery_summary` workspace event.

    `priority=8` because operator-attention items should bubble above
    normal feedback churn but below `nudge`-priority hot asks.

    **It resolves itself.** A boot that found nothing needing the operator
    writes the card as settled, so a card that was pending after a bad boot
    clears on the next good one without anybody pressing anything, and a later
    bad boot re-opens the same card. What that leaves in the inbox is one row
    that is either asking or not, which is what a person can act on; what it
    replaces is a stack of identical cards where only the top one was true.

    Dedup is on the READ side: `EventStore.list_events` keys by `event_id` so
    only the newest row for one id surfaces. `append_event` is a blind append
    and is not a write-side dedup.
    """
    summary_text = _render_summary_text(summary)
    now = datetime.now(timezone.utc).isoformat()
    needs_you = bool(summary.operator_attention)
    return WorkspaceEvent(
        event_id=EVENT_ID,
        ts=now,
        kind="recovery_summary",
        source="recovery",
        title=f"Recovery after boot {summary.boot_id}",
        summary=summary_text,
        payload=summary.to_payload(),
        status="pending" if needs_you else "resolved",
        decided_at=None if needs_you else now,
        decided_reason=(
            "" if needs_you else "this boot left nothing for you to answer"
        ),
        priority=8,
        author_id="system",
        author_display="Recovery",
    )


def _render_summary_text(summary: RecoverySummary) -> str:
    """One-line plain-text summary that fits in the inbox preview and
    the Telegram nudge."""
    parts: list[str] = []
    # First, because it is the only line here about a person who was left
    # waiting. Everything else on this envelope is about the machine.
    cut_short = (summary.scans.get("turns") or {}).get("interrupted", 0)
    if cut_short:
        parts.append(
            f"{cut_short} conversation{'s' if cut_short != 1 else ''} stopped "
            f"mid answer and got no reply"
        )
    # Second, because it is the other line about something outside this
    # machine: a call that may have reached somebody and may not have.
    effects = summary.scans.get("effects") or {}
    in_the_air = effects.get("check_first", 0) + effects.get("asked_you", 0)
    if in_the_air:
        parts.append(
            f"{in_the_air} action{'s' if in_the_air != 1 else ''} may or may "
            f"not have finished"
        )
    sched = summary.scans.get("schedule") or {}
    failed_runs = sched.get("failed", 0)
    if failed_runs:
        parts.append(f"{failed_runs} schedule runs failed")
    attn = len(summary.operator_attention)
    if attn:
        parts.append(f"{attn} item{'s' if attn != 1 else ''} need operator")
    if not parts:
        return "clean boot — no in-flight state to recover"
    return "; ".join(parts) + "."


__all__ = [
    "AttentionItem",
    "RecoverySummary",
    "EVENT_ID",
    "build_recovery_event",
    "empty_scan_counts",
]
