"""Shared fixtures for the models.yaml → providers.yaml + roles.yaml split."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


def _baseline_providers() -> dict[str, Any]:
    return {
        "availability": {"max_consecutive_failures": 3},
        "cost_tracking": {
            "enabled": True,
            "warning_at_pct": 0.75,
            "log_file": "logs/cost-tracking.jsonl",
        },
        "api": {
            "openai": {
                "base_url": "${OPENAI_BASE_URL:-https://api.openai.com/v1}",
                "api_key_env": "OPENAI_API_KEY",
                "timeout_seconds": 60,
                "max_retries": 3,
                "adapter": "openai",
                "models": {
                    "gpt54_mini": {
                        "model": "gpt-5.4-mini",
                        "context_window": 400000,
                        "max_output_tokens": 8192,
                        "reasoning_effort": "high",
                        "temperature": 1.0,
                        "knowledge_cutoff": "2024-05-31",
                        "use_responses_api": True,
                        "cost_per_mtok_in": 0.75,
                        "cost_per_mtok_out": 4.50,
                    },
                    "gpt54_nano": {
                        "model": "gpt-5.4-nano",
                        "context_window": 400000,
                        "max_output_tokens": 8192,
                        "reasoning_effort": "high",
                        "temperature": 1.0,
                        "knowledge_cutoff": "2025-08-31",
                        "use_responses_api": True,
                        "cost_per_mtok_in": 0.20,
                        "cost_per_mtok_out": 1.25,
                    },
                },
            },
            "google": {
                "api_key_env": "GOOGLE_API_KEY",
                "timeout_seconds": 60,
                "max_retries": 3,
                "adapter": "gemini",
                "models": {
                    "gemini_25_flash": {
                        "model": "gemini-2.5-flash",
                        "context_window": 1000000,
                        "max_output_tokens": 8192,
                        "temperature": 0.7,
                        "use_responses_api": False,
                        "cost_per_mtok_in": 0.30,
                        "cost_per_mtok_out": 2.50,
                    },
                    "gemini_flash_tts": {
                        "kind": "tts",
                        "model": "gemini-2.5-flash-preview-tts",
                        "cost_per_million_chars": 10.00,
                    },
                },
            },
        },
        "cli": {
            "claude": {
                "command": "claude",
                "timeout_seconds": 300,
                "max_retries": 1,
                "stream_json_capable": True,
                "adapter": "cli",
                "models": {
                    "opus_47": {
                        "model": "claude-opus-4-7",
                        "context_window": 1000000,
                        "max_output_ratio": 0.35,
                        "temperature": 0.7,
                        "cost_per_mtok_in": 0,
                        "cost_per_mtok_out": 0,
                    },
                },
            },
        },
        "local": {
            "ollama": {
                "base_url": "${OLLAMA_BASE_URL:-http://localhost:11434}",
                "timeout_seconds": 120,
                "max_retries": 3,
                "models_endpoint": "/api/tags",
                "auto_start": True,
                "host": "this_pc",
                "adapter": "ollama",
                "models": {
                    "nomic_embed": {
                        "kind": "embedding",
                        "model": "nomic-embed-text",
                        "dimensions": 768,
                        "timeout_seconds": 30,
                        "max_retries": 3,
                    },
                },
            },
        },
    }


def _baseline_roles() -> dict[str, Any]:
    return {
        "embeddings": {"primary": "local.ollama.nomic_embed"},
        "roles": {
            "chat_brain": {
                "mode": "active",
                "primary": "api.openai.gpt54_mini",
                "fallbacks": ["api.openai.gpt54_nano", "api.google.gemini_25_flash"],
                "compact_threshold": 0.4,
                "keep_recent_turns": 10,
                "daily_budget_usd": 3.0,
                "notes": "TARS conversational layer.",
            },
            "observer_agent": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
                "fallbacks": ["api.google.gemini_25_flash"],
                "reasoning_effort_override": "low",
                "daily_budget_usd": 1.0,
            },
            "claude_cli": {
                "mode": "active",
                "primary": "cli.claude.opus_47",
            },
            "agents_default": {
                "mode": "active",
                "primary": "api.openai.gpt54_nano",
            },
        },
        "voice": {
            "default_voice_id": "Charon",
            "default_tone_prompt": "A male voice with a light British accent.",
            "tts": {
                "mode": "active",
                "primary": "api.google.gemini_flash_tts",
                "settings": {
                    "api.google.gemini_flash_tts": {
                        "voice_id": "Charon",
                        "timeout_seconds": 30,
                        "daily_budget_usd": 1.00,
                    },
                },
            },
        },
    }


@pytest.fixture
def config_files(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize a baseline providers.yaml + roles.yaml pair under tmp_path."""
    pp = tmp_path / "providers.yaml"
    rp = tmp_path / "roles.yaml"
    pp.write_text(yaml.safe_dump(_baseline_providers()), encoding="utf-8")
    rp.write_text(yaml.safe_dump(_baseline_roles()), encoding="utf-8")
    return pp, rp


@pytest.fixture
def baseline_providers() -> dict[str, Any]:
    return _baseline_providers()


@pytest.fixture
def baseline_roles() -> dict[str, Any]:
    return _baseline_roles()
