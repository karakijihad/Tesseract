"""MO-9-13 — InterestsProfile load/decay/score round-trip.

Per CLAUDE.md log-safety: every test monkeypatches ``TESSERACT_HOME``
before instantiating writers so the profile lands under tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.orchestrator.brief.interests import (
    DEFAULT_HALF_LIFE_DAYS,
    InterestsProfile,
    Signal,
    WEIGHT_CLAMP,
    decay,
    load_profile,
    record_signal,
    save_profile,
    score_url,
)


@pytest.fixture(autouse=True)
def _tesseract_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    return tmp_path / "memory-store" / "interests" / "profile.yaml"


def test_load_profile_zero_state_when_missing(profile_path: Path) -> None:
    assert not profile_path.exists()
    profile = load_profile(profile_path)
    assert isinstance(profile, InterestsProfile)
    assert set(profile.pillars.keys()) == {"tech", "science", "politics"}
    for topics in profile.pillars.values():
        assert topics == {}


def test_load_profile_falls_back_on_malformed_yaml(profile_path: Path) -> None:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("not: a: dict: yaml::", encoding="utf-8")
    profile = load_profile(profile_path)
    # Falls back to zero-state with default pillars rather than crashing.
    assert set(profile.pillars.keys()) == {"tech", "science", "politics"}


def test_load_profile_drops_non_numeric_weights(profile_path: Path) -> None:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        yaml.safe_dump({
            "pillars": {
                "tech": {"good topic": 1.5, "bad weight": "oops"},
                "science": "not-a-dict",
            },
        }),
        encoding="utf-8",
    )
    profile = load_profile(profile_path)
    assert profile.pillars["tech"] == {"good topic": 1.5}
    # science was wrong-shape — ensure_pillars rehydrates the empty default.
    assert profile.pillars["science"] == {}


def test_save_and_reload_round_trip(profile_path: Path) -> None:
    initial = load_profile(profile_path)
    updated = record_signal(initial, "tech", "local-first software", Signal.INTERESTED)
    updated = record_signal(updated, "tech", "local-first software", Signal.DIG_DEEPER)
    save_profile(updated, profile_path)
    assert profile_path.exists()
    reloaded = load_profile(profile_path)
    assert reloaded.pillars["tech"]["local-first software"] == pytest.approx(1.5)


def test_record_signal_clamps_to_weight_clamp(profile_path: Path) -> None:
    profile = load_profile(profile_path)
    for _ in range(30):  # 30 × +1.0 would overshoot 10.0 without clamping
        profile = record_signal(profile, "tech", "AI safety", Signal.INTERESTED)
    assert profile.pillars["tech"]["AI safety"] == WEIGHT_CLAMP

    for _ in range(60):  # negative clamp boundary too
        profile = record_signal(profile, "tech", "AI safety", Signal.NOT_FOR_ME)
    assert profile.pillars["tech"]["AI safety"] == -WEIGHT_CLAMP


def test_record_signal_empty_topic_is_noop(profile_path: Path) -> None:
    profile = load_profile(profile_path)
    out = record_signal(profile, "tech", "   ", Signal.INTERESTED)
    assert out is profile  # short-circuit returns input unchanged


def test_decay_half_life_arithmetic(profile_path: Path) -> None:
    profile = load_profile(profile_path)
    profile = record_signal(profile, "tech", "alpha", Signal.INTERESTED)
    # After one full half-life, weight should halve.
    decayed = decay(profile, days=DEFAULT_HALF_LIFE_DAYS)
    assert decayed.pillars["tech"]["alpha"] == pytest.approx(0.5, abs=1e-6)
    assert decayed.last_decay_at  # stamped


def test_decay_prunes_below_threshold(profile_path: Path) -> None:
    profile = InterestsProfile(pillars={"tech": {"faint": 0.06}, "science": {}, "politics": {}})
    # 10 half-lives → factor ≈ 0.001 → faint becomes ≈ 0.00006, well below 0.05 prune floor.
    decayed = decay(profile, days=DEFAULT_HALF_LIFE_DAYS * 10)
    assert "faint" not in decayed.pillars["tech"]


def test_decay_zero_days_is_noop(profile_path: Path) -> None:
    profile = load_profile(profile_path)
    profile = record_signal(profile, "tech", "alpha", Signal.INTERESTED)
    out = decay(profile, days=0)
    assert out is profile


def test_score_url_sums_matching_topic_weights(profile_path: Path) -> None:
    profile = InterestsProfile(pillars={
        "tech": {"local-first": 2.0, "AI safety": -1.0, "wasm": 1.0},
        "science": {}, "politics": {},
    })
    # Title contains "local-first" and "wasm" (case-folded substring match).
    score = score_url(
        profile,
        "tech",
        title="Local-First and WASM in 2026",
        summary="A look at the local-first revival and how it intersects with wasm runtimes.",
    )
    assert score == pytest.approx(3.0)


def test_score_url_demotes_negative_topics(profile_path: Path) -> None:
    profile = InterestsProfile(pillars={
        "tech": {"AI safety": -2.0, "local-first": 1.0},
        "science": {}, "politics": {},
    })
    score = score_url(profile, "tech", "AI safety is hard", "discussion of AI safety")
    assert score == pytest.approx(-2.0)


def test_score_url_unknown_pillar_is_zero(profile_path: Path) -> None:
    profile = InterestsProfile(pillars={"tech": {"alpha": 5.0}, "science": {}, "politics": {}})
    assert score_url(profile, "world", "alpha beta", "alpha gamma") == 0.0


def test_load_profile_defaults_to_tesseract_home(tmp_path: Path, monkeypatch) -> None:
    """Calling load_profile() with no argument uses TESSERACT_HOME at call time."""
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    profile = load_profile()  # implicit path
    assert set(profile.pillars.keys()) == {"tech", "science", "politics"}
    # No file created on read.
    assert not (tmp_path / "memory-store" / "interests" / "profile.yaml").exists()


def test_save_profile_defaults_resolves_at_call_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    profile = load_profile()
    profile = record_signal(profile, "tech", "topic-A", Signal.INTERESTED)
    save_profile(profile)
    target = tmp_path / "memory-store" / "interests" / "profile.yaml"
    assert target.exists()
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert raw["pillars"]["tech"]["topic-A"] == 1.0
