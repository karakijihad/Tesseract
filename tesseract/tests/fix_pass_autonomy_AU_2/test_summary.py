"""AU-2 — RecoverySummary builder + event-envelope shape lock."""

from __future__ import annotations

from datetime import datetime, timezone

from tesseract.orchestrator.recovery.summary import (
    RecoverySummary,
    build_recovery_event,
    empty_scan_counts,
)


def test_empty_scan_counts_is_full_shape() -> None:
    """Dashboard relies on the canonical scan keys being present even
    when nothing happened."""
    counts = empty_scan_counts()
    assert set(counts.keys()) == {
        "workers", "schedule", "agenda",
    }
    assert counts["workers"] == {"preserved": 0, "interrupted": 0, "failed": 0}


def test_inc_creates_section_lazily() -> None:
    s = RecoverySummary(boot_id="boot-x", started_at=datetime.now(timezone.utc))
    s.inc("custom_scan", "preserved")
    s.inc("custom_scan", "preserved")
    s.inc("custom_scan", "interrupted")
    assert s.scans["custom_scan"] == {"preserved": 2, "interrupted": 1}


def test_flag_appends_attention_item() -> None:
    s = RecoverySummary(boot_id="boot-x", started_at=datetime.now(timezone.utc))
    s.flag(kind="worker", id="wk-1", reason="worker_lost_at_restart")
    s.flag(kind="agenda", id="ag-1", reason="worker_interrupted_no_retry")
    assert len(s.operator_attention) == 2
    assert s.operator_attention[0].kind == "worker"


def test_event_envelope_shape_matches_design() -> None:
    """Lock the JSON keys against the table in
    `_shared/recovery-state-machine.md §Recovery summary envelope`."""
    s = RecoverySummary(
        boot_id="boot-20260517T220000-abc12345",
        started_at=datetime(2026, 5, 17, 22, 0, 0, tzinfo=timezone.utc),
        downtime_seconds=42.5,
        scans=empty_scan_counts(),
    )
    s.inc("workers", "interrupted")
    s.flag(kind="worker", id="wk-x", reason="worker_lost_at_restart")
    event = build_recovery_event(s)
    assert event.kind == "recovery_summary"
    assert event.source == "recovery"
    assert event.event_id == "recovery-boot-20260517T220000-abc12345"
    payload = event.payload
    assert payload["boot_id"] == s.boot_id
    assert payload["downtime_seconds"] == 42.5
    assert payload["scans"]["workers"]["interrupted"] == 1
    assert payload["operator_attention"][0]["reason"] == "worker_lost_at_restart"


def test_summary_text_clean_boot() -> None:
    s = RecoverySummary(boot_id="boot-x", started_at=datetime.now(timezone.utc))
    event = build_recovery_event(s)
    assert "clean boot" in event.summary


def test_summary_text_with_interrupts() -> None:
    s = RecoverySummary(boot_id="boot-x", started_at=datetime.now(timezone.utc))
    s.scans = empty_scan_counts()
    s.scans["schedule"]["failed"] = 3
    s.flag(kind="worker", id="wk-1", reason="worker_lost_at_restart")
    event = build_recovery_event(s)
    assert "3 schedule runs failed" in event.summary
    assert "1 item need" in event.summary
