"""Cost catch-up snapshot regression tests.

The Mirror HUD chips were stuck stale because the backend only emitted
`cost_delta` after a billed turn — reload or midnight rollover left the
chips reading stale localStorage. The new `CostLedger.snapshot()` powers
both `GET /api/cost/state` and the WS-connect `cost_state` envelope.

Tests cover: zero-state shape (fresh boot, no spend), voice provider
rollup (per-provider totals → role buckets), and midnight reset.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tesseract.brain.cost.ledger import (
    CostLedger,
    CostUsage,
    SttUsage,
    TtsUsage,
)


def _write_models_yaml(tmp_path: Path, log_path: Path) -> Path:
    """Minimal models.yaml exercising chat + voice pricing for the snapshot.

    Derived global cap: per_role(chat_brain=3.0 + observer_agent=1.0)
    + voice tts cap 0.5 + voice stt cap 0.3 = 4.8.
    warning_at_pct = 0.75 → warning_usd = 4.8 * 0.75 = 3.6.

    Tests that previously asserted cap_usd==5.0 and warning_usd==2.0
    now assert against the derived values from this fixture.
    """
    cfg = {
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": str(log_path),
            "per_role": {"chat_brain": 3.0, "observer_agent": 1.0},
            "voice": {
                "tts": {
                    "gemini_flash_tts": {
                        "cost_per_million_chars": 10.0,
                        "daily_budget_usd": 0.5,
                    },
                },
                "stt": {
                    "gemini_flash_audio": {
                        "cost_per_audio_hour": 0.09,
                        "daily_budget_usd": 0.3,
                    },
                },
            },
        },
        "roles": {
            "chat_brain": {
                "resolution": [
                    {
                        "model": "gpt-test",
                        "cost_per_mtok_in": 1.0,
                        "cost_per_mtok_out": 4.0,
                    },
                ],
            },
        },
    }
    p = tmp_path / "models.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


@pytest.fixture
def fresh_ledger(tmp_path: Path) -> CostLedger:
    log_path = tmp_path / "cost.jsonl"
    yaml_path = _write_models_yaml(tmp_path, log_path)
    return CostLedger.from_models_yaml(
        models_yaml=yaml_path,
        log_path=log_path,
        today_fn=lambda: "2026-04-27",
    )


def test_snapshot_zero_state_shape(fresh_ledger: CostLedger) -> None:
    """Fresh boot, no spend yet — chips need $0 / $cap immediately so the
    operator sees the current ceiling without waiting for a turn."""
    snap = fresh_ledger.snapshot()

    assert snap["enabled"] is True
    assert snap["local_date"] == "2026-04-27"
    # Derived cap: 3.0 + 1.0 + 0.5 + 0.3 = 4.8; warning_usd = 4.8 * 0.75 = 3.6
    assert snap["global"]["spent_usd"] == 0.0
    assert snap["global"]["cap_usd"] == pytest.approx(4.8)
    assert snap["global"]["warning_usd"] == pytest.approx(3.6)
    assert snap["global"]["warning"] is False
    assert snap["global"]["blocked"] is False

    # All four canonical roles populated even with no spend so the chips
    # render before the first billed turn.
    assert set(snap["roles"]) == {
        "chat_brain", "observer_agent", "voice_tts", "voice_stt",
    }
    assert snap["roles"]["chat_brain"]["role_cap_usd"] == 3.0
    assert snap["roles"]["observer_agent"]["role_cap_usd"] == 1.0
    assert snap["roles"]["voice_tts"]["role_cap_usd"] is None
    assert snap["roles"]["voice_tts"]["role_total_usd"] == 0.0

    assert "gemini_flash_tts" in snap["voice_providers"]["tts"]
    assert snap["voice_providers"]["tts"]["gemini_flash_tts"]["cap_usd"] == 0.5
    assert snap["voice_providers"]["tts"]["gemini_flash_tts"]["rate"] == 10.0
    assert snap["voice_providers"]["tts"]["gemini_flash_tts"]["spent_usd"] == 0.0


def test_snapshot_rolls_voice_provider_into_role_bucket(
    fresh_ledger: CostLedger,
) -> None:
    """The HUD VoiceCostChip reads `roles.voice_tts` / `roles.voice_stt`,
    not per-provider keys. Snapshot must aggregate provider totals so a
    multi-provider voice setup still surfaces a single chip total."""
    fresh_ledger.record_voice(
        "tts", "gemini_flash_tts", TtsUsage(char_count=1_000_000)
    )
    fresh_ledger.record_voice(
        "stt", "gemini_flash_audio", SttUsage(seconds=3600.0)
    )

    snap = fresh_ledger.snapshot()
    # 1M chars × $10/Mchar = $10 — but capped to one billing event.
    assert snap["roles"]["voice_tts"]["role_total_usd"] == pytest.approx(10.0)
    # 1h × $0.09/h = $0.09.
    assert snap["roles"]["voice_stt"]["role_total_usd"] == pytest.approx(0.09)
    assert snap["voice_providers"]["tts"]["gemini_flash_tts"]["spent_usd"] == pytest.approx(10.0)
    assert snap["voice_providers"]["stt"]["gemini_flash_audio"]["spent_usd"] == pytest.approx(0.09)
    # Global rolls everything up.
    assert snap["global"]["spent_usd"] == pytest.approx(10.09)


def test_snapshot_midnight_rollover_resets_totals(tmp_path: Path) -> None:
    """After local-date change, totals must read 0 again — otherwise the
    chip shows yesterday's spend against today's cap."""
    log_path = tmp_path / "cost.jsonl"
    yaml_path = _write_models_yaml(tmp_path, log_path)
    today = ["2026-04-27"]
    ledger = CostLedger.from_models_yaml(
        models_yaml=yaml_path,
        log_path=log_path,
        today_fn=lambda: today[0],
    )
    ledger.record("chat_brain", "gpt-test", CostUsage(input_tokens=1000, output_tokens=500))
    pre = ledger.snapshot()
    assert pre["roles"]["chat_brain"]["role_total_usd"] > 0.0

    today[0] = "2026-04-28"
    post = ledger.snapshot()
    assert post["local_date"] == "2026-04-28"
    assert post["roles"]["chat_brain"]["role_total_usd"] == 0.0
    assert post["global"]["spent_usd"] == 0.0
