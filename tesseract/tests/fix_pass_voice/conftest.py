"""Shared fixtures for the fix_pass_voice suite.

`voice_ledger_yaml` is the canonical models.yaml stub for any test that
needs a `CostLedger` with the post-G2 voice block wired (2026-04-26).
Both lanes are cloud-only Gemini on a single GOOGLE_API_KEY; caps are
small but non-zero so a deliberately large recorded usage trips the
gate.
"""

from __future__ import annotations

import pytest

from tesseract.brain.cost import CostLedger


VOICE_LEDGER_YAML = """
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
      gemini_flash_tts:
        cost_per_million_chars: 10.00
        daily_budget_usd: 0.20
    stt:
      gemini_flash_audio:
        cost_per_audio_hour: 0.09
        daily_budget_usd: 0.30
"""


@pytest.fixture
def voice_ledger_yaml() -> str:
    """Canonical voice-ledger YAML body. Tests typically write it to
    `tmp_path / "models.yaml"` before constructing the CostLedger."""
    return VOICE_LEDGER_YAML


@pytest.fixture
def voice_ledger(tmp_path):
    """A CostLedger with the voice block wired, isolated to `tmp_path`."""
    yaml_path = tmp_path / "models.yaml"
    yaml_path.write_text(VOICE_LEDGER_YAML, encoding="utf-8")
    return CostLedger.from_models_yaml(
        models_yaml=yaml_path,
        log_path=tmp_path / "cost.jsonl",
    )
