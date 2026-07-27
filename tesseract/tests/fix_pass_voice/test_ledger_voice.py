"""Voice cost-ledger tests.

Covers:
- pricing math for both kinds (TTS chars / STT seconds)
- budget gate (`voice_check_preflight` raises BudgetExhausted at the cap)
- midnight roll resets per-provider voice totals
- JSONL persistence + reload-from-log
- subscriber fan-out fires on `record_voice` with synthesized event/state

Both lanes are cloud-only Gemini after the G2 cutover (2026-04-26):
TTS = `gemini_flash_tts`, STT = `gemini_flash_audio`. No local
provider, no fallback policy — hitting either cap raises BudgetExhausted
and the WS handler emits a `voice_instruction` toast.

Tests build a fresh `CostLedger` with an in-tmp YAML so global state
never leaks between cases. The `voice_ledger` / `voice_ledger_yaml`
fixtures live in the suite-level conftest.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from tesseract.brain.cost import (
    BudgetExhausted,
    CostLedger,
    SttUsage,
    TtsUsage,
)


def _new_ledger(tmp_path, voice_ledger_yaml: str, today: str | None = None):
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(voice_ledger_yaml, encoding="utf-8")
    log_path = tmp_path / "cost.jsonl"
    today_fn = (lambda: today) if today else None
    return CostLedger.from_models_yaml(
        models_yaml=yaml_path,
        log_path=log_path,
        today_fn=today_fn,
    )


# ─── pricing math ──────────────────────────────────────────


def test_record_tts_charges_per_million_chars(voice_ledger):
    # 1_000_000 chars at $10/M = $10
    event = voice_ledger.record_voice(
        "tts", "gemini_flash_tts", TtsUsage(char_count=1_000_000)
    )
    assert event.cost_usd == pytest.approx(10.0)
    assert event.kind == "voice_tts"
    assert event.provider == "gemini_flash_tts"


def test_record_stt_charges_per_audio_hour(voice_ledger):
    """Gemini Flash audio: $0.09/audio-hour → 60 s = $0.0015."""
    event = voice_ledger.record_voice(
        "stt", "gemini_flash_audio", SttUsage(seconds=60.0)
    )
    assert event.cost_usd == pytest.approx(0.09 * 60.0 / 3600.0)
    assert event.kind == "voice_stt"
    assert event.provider == "gemini_flash_audio"


# ─── persistence ───────────────────────────────────────────


def test_record_voice_appends_jsonl(voice_ledger):
    voice_ledger.record_voice("tts", "gemini_flash_tts", TtsUsage(char_count=10_000))
    raw = voice_ledger.log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 1
    entry = json.loads(raw[0])
    assert entry["kind"] == "voice_tts"
    assert entry["provider"] == "gemini_flash_tts"
    assert entry["char_count"] == 10_000
    # 10_000 chars * $10/Mchars = $0.10
    assert entry["cost_usd"] == pytest.approx(0.10)


def test_voice_provider_total_accumulates(voice_ledger):
    voice_ledger.record_voice("tts", "gemini_flash_tts", TtsUsage(char_count=10_000))
    voice_ledger.record_voice("tts", "gemini_flash_tts", TtsUsage(char_count=10_000))
    assert voice_ledger.voice_provider_total_usd("gemini_flash_tts") == pytest.approx(0.20)


# ─── budget gate ───────────────────────────────────────────


def test_voice_check_preflight_below_cap_passes(voice_ledger):
    voice_ledger.record_voice(
        "tts", "gemini_flash_tts", TtsUsage(char_count=1_000),  # ~$0.01
    )
    voice_ledger.voice_check_preflight("tts", "gemini_flash_tts")  # no raise


def test_voice_check_preflight_above_cap_raises(voice_ledger):
    # Cap is $0.20; one record at $0.20 trips the gate on the next call.
    voice_ledger.record_voice(
        "tts", "gemini_flash_tts", TtsUsage(char_count=20_000),  # = $0.20
    )
    with pytest.raises(BudgetExhausted) as exc_info:
        voice_ledger.voice_check_preflight("tts", "gemini_flash_tts")
    assert exc_info.value.scope == "voice"


def test_voice_check_preflight_unknown_provider_raises(voice_ledger):
    with pytest.raises(RuntimeError, match="no voice pricing"):
        voice_ledger.voice_check_preflight("tts", "unknown_voice")


def test_voice_check_preflight_zero_cap_is_free(tmp_path):
    """Cap == 0 = free at use-time (local Piper / local Whisper). Must
    not trip BudgetExhausted on the first call (regression: $0 spent >=
    $0 cap was raising before the fix)."""
    yaml_body = """
roles:
  chat_brain:
    resolution:
      - tier: api
        provider: openai
        model: gpt-test
        cost_per_mtok_in: 0.20
        cost_per_mtok_out: 1.25

