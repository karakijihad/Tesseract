"""Runtime fallback regression for chat_brain startup wiring."""

from __future__ import annotations

import pytest

from tesseract.brain import boot as brain_boot
from tesseract.config.loader import ProviderConnection, ProviderModel, ResolvedRef


def _cfg(provider: str, model: str) -> brain_boot.ChatBrainConfig:
    conn = ProviderConnection(
        tier="api",
        name=provider,
        adapter="openai" if provider == "openai" else "gemini",
        timeout_seconds=60,
        max_retries=3,
    )
    pmodel = ProviderModel(id=model.replace("-", "_"), model=model, kind="chat", fields={})
    ref = ResolvedRef(ref=f"api.{provider}.{pmodel.id}", connection=conn, model=pmodel)
    return brain_boot.ChatBrainConfig(
        provider=provider,
        model=model,
        tier="api",
        temperature=1.0,
        max_output_tokens=8192,
        context_window=400000,
        reasoning_effort="medium",
        knowledge_cutoff="2025-08-31",
        use_responses_api=(provider == "openai"),
        compact_threshold=0.40,
        keep_recent_turns=10,
        head_anchor_messages=3,
        active_window_tokens=None,
        summary_char_budget=8_000,
        provider_cfg={},
        ref=ref,
        tool_iteration_cap=25,
        consecutive_error_cap=3,
    )


def test_resolve_chat_brain_runtime_skips_unavailable_primary(monkeypatch) -> None:
    primary = _cfg("openai", "gpt-5.4-nano")
    fallback = _cfg("google", "gemini-2.5-flash")
    fallback_adapter = object()

    monkeypatch.setattr(brain_boot, "load_chat_brain_chain", lambda: [primary, fallback])

    def _build(cfg: brain_boot.ChatBrainConfig):
        if cfg is primary:
            raise RuntimeError("OPENAI_API_KEY missing")
        return fallback_adapter

    monkeypatch.setattr(brain_boot, "build_chat_brain_adapter", _build)

    chosen_cfg, adapter, options, chain = brain_boot.resolve_chat_brain_runtime()

    assert chosen_cfg is fallback
    assert adapter is fallback_adapter
    assert options.provider == "google"
    assert options.model == "gemini-2.5-flash"
    assert chain == [(fallback_adapter, options)]


def test_resolve_chat_brain_runtime_raises_when_no_provider_is_live(monkeypatch) -> None:
    cfg = _cfg("openai", "gpt-5.4-nano")
    monkeypatch.setattr(brain_boot, "load_chat_brain_chain", lambda: [cfg])

    def _raise(_cfg: brain_boot.ChatBrainConfig):
        raise RuntimeError("OPENAI_API_KEY missing")

    monkeypatch.setattr(brain_boot, "build_chat_brain_adapter", _raise)

    with pytest.raises(RuntimeError, match="no available chat_brain providers"):
        brain_boot.resolve_chat_brain_runtime()


# test_tars_repl_uses_resolve_chat_brain_runtime deleted 2026-07-13 with the
# REPL itself (prune wave; supervisor/Mirror is the sole entry point).
