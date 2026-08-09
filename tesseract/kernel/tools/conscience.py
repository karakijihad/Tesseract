"""conscience_status tool — the assistant's self-check against the drift log.

Read-only pull. Returns the latest `drift-*.jsonl` report as compact text
that the assistant can paraphrase ("my conscience shows 2 ok, 1 warn, 0 bad as of
3h ago"). AUTO permission — no operator prompt. When the heartbeat
hasn't fired yet (disabled cron or fresh install), returns a helpful
empty-state string instead of an error so the assistant can say so honestly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from tesseract.conscience.reader import load_latest_report
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.paths import home_dir, log_dir


def _drift_dir() -> Path:
    """Call-time resolution under `TESSERACT_HOME` — matches the writer
    (`ConscienceHeartbeatJob`, home-anchored). `tesseract.paths` is a leaf
    module (no circular-import risk), unlike `tesseract.brain.boot`, which
    imports every tool module."""
    return log_dir("conscience")


class ConscienceStatusInput(BaseModel):
    verbose: bool = Field(
        default=False,
        description="Include per-signal detail lines. Default is a summary-only view.",
    )


class ConscienceStatusTool(Tool):
    default_posture = "auto"

    risk_class: ClassVar[str] = "autonomous"
    @property
    def name(self) -> str:
        return "conscience_status"

    @property
    def description(self) -> str:
        return (
            "Check your own drift / conscience state. Returns the most recent "
            "rule-based report from the scheduled conscience_heartbeat job: signal "
            "statuses (ok/warn/bad), worst-status summary, and how long ago it was "
            "scraped. Use when asked how you're holding up, when something feels off, "
            "or before a long-running task. Pass verbose=true for per-signal detail."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return ConscienceStatusInput

    def is_concurrency_safe(self) -> bool:
        return True

    def is_read_only(self) -> bool:
        return True

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: ConscienceStatusInput = tool_input  # type: ignore[assignment]
        report = load_latest_report(_drift_dir())
        if report is None:
            return ToolResult(
                output=(
                    "Conscience status: no report yet. The conscience_heartbeat job "
                    "hasn't fired — it ships disabled by default; toggle it on in the "
                    "Mirror Schedule tab, or ask the operator to run it once."
                ),
                metadata={"report_available": False},
            )
        return ToolResult(
            output=_format(report, verbose=inp.verbose),
            metadata={
                "report_available": True,
                "summary": report.get("summary") or {},
                "timestamp": report.get("timestamp"),
            },
        )


def _format(report: dict[str, Any], *, verbose: bool) -> str:
    ts = report.get("timestamp") or ""
    age = _format_age(ts)
    window = report.get("window_hours")
    signals = report.get("signals") or []
    summary = report.get("summary") or {}

    ok = int(summary.get("ok", 0))
    warn = int(summary.get("warn", 0))
    bad = int(summary.get("bad", 0))
    worst = "bad" if bad else ("warn" if warn else "ok")

    header = (
        f"Conscience status — worst: {worst}. "
        f"Summary: {ok} ok · {warn} warn · {bad} bad "
        f"(scraped {age}, {window}h window, {len(signals)} signals)."
    )
    if not verbose:
        # Still mention any non-ok signals by name so the one-line
        # answer isn't misleading.
        flagged = [s["name"] for s in signals if s.get("status") in ("warn", "bad")]
        if flagged:
            header += f" Flagged: {', '.join(flagged)}."
        return header

    lines = [header, ""]
    for s in signals:
        lines.append(
            f"- {s.get('name')}: {s.get('status')} "
            f"(value={s.get('value')}, warn≥{s.get('warn')}, bad≥{s.get('bad')})"
            + (f" — {s['detail']}" if s.get("detail") else "")
        )
    return "\n".join(lines)


def _format_age(iso: str) -> str:
    if not iso:
        return "unknown time"
    try:
        t = datetime.fromisoformat(iso).astimezone(timezone.utc)
    except ValueError:
        return "unknown time"
    delta = (datetime.now(timezone.utc) - t).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