cost_tracking:
  enabled: true
  warning_at_pct: 0.75
  log_file: "logs/cost-tracking.jsonl"
  per_role:
    chat_brain: 5.00
  voice:
    tts:
      piper_local:
        cost_per_million_chars: 0.00
        daily_budget_usd: 0.00
"""
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(yaml_body, encoding="utf-8")
    ledger = CostLedger.from_models_yaml(
        models_yaml=yaml_path,
        log_path=tmp_path / "cost.jsonl",
    )
    # First call must pass — cap of 0 means free, not exhausted.
    ledger.voice_check_preflight("tts", "piper_local")
    # Even after recording usage at $0, still free.
    ledger.record_voice("tts", "piper_local", TtsUsage(char_count=10_000))
    ledger.voice_check_preflight("tts", "piper_local")


def test_voice_check_preflight_global_cap_zero_is_free(tmp_path):
    """Global cap == 0 (all-local config — every per-role / per-voice
    budget is $0) must not trip BudgetExhausted on the first call.
    Same regression as the per-provider zero-cap fix, applied to the
    outer global envelope."""
    yaml_body = """
roles:
  chat_brain:
    resolution:
      - tier: api
        provider: openai
        model: gpt-test
        cost_per_mtok_in: 0.20
        cost_per_mtok_out: 1.25

cost_tracking:
  enabled: true
  warning_at_pct: 0.75
  log_file: "logs/cost-tracking.jsonl"
  per_role:
    chat_brain: 0.00
  voice:
    tts:
      piper_local:
        cost_per_million_chars: 0.00
        daily_budget_usd: 0.00
"""
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(yaml_body, encoding="utf-8")
    ledger = CostLedger.from_models_yaml(
        models_yaml=yaml_path,
        log_path=tmp_path / "cost.jsonl",
    )
    assert ledger.cap_usd == 0.0
    # First call must pass — global cap of 0 means free, not exhausted.
    ledger.voice_check_preflight("tts", "piper_local")


def test_voice_check_preflight_global_cap_blocks(voice_ledger):
    # Force daily total to exceed the global cap directly.
    voice_ledger._daily_total_usd = voice_ledger.cap_usd  # type: ignore[attr-defined]
    voice_ledger._current_local_date = date.today().isoformat()  # type: ignore[attr-defined]
    with pytest.raises(BudgetExhausted) as exc_info:
        voice_ledger.voice_check_preflight("tts", "gemini_flash_tts")
    assert exc_info.value.scope == "global"


# ─── midnight roll + reload ────────────────────────────────


def test_midnight_roll_resets_voice_totals(tmp_path, voice_ledger_yaml):
    state = {"day": "2026-04-25"}
    ledger = _new_ledger(tmp_path, voice_ledger_yaml, today=state["day"])
    # Replace the today_fn with one that follows `state` so we can advance.
    ledger._today_fn = lambda: state["day"]  # type: ignore[attr-defined]

    ledger.record_voice("tts", "gemini_flash_tts", TtsUsage(char_count=10_000))
    assert ledger.voice_provider_total_usd("gemini_flash_tts") > 0

    state["day"] = "2026-04-26"
    # Budget state read triggers the midnight roll.
    _ = ledger.budget_state("chat_brain")
    assert ledger.voice_provider_total_usd("gemini_flash_tts") == 0.0


def test_seed_from_log_replays_voice_entries(tmp_path, voice_ledger_yaml):
    today = date.today().isoformat()
    log_path = tmp_path / "cost.jsonl"
    log_path.write_text(
        json.dumps({
            "ts": "2026-04-25T10:00:00Z",
            "local_date": today,
            "kind": "voice_tts",
            "provider": "gemini_flash_tts",
            "char_count": 100_000,
            "seconds": 0.0,
            "cost_usd": 1.0,
            "daily_total_usd": 1.0,
            "provider_total_usd": 1.0,
        }) + "\n",
        encoding="utf-8",
    )
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(voice_ledger_yaml, encoding="utf-8")

    ledger = CostLedger.from_models_yaml(models_yaml=yaml_path, log_path=log_path)
    assert ledger.voice_provider_total_usd("gemini_flash_tts") == pytest.approx(1.0)


# ─── subscriber fan-out ────────────────────────────────────


def test_record_voice_fires_subscribers(voice_ledger):
    captured: list[tuple] = []

    def cb(event, state):
        captured.append((event.role, event.model, state.role_spent_usd))

    voice_ledger.subscribe(cb)
    voice_ledger.record_voice("tts", "gemini_flash_tts", TtsUsage(char_count=10_000))
    assert len(captured) == 1
    role, model, role_spent = captured[0]
    assert role == "voice_tts"
    assert model == "gemini_flash_tts"
    assert role_spent == pytest.approx(0.10)


def test_record_voice_invalid_kind_raises(voice_ledger):
    with pytest.raises(RuntimeError, match="voice kind"):
        voice_ledger.record_voice("nope", "gemini_flash_tts", TtsUsage(char_count=1))
