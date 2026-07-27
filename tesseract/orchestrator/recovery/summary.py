"""``recovery_summary`` workspace event builder.

Single envelope per boot. Shape locked in
`_shared/recovery-state-machine.md §Recovery summary envelope`.
The Mirror dashboard's Recovery pane (AU-7) consumes this directly;
the Telegram nudge (AU-2 S2) renders a one-line digest.
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
    firings leave no row at all in S1; AU-2 S2 will add a started-row
    log marker so the bucket gains true ``interrupted`` semantics.
    """
    return {
        "workers": {"preserved": 0, "interrupted": 0, "failed": 0},
        "schedule": {"completed": 0, "failed": 0},
        "agenda": {"resume_queued": 0, "blocked": 0, "preserved": 0},
    }


def build_recovery_event(summary: RecoverySummary) -> WorkspaceEvent:
    """Snapshot the summary into a `recovery_summary` workspace event.

    `priority=8` because operator-attention items should bubble above
    normal feedback churn but below `nudge`-priority hot asks. Stable
    `event_id` shape (``recovery-<boot_id>``); the underlying boot_id
    carries a uuid suffix so collisions across boots are vanishingly
    rare. Dedup is handled on the READ side — `EventStore.list_events`
    keys by `event_id` so duplicate raw rows surface only once in the
    inbox. `append_event` itself is a blind append; do not rely on it
    for write-side dedup.
    """
    summary_text = _render_summary_text(summary)
    return WorkspaceEvent(
        event_id=f"recovery-{summary.boot_id}",
        ts=datetime.now(timezone.utc).isoformat(),
        kind="recovery_summary",
        source="recovery",
        title=f"Recovery — boot {summary.boot_id}",
        summary=summary_text,
        payload=summary.to_payload(),
        priority=8,
        author_id="system",
        author_display="Recovery",
    )


def _render_summary_text(summary: RecoverySummary) -> str:
    """One-line plain-text summary that fits in the inbox preview and
    the AU-2 S2 Telegram nudge."""
    parts: list[str] = []
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
    "build_recovery_event",
    "empty_scan_counts",
]
