"""ChatSession preflight + record — tier:api blocks at cap, tier:cli passes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
import yaml

from tesseract.brain.chat import ChatSession
from tesseract.brain.cost import CostLedger
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ModelAdapter,
    StreamChunk,
)


class _FakeAdapter(ModelAdapter):
    """Yields one STOP with the configured usage, no text, no tool calls."""

    def __init__(self, input_tokens: int = 100, output_tokens: int = 50, cached_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        yield StreamChunk(type=ChunkType.TEXT, text="ok.")
        usage = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.cached_tokens:
            usage["cached_tokens"] = self.cached_tokens
        yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": usage})

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _ledger_config(tmp_path: Path, cap_usd: float, warn_usd: float, per_role: dict | None = None) -> tuple[Path, Path]:
    """Build a minimal models.yaml for budget-gate tests.

    Under the new ledger contract `cap_usd` is derived from per_role + voice
    caps. If no per_role is given we create a single `chat_brain` entry equal
    to `cap_usd` so the derived global cap equals the caller's intent.
    `warning_at_pct` is derived from warn_usd / cap_usd.
    """
    log_path = tmp_path / "cost.jsonl"
    models_yaml = tmp_path / "models.yaml"
    effective_per_role = per_role if per_role is not None else {"_budget": cap_usd}
    warn_pct = round(warn_usd / cap_usd, 10) if cap_usd > 0 else 0.75
    data = {
        "providers": {"openai": {"timeout_seconds": 60, "max_retries": 3}},
        "roles": {
            "chat_brain": {
                "resolution": [
                    {"model": "gpt-5.4-nano", "cost_per_mtok_in": 0.20, "cost_per_mtok_out": 1.25}
                ]
            },
            "claude_cli": {
                "resolution": [
                    {"model": "claude-opus-4-7", "cost_per_mtok_in": 0, "cost_per_mtok_out": 0}
                ]
            },
        },
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": warn_pct,
            "log_file": "logs/cost-tracking.jsonl",
            "per_role": effective_per_role,
        },
    }
    models_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    return models_yaml, log_path


def _seed_spent(log_path: Path, role: str, cost_usd: float, local_date: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": f"{local_date}T12:00:00Z",
            "local_date": local_date,
            "role": role,
            "model": "gpt-5.4-nano",
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": cost_usd,
            "daily_total_usd": cost_usd,
            "role_total_usd": cost_usd,
        }) + "\n")


async def _collect(gen) -> list[StreamChunk]:
    out: list[StreamChunk] = []
    async for chunk in gen:
        out.append(chunk)
    return out


async def test_preflight_blocks_api_turn_when_global_cap_hit(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=0.10, warn_usd=0.05)
    _seed_spent(log_path, "chat_brain", 0.20, "2026-04-24")

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )
    adapter = _FakeAdapter()

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=ledger,
    )

    chunks = await _collect(session.send("hello"))

    assert adapter.calls == 0, "adapter must not be called when cap is hit"
    assert session.history == [], "user message must not be appended when blocked"
    errs = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(errs) == 1
    assert errs[0].raw.get("reason") == "budget_exhausted"
    assert errs[0].raw.get("scope") == "global"
    assert errs[0].raw.get("role") == "chat_brain"


async def test_preflight_blocks_on_role_subcap(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(
        tmp_path, cap_usd=10.0, warn_usd=5.0, per_role={"chat_brain": 0.10}
    )
    _seed_spent(log_path, "chat_brain", 0.20, "2026-04-24")

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )
    adapter = _FakeAdapter()

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=ledger,
    )

    chunks = await _collect(session.send("hello"))

    assert adapter.calls == 0
    errs = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(errs) == 1
    assert errs[0].raw.get("scope") == "role"


async def test_cli_tier_bypasses_cap(tmp_path: Path) -> None:
    """A tier:cli turn proceeds even when the daily cap is already blown."""
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=0.10, warn_usd=0.05)
    _seed_spent(log_path, "chat_brain", 1.00, "2026-04-24")

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )
    adapter = _FakeAdapter(input_tokens=0, output_tokens=0)

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="claude-opus-4-7", role="claude_cli", tier="cli"),
        cost_ledger=ledger,
    )

    chunks = await _collect(session.send("hello"))

    assert adapter.calls == 1, "CLI tier must not be blocked by the global cap"
    assert not any(c.type == ChunkType.ERROR for c in chunks)


async def test_successful_turn_records_usage(tmp_path: Path) -> None:
    models_yaml, log_path = _ledger_config(tmp_path, cap_usd=10.0, warn_usd=5.0)

    ledger = CostLedger.from_models_yaml(
        models_yaml=models_yaml, log_path=log_path, today_fn=lambda: "2026-04-24"
    )
    adapter = _FakeAdapter(input_tokens=1_000_000, output_tokens=1_000_000)

    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=ledger,
    )

    await _collect(session.send("hello"))

    # 1M input @ $0.20 + 1M output @ $1.25 = $1.45
    state = ledger.budget_state("chat_brain")
    assert state.spent_usd == pytest.approx(1.45, abs=1e-9)
    assert state.role_spent_usd == pytest.approx(1.45, abs=1e-9)

    # JSONL entry was appended with the correct cost
    entries = [
        json.loads(ln)
        for ln in log_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(entries) == 1
    assert entries[0]["cost_usd"] == pytest.approx(1.45, abs=1e-9)
    assert entries[0]["role"] == "chat_brain"


async def test_no_ledger_session_still_works(tmp_path: Path) -> None:
    """A session without a ledger runs turns normally — no preflight, no record."""
    adapter = _FakeAdapter()
    session = ChatSession(
        adapter=adapter,
        system_prompt="you are tars",
        max_tool_iterations=25,
        max_consecutive_adapter_errors=3,
        options=AdapterOptions(model="gpt-5.4-nano", role="chat_brain", tier="api"),
        cost_ledger=None,
    )

    chunks = await _collect(session.send("hello"))
    assert adapter.calls == 1
    assert not any(c.type == ChunkType.ERROR for c in chunks)
