"""Cost UX overhaul (2026-04-27) — warning toast at 75% + overage ask at 100%.

Locks the new ledger contract:
- `check_warning(scope, spent, cap)` — returns True ONCE per scope per day
  when spend crosses 75% of cap. Idempotent across midnight rollover.
- `unlock_overage(scope_key)` + `is_overage_unlocked(scope_key)` — operator
  approves continuing past 100%. Cap test is skipped for that scope.
- `BudgetExhausted.scope_key()` — stable key for overage-ask correlation.
- `snapshot()` exposes `overage_unlocked` and `warned` arrays so the HUD
  can render in red without waiting for the next billed turn.
- ChatSession preflight retries the turn when `overage_ask_fn` approves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
import yaml

from tesseract.brain.chat import ChatSession
from tesseract.brain.cost import CostLedger
from tesseract.brain.cost.ledger import BudgetExhausted

# yaml-driven `warning_at_pct` default. Tests using literal 0.75 here
# match the value seeded by `_ledger_config` below.
WARNING_AT_PCT = 0.75
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


def _ledger_config(
    tmp_path: Path,
    cap_usd: float,
    warn_usd: float,
    per_role: dict | None = None,
    *,
    include_voice: bool = True,
) -> tuple[Path, Path]:
    """Build a minimal models.yaml for cost-UX-overhaul tests.

    Under the new contract `cap_usd` is derived: sum(per_role) + sum(voice caps).
    When `include_voice=True` a tiny voice block ($0.01 each) is added so
    `check_warning("voice:tts:gemini", ...)` can be called. When
    `include_voice=False` only per_role caps form the umbrella.

    `warn_pct` is derived from warn_usd / cap_usd so callers can keep
    expressing intent as dollar amounts.
    """
    log_path = tmp_path / "cost.jsonl"
    models_yaml = tmp_path / "models.yaml"
    voice_contrib = 0.02 if include_voice else 0.0  # $0.01 tts + $0.01 stt
    if per_role is not None:
        effective_per_role = per_role
    else:
        chat_cap = max(0.0, cap_usd - voice_contrib)
        effective_per_role = {"chat_brain": chat_cap}
    derived_cap = sum(effective_per_role.values()) + voice_contrib
    warn_pct = round(warn_usd / derived_cap, 10) if derived_cap > 0 else 0.75
    cost_tracking: dict = {
        "enabled": True,
        "warning_at_pct": warn_pct,
        "log_file": "logs/cost-tracking.jsonl",
        "per_role": effective_per_role,
    }
    if include_voice:
        cost_tracking["voice"] = {
            "tts": {"gemini": {"cost_per_million_chars": 16.0, "daily_budget_usd": 0.01}},
            "stt": {"gemini": {"cost_per_audio_hour": 0.36, "daily_budget_usd": 0.01}},
        }
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": {
            "chat_brain": {
                "resolution": [
                    {"model": "gpt-5.4-nano", "cost_per_mtok_in": 0.20, "cost_per_mtok_out": 1.25}
                ]
            },
        },
        "cost_tracking": cost_tracking,
    }
    models_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    return models_yaml, log_path


class _FakeAdapter(ModelAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        yield StreamChunk(type=ChunkType.TEXT, text="ok.")
        yield StreamChunk(
            type=ChunkType.STOP,
            stop_reason="end_turn",
            raw={"usage": {"input_tokens": 100, "output_tokens": 50}},
        )

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


# ── 1. Warning at 75% ────────────────────────────────────────────────────


def test_warning_fires_once_per_scope_per_day(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=1.0, warn_usd=0.75)
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    # Below 75% — no warning.
    assert ledger.check_warning("global", spent=0.50, cap=1.0) is False
    # First crossing — warns.
    assert ledger.check_warning("global", spent=0.80, cap=1.0) is True
    # Subsequent crossings — silent until midnight rollover.
    assert ledger.check_warning("global", spent=0.90, cap=1.0) is False
    assert ledger.check_warning("global", spent=1.00, cap=1.0) is False


def test_warning_independent_per_scope(tmp_path: Path) -> None:
    """A warning on `global` must not gag a separate warning on `role:chat_brain`."""
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=1.0, warn_usd=0.75)
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    assert ledger.check_warning("global", 0.80, 1.0) is True
    assert ledger.check_warning("role:chat_brain", 0.80, 1.0) is True
    assert ledger.check_warning("voice:tts:gemini", 0.80, 1.0) is True


def test_warning_threshold_is_exactly_75_pct(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=1.0, warn_usd=0.75)
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    # Just under threshold.
    assert ledger.check_warning("global", WARNING_AT_PCT - 1e-9, 1.0) is False
    # At threshold.
    assert ledger.check_warning("global", WARNING_AT_PCT, 1.0) is True


# ── 2. Overage unlock at 100% ────────────────────────────────────────────


def test_unlock_overage_skips_role_cap(tmp_path: Path) -> None:
    # chat_brain role cap = 0.10; global cap = 0.10 + 9.90 + voice(0.02) = 10.02
    # Spend 0.20 trips only the role cap; global (10.02) is not hit.
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=10.0, warn_usd=5.0,
        per_role={"chat_brain": 0.10, "_global_budget": 9.90},
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-04-27T12:00:00Z","local_date":"2026-04-27","role":"chat_brain",'
        '"model":"gpt-5.4-nano","input_tokens":0,"output_tokens":0,"cached_tokens":0,'
        '"cost_usd":0.20,"daily_total_usd":0.20,"role_total_usd":0.20}\n',
        encoding="utf-8",
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    # Without unlock — preflight raises.
    with pytest.raises(BudgetExhausted) as exc_info:
        ledger.check_preflight("chat_brain")
    assert exc_info.value.scope == "role"
    assert exc_info.value.scope_key() == "role:chat_brain"

    # Unlock that scope; preflight is now silent.
    ledger.unlock_overage("role:chat_brain")
    assert ledger.is_overage_unlocked("role:chat_brain") is True
    ledger.check_preflight("chat_brain")  # no raise


def test_unlock_does_not_leak_across_scopes(tmp_path: Path) -> None:
    """Unlocking `role:chat_brain` must NOT permit the global cap to overflow.

    Under the new derived-global model the only way for global to trip while
    the role-cap is independently lower is to set a small per_role so that
    the derived global (per_role + voice caps) is also below the spend.
    """
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=0.12, warn_usd=0.06, per_role={"chat_brain": 0.10}
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-04-27T12:00:00Z","local_date":"2026-04-27","role":"chat_brain",'
        '"model":"gpt-5.4-nano","input_tokens":0,"output_tokens":0,"cached_tokens":0,'
        '"cost_usd":0.50,"daily_total_usd":0.50,"role_total_usd":0.50}\n',
        encoding="utf-8",
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    ledger.unlock_overage("role:chat_brain")
    with pytest.raises(BudgetExhausted) as exc_info:
        ledger.check_preflight("chat_brain")
    assert exc_info.value.scope == "global"
    assert exc_info.value.scope_key() == "global"


# ── 2b. budget_state.blocked is unlock-aware ─────────────────────────────


def test_budget_state_blocked_is_unlock_aware(tmp_path: Path) -> None:
    """Reviewer fix #5: once an operator approves overage, `blocked`
    must drop to False so HUD chips render `is-overage` (red bg) rather
    than `--bad` (which would mask the new styling).

    No per-role cap on chat_brain so the only block is global. Unlocking
    global must clear `blocked` entirely.
    """
    # No per_role for chat_brain — only global cap applies. Global = voice 0.02.
    # Spend 0.50 > 0.02 → global blocked, role NOT blocked (no role cap).
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=0.02, warn_usd=0.01, per_role={}, include_voice=True
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-04-27T12:00:00Z","local_date":"2026-04-27","role":"chat_brain",'
        '"model":"gpt-5.4-nano","input_tokens":0,"output_tokens":0,"cached_tokens":0,'
        '"cost_usd":0.50,"daily_total_usd":0.50,"role_total_usd":0.50}\n',
        encoding="utf-8",
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    state = ledger.budget_state("chat_brain")
    assert state.blocked is True

    ledger.unlock_overage("global")
    state = ledger.budget_state("chat_brain")
    assert state.blocked is False, "global unlock must clear blocked flag"


def test_budget_state_blocked_role_unlock_does_not_clear_global(tmp_path: Path) -> None:
    """Role unlock must NOT mask global blocked state."""
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=0.10, warn_usd=0.05, per_role={"chat_brain": 0.10}
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-04-27T12:00:00Z","local_date":"2026-04-27","role":"chat_brain",'
        '"model":"gpt-5.4-nano","input_tokens":0,"output_tokens":0,"cached_tokens":0,'
        '"cost_usd":0.50,"daily_total_usd":0.50,"role_total_usd":0.50}\n',
        encoding="utf-8",
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    ledger.unlock_overage("role:chat_brain")
    # Global is still blocked; role unlock alone is insufficient.
    state = ledger.budget_state("chat_brain")
    assert state.blocked is True


# ── 3. BudgetExhausted scope_key ─────────────────────────────────────────


def test_budget_exhausted_scope_key_shape() -> None:
    role_exc = BudgetExhausted(role="chat_brain", spent_usd=1.0, cap_usd=0.5, scope="role")
    assert role_exc.scope_key() == "role:chat_brain"

    global_exc = BudgetExhausted(role="chat_brain", spent_usd=1.0, cap_usd=0.5, scope="global")
    assert global_exc.scope_key() == "global"

    voice_exc = BudgetExhausted(
        role="voice:tts:gemini", spent_usd=1.0, cap_usd=0.5, scope="voice"
    )
    assert voice_exc.scope_key() == "voice:tts:gemini"


# ── 4. Snapshot exposes new fields ───────────────────────────────────────


def test_snapshot_exposes_overage_and_warning_state(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=1.0, warn_usd=0.75)
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    snap = ledger.snapshot()
    assert snap["overage_unlocked"] == []
    assert snap["warned"] == []

    ledger.check_warning("global", spent=0.80, cap=1.0)
    ledger.unlock_overage("role:chat_brain")
    snap = ledger.snapshot()
    assert "global" in snap["warned"]
    assert "role:chat_brain" in snap["overage_unlocked"]


# ── 5. Midnight rollover clears both sets ────────────────────────────────


def test_midnight_rollover_clears_warning_and_overage(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=1.0, warn_usd=0.75)
    today = {"date": "2026-04-27"}
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: today["date"]
    )
    ledger.check_warning("global", 0.80, 1.0)
    ledger.unlock_overage("role:chat_brain")
    assert ledger.is_overage_unlocked("role:chat_brain") is True

    # Trip midnight rollover.
    today["date"] = "2026-04-28"
    assert ledger.is_overage_unlocked("role:chat_brain") is False
    # And the warning fires fresh.
    assert ledger.check_warning("global", 0.80, 1.0) is True


# ── 6. ChatSession integrates with overage_ask_fn ────────────────────────


async def test_chat_session_overage_ask_approve_retries_turn(tmp_path: Path) -> None:
    # role cap = 0.10; global = 0.10 + 9.90 + voice(0.02) = 10.02. Spend 0.20
    # trips only the role cap — global headroom exists so role unlock suffices.
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=10.0, warn_usd=5.0,
        per_role={"chat_brain": 0.10, "_global_budget": 9.90},
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-04-27T12:00:00Z","local_date":"2026-04-27","role":"chat_brain",'
        '"model":"gpt-5.4-nano","input_tokens":0,"output_tokens":0,"cached_tokens":0,'
        '"cost_usd":0.20,"daily_total_usd":0.20,"role_total_usd":0.20}\n',
        encoding="utf-8",
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    adapter = _FakeAdapter()
    asks_seen: list[BudgetExhausted] = []

    async def approve(exc: BudgetExhausted) -> bool:
        asks_seen.append(exc)
        return True

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=ledger,
        overage_ask_fn=approve,
    )

    chunks: list[StreamChunk] = []
    async for c in session.send("hello"):
        chunks.append(c)

    assert len(asks_seen) == 1
    assert asks_seen[0].scope_key() == "role:chat_brain"
    assert adapter.calls == 1, "adapter must run after operator approves overage"
    assert ledger.is_overage_unlocked("role:chat_brain") is True


async def test_chat_session_overage_ask_deny_aborts_turn(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=10.0, warn_usd=5.0,
        per_role={"chat_brain": 0.10, "_global_budget": 9.90},
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-04-27T12:00:00Z","local_date":"2026-04-27","role":"chat_brain",'
        '"model":"gpt-5.4-nano","input_tokens":0,"output_tokens":0,"cached_tokens":0,'
        '"cost_usd":0.20,"daily_total_usd":0.20,"role_total_usd":0.20}\n',
        encoding="utf-8",
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    adapter = _FakeAdapter()

    async def deny(exc: BudgetExhausted) -> bool:
        return False

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=ledger,
        overage_ask_fn=deny,
    )

    chunks: list[StreamChunk] = []
    async for c in session.send("hello"):
        chunks.append(c)

    assert adapter.calls == 0
    errs = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(errs) == 1
    assert errs[0].raw.get("reason") == "budget_exhausted"
    assert ledger.is_overage_unlocked("role:chat_brain") is False


async def test_chat_session_paused_source_skips_overage_ask(tmp_path: Path) -> None:
    # budget.pause_source (P3 MCP) is an explicit operator hold, NOT a cap
    # overage — check_preflight raises scope="paused" and the turn must abort
    # WITHOUT presenting the overage-ask card (which would mislead + no-op).
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=10.0, warn_usd=5.0,
        per_role={"chat_brain": 5.0, "_global_budget": 5.0},
    )
    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-27"
    )
    ledger.pause_source("chat_brain")  # no spend — a pause blocks regardless
    adapter = _FakeAdapter()
    asks_seen: list[BudgetExhausted] = []

    async def approve(exc: BudgetExhausted) -> bool:
        asks_seen.append(exc)
        return True

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=ledger,
        overage_ask_fn=approve,
    )

    chunks: list[StreamChunk] = []
    async for c in session.send("hello"):
        chunks.append(c)

    assert asks_seen == []  # paused → overage card never shown
    assert adapter.calls == 0  # turn aborted
    errs = [c for c in chunks if c.type == ChunkType.ERROR]
    assert any(e.raw.get("scope") == "paused" for e in errs)
