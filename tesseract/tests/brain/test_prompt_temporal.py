from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from tesseract.brain.prompt import _build_now_section

LOCAL = timezone(timedelta(hours=1))

_IDENTITY_YAML = (
    "born_at: '2026-04-21T20:10:00+01:00'\n"
    "time_of_day_buckets:\n"
    "  morning: { start: '05:00', end: '12:00' }\n"
    "  afternoon: { start: '12:00', end: '17:00' }\n"
    "  evening: { start: '17:00', end: '21:00' }\n"
    "  night: { start: '21:00', end: '05:00' }\n"
)


def test_build_now_section_emits_local_time_and_time_of_day(tmp_path, monkeypatch):
    cfg = tmp_path / "identity.yaml"
    cfg.write_text(_IDENTITY_YAML)
    monkeypatch.setattr("tesseract.brain.prompt._IDENTITY_CONFIG_PATH", cfg)

    fixed_now = datetime(2026, 5, 20, 14, 32, tzinfo=LOCAL)
    with patch("tesseract.brain.prompt._now_local", return_value=fixed_now):
        section = _build_now_section({})
    assert "Local time: 14:32 (afternoon)" in section
    assert "Today: 2026-05-20 Wednesday" in section


def test_build_now_section_emits_age_day_n_born_date(tmp_path, monkeypatch):
    cfg = tmp_path / "identity.yaml"
    cfg.write_text(_IDENTITY_YAML)
    monkeypatch.setattr("tesseract.brain.prompt._IDENTITY_CONFIG_PATH", cfg)

    fixed_now = datetime(2026, 5, 20, 14, 32, tzinfo=LOCAL)
    with patch("tesseract.brain.prompt._now_local", return_value=fixed_now):
        section = _build_now_section({})
    assert "Age: day 28 (born 2026-04-21)" in section


def test_build_now_section_falls_back_to_legacy_form_when_config_missing(tmp_path, monkeypatch):
    bad_cfg = tmp_path / "nope.yaml"
    monkeypatch.setattr("tesseract.brain.prompt._IDENTITY_CONFIG_PATH", bad_cfg)
    fixed_now = datetime(2026, 5, 20, 14, 32, tzinfo=LOCAL)
    with patch("tesseract.brain.prompt._now_local", return_value=fixed_now):
        section = _build_now_section({})
    # Falls back to single `- Today:` line — does NOT raise.
    assert "Today:" in section
    assert "Local time:" not in section  # fallback excludes the new fields
    assert "Age:" not in section


def test_build_now_section_raises_loudly_path_falls_back_on_missing_born_at(tmp_path, monkeypatch):
    cfg = tmp_path / "identity.yaml"
    cfg.write_text(
        "time_of_day_buckets:\n"
        "  morning: { start: '05:00', end: '12:00' }\n"
        "  afternoon: { start: '12:00', end: '17:00' }\n"
        "  evening: { start: '17:00', end: '21:00' }\n"
        "  night: { start: '21:00', end: '05:00' }\n"
    )
    monkeypatch.setattr("tesseract.brain.prompt._IDENTITY_CONFIG_PATH", cfg)
    fixed_now = datetime(2026, 5, 20, 14, 32, tzinfo=LOCAL)
    with patch("tesseract.brain.prompt._now_local", return_value=fixed_now):
        section = _build_now_section({})
    # Missing required key raises inside the try — fail-open catches it.
    assert "Today:" in section
    assert "Local time:" not in section


def test_build_now_section_warns_fallback_at_most_once(tmp_path, monkeypatch, caplog):
    import logging
    bad_cfg = tmp_path / "nope.yaml"
    monkeypatch.setattr("tesseract.brain.prompt._IDENTITY_CONFIG_PATH", bad_cfg)
    monkeypatch.setattr("tesseract.brain.prompt._TEMPORAL_FALLBACK_WARNED", False)
    fixed_now = datetime(2026, 5, 20, 14, 32, tzinfo=LOCAL)
    with caplog.at_level(logging.ERROR, logger="tesseract.brain.prompt"):
        with patch("tesseract.brain.prompt._now_local", return_value=fixed_now):
            _build_now_section({})
            _build_now_section({})
            _build_now_section({})
    fallback_records = [r for r in caplog.records if "falling back to legacy now-section" in r.message]
    assert len(fallback_records) == 1, f"expected 1 warning, got {len(fallback_records)}"
