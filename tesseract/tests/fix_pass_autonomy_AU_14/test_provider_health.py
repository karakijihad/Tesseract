"""AU-14 — provider_health JSONL writer / tail / rolling_window."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.orchestrator import provider_health as ph
from tesseract.scheduler.tasks._probes.base import ProbeResult


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _make(role: str = "chat_brain", *, ok: bool = True, probed_at: str | None = None) -> ProbeResult:
    return ProbeResult(
        role=role,
        ref="api.x.y",
        ok=ok,
        drift_kind="none" if ok else "http_error",
        evidence={"k": "v"},
        probed_at=probed_at or datetime.now(timezone.utc).isoformat(),
        latency_ms=12.5,
    )


def test_record_appends_row_to_jsonl(isolated_home: Path) -> None:
    path = ph.record_probe_result(_make())
    assert path.exists()
    line = path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["role"] == "chat_brain"
    assert row["ok"] is True


def test_record_drift_invokes_publisher(isolated_home: Path) -> None:
    seen: list[ProbeResult] = []

    def _pub(result: ProbeResult) -> None:
        seen.append(result)

    ph.record_probe_result(_make(ok=False), publisher=_pub)
    assert len(seen) == 1
    assert seen[0].ok is False


def test_record_ok_skips_publisher(isolated_home: Path) -> None:
    seen: list[ProbeResult] = []

    def _pub(result: ProbeResult) -> None:
        seen.append(result)

    ph.record_probe_result(_make(ok=True), publisher=_pub)
    assert seen == []


def test_record_publisher_exception_does_not_break_write(isolated_home: Path) -> None:
    def _pub(result: ProbeResult) -> None:
        raise RuntimeError("bus down")

    path = ph.record_probe_result(_make(ok=False), publisher=_pub)
    # Row was still written even though publisher exploded.
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() != ""


def test_tail_recent_returns_last_n(isolated_home: Path) -> None:
    for _ in range(5):
        ph.record_probe_result(_make())
    rows = ph.tail_recent("chat_brain", n=3)
    assert len(rows) == 3
    assert all(r["role"] == "chat_brain" for r in rows)


def test_tail_recent_missing_file_returns_empty(isolated_home: Path) -> None:
    assert ph.tail_recent("nonexistent", n=10) == []


def test_rolling_window_filters_by_age(isolated_home: Path) -> None:
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(days=2)).isoformat()
    stale = (now - timedelta(days=30)).isoformat()
    ph.record_probe_result(_make(probed_at=fresh))
    ph.record_probe_result(_make(probed_at=stale))

    rows = ph.rolling_window("chat_brain", days=7, now=now)
    assert len(rows) == 1
    assert rows[0]["probed_at"] == fresh


def test_rotation_archives_oversize_file(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Squeeze the rotation cap so we trip it cheaply.
    monkeypatch.setattr(ph, "ROTATE_AT_BYTES", 256)
    # First write — file is small, no rotation.
    ph.record_probe_result(_make())
    path = ph.provider_health_dir() / "chat_brain.jsonl"
    # Pad above the rotation cap.
    big = "x" * 1024
    path.write_text(big, encoding="utf-8")
    assert path.stat().st_size > 256

    ph.record_probe_result(_make())
    archive_dir = ph.provider_health_dir() / "archive"
    archived = list(archive_dir.glob("chat_brain.*.jsonl"))
    assert archived, "rotation should have moved the oversized file to archive/"
    # Fresh file contains only the new row.
    new_lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(new_lines) == 1


def test_iter_roles_with_history_lists_files(isolated_home: Path) -> None:
    ph.record_probe_result(_make(role="chat_brain"))
    ph.record_probe_result(_make(role="image_generator"))
    roles = set(ph.iter_roles_with_history())
    assert roles == {"chat_brain", "image_generator"}


def test_tail_recent_skips_invalid_json(isolated_home: Path) -> None:
    ph.record_probe_result(_make())
    path = ph.provider_health_dir() / "chat_brain.jsonl"
    # Append a corrupted line and a blank line — both should be ignored.
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write("not json\n\n")
    ph.record_probe_result(_make())
    rows = ph.tail_recent("chat_brain", n=10)
    # 2 valid rows survive the invalid + blank lines.
    assert len(rows) == 2
