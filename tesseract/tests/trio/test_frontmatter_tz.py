"""W1 D6b — naive frontmatter timestamps crashed every aware-datetime
compare downstream (memory.search over MCP 500'd in stage_a_prefilter;
librarian recency window crashed). Naive parses as UTC at the boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tesseract.memory.types import MemoryFrontmatter


def _fm(**overrides) -> MemoryFrontmatter:
    base = {
        "id": "mem_deadbeef",
        "type": "project",
        "title": "tz test",
        "created_at": "2026-07-01T10:00:00+00:00",
    }
    base.update(overrides)
    return MemoryFrontmatter.from_yaml_dict(base)


def test_naive_iso_string_becomes_utc_aware():
    fm = _fm(created_at="2026-07-01T10:00:00", updated_at="2026-07-02T11:30:00")
    assert fm.created_at.tzinfo is timezone.utc
    assert fm.updated_at.tzinfo is timezone.utc


def test_naive_datetime_object_becomes_utc_aware():
    # yaml.safe_load hands pydantic a datetime OBJECT (already parsed, naive).
    fm = _fm(created_at=datetime(2026, 7, 1, 10, 0, 0))
    assert fm.created_at == datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_aware_timestamps_pass_through_unchanged():
    tz = timezone(timedelta(hours=2))
    fm = _fm(created_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=tz))
    assert fm.created_at.utcoffset() == timedelta(hours=2)


def test_expiry_at_normalized_too():
    fm = _fm(expiry_at="2026-08-01T00:00:00")
    assert fm.expiry_at.tzinfo is timezone.utc


def test_age_arithmetic_regression():
    """The exact expression that 500'd memory.search (retrieval.py
    stage_a_prefilter): now(aware) - updated_at(previously naive)."""
    fm = _fm(updated_at="2026-07-02T11:30:00")
    age_days = (datetime.now(timezone.utc) - fm.updated_at).days
    assert isinstance(age_days, int)
