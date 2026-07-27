"""Typed ``channels.yaml`` loader — two-tier schema (defaults + per-channel).

Rewritten 2026-05-18 for the global-defaults refactor:

* ``defaults:`` holds retention / attachments / extract / cost / gate_policy.
* Per-channel blocks (``telegram:`` and any future ``whatsapp:`` /
  ``signal:`` / …) hold channel-specific fields plus optional sparse
  overrides for any default block.
* ``cfg.telegram`` / ``cfg.<name>`` returns a merged :class:`ResolvedChannel`
  view — overrides on top of defaults — so consumers read one shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tesseract.integrations._channels_config import (
    ChannelsConfig,
    ResolvedChannel,
    load_channels_config,
)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_defaults_when_file_absent(tmp_path) -> None:
    """No yaml → built-in defaults; cfg.telegram is a synthetic view with
    enabled=False (no channel block declared) but the global blocks are
    populated from the model defaults."""
    cfg = load_channels_config(tmp_path / "missing.yaml")
    assert isinstance(cfg, ChannelsConfig)
    assert cfg.defaults.retention.max_turns_in_context == 20
    assert cfg.defaults.retention.inactivity_reset_minutes == 360
    assert cfg.defaults.attachments.voice.max_seconds == 600
    assert cfg.defaults.gate_policy.on_ask == "workspace_nudge"
    # Synthetic channel view for a name not in the YAML.
    syn = cfg.telegram
    assert isinstance(syn, ResolvedChannel)
    assert syn.enabled is False
    assert syn.attachments.voice.max_seconds == 600
    assert syn.gate_policy.on_ask == "workspace_nudge"


def test_per_channel_inherits_global_defaults(tmp_path) -> None:
    """Channel block declares only channel-specific fields — every
    global block is inherited from ``defaults`` automatically."""
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  retention:
    max_turns_in_context: 30
    inactivity_reset_minutes: 120
  attachments:
    voice: { max_seconds: 30 }
    audio: { max_seconds: 60 }
    photo: { max_bytes: 1024 }
    document: { max_bytes: 2048 }
    video: { max_seconds: 10, max_bytes: 4096 }
  extract:
    document_chars: 100
    image_caption_chars: 50
  cost:
    daily_max_usd: 9.99
    per_role:
      channel_vision: 1.0
  gate_policy:
    on_ask: deny
    approve_next_turn_ttl_s: 90

telegram:
  enabled: false
  display_name: TG
""",
    )
    cfg = load_channels_config(path)
    tg = cfg.telegram
    assert tg.enabled is False
    assert tg.display_name == "TG"
    # Every global field inherited from defaults.
    assert tg.retention.max_turns_in_context == 30
    assert tg.retention.inactivity_reset_minutes == 120
    assert tg.attachments.voice.max_seconds == 30
    assert tg.attachments.video.max_bytes == 4096
    assert tg.extract.document_chars == 100
    assert tg.cost.daily_max_usd == 9.99
    assert tg.cost.per_role["channel_vision"] == 1.0
    assert tg.gate_policy.on_ask == "deny"
    assert tg.gate_policy.approve_next_turn_ttl_s == 90


def test_per_channel_sparse_override_wins_over_defaults(tmp_path) -> None:
    """A channel block can override any global block — sparse: only the
    keys you list win; the rest still inherit."""
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  retention:
    max_turns_in_context: 20
    inactivity_reset_minutes: 360

telegram:
  enabled: true
  display_name: TG
  retention:
    max_turns_in_context: 7
    inactivity_reset_minutes: 45
""",
    )
    cfg = load_channels_config(path)
    tg = cfg.telegram
    assert tg.retention.max_turns_in_context == 7         # override
    assert tg.retention.inactivity_reset_minutes == 45    # override
    # Global defaults still surface for non-overridden blocks.
    assert tg.attachments.voice.max_seconds == 600


def test_multiple_channels_each_resolved(tmp_path) -> None:
    """Two channels — one inherits everything, one overrides cost.
    Demonstrates that the schema is generic across adapters."""
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  cost:
    daily_max_usd: 1.50

telegram:
  enabled: true
  display_name: Telegram

whatsapp:
  enabled: true
  display_name: WhatsApp
  cost:
    daily_max_usd: 5.00
""",
    )
    cfg = load_channels_config(path)
    assert "telegram" in cfg.known_channels()
    assert "whatsapp" in cfg.known_channels()
    assert cfg.resolved("telegram").cost.daily_max_usd == 1.50
    assert cfg.resolved("whatsapp").cost.daily_max_usd == 5.00


def test_negative_cap_rejected(tmp_path) -> None:
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  attachments:
    voice: { max_seconds: -10 }
""",
    )
    with pytest.raises(RuntimeError, match="channels.yaml invalid"):
        load_channels_config(path)


def test_unknown_on_ask_rejected(tmp_path) -> None:
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  gate_policy:
    on_ask: maybe
""",
    )
    with pytest.raises(RuntimeError, match="channels.yaml invalid"):
        load_channels_config(path)


def test_ttl_out_of_range_rejected(tmp_path) -> None:
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  gate_policy:
    approve_next_turn_ttl_s: 30
""",
    )
    with pytest.raises(RuntimeError, match="channels.yaml invalid"):
        load_channels_config(path)


def test_ttl_too_large_rejected(tmp_path) -> None:
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  gate_policy:
    approve_next_turn_ttl_s: 999999
""",
    )
    with pytest.raises(RuntimeError, match="channels.yaml invalid"):
        load_channels_config(path)


def test_negative_per_role_cap_rejected(tmp_path) -> None:
    path = tmp_path / "channels.yaml"
    _write(
        path,
        """
defaults:
  cost:
    per_role:
      channel_vision: -0.1
""",
    )
    with pytest.raises(RuntimeError, match="channels.yaml invalid"):
        load_channels_config(path)


def test_env_override_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "alt.yaml"
    _write(path, "telegram:\n  display_name: OVERRIDE\n")
    monkeypatch.setenv("TESSERACT_CHANNELS_YAML", str(path))
    cfg = load_channels_config()
    assert cfg.telegram.display_name == "OVERRIDE"


def test_channel_block_alias_returns_resolved(tmp_path) -> None:
    """``channel_block(name)`` is the backward-compat alias for ``resolved``."""
    path = tmp_path / "channels.yaml"
    _write(path, "telegram:\n  enabled: true\n")
    cfg = load_channels_config(path)
    a = cfg.channel_block("telegram")
    b = cfg.resolved("telegram")
    assert a == b
    assert cfg.channel_block("missing") is None
    assert cfg.resolved("missing") is None


@pytest.mark.asyncio
async def test_live_reload_via_config_watcher(tmp_path, monkeypatch) -> None:
    """``reload_channels`` re-reads the typed config and refreshes
    ``app['channels_config']`` without restart."""
    from aiohttp import web

    from tesseract.integrations import clear_registry
    from tesseract.mirror.server.config_watcher import reload_channels

    path = tmp_path / "channels.yaml"
    _write(
        path,
        "telegram:\n  attachments:\n    voice: { max_seconds: 100 }\n",
    )
    monkeypatch.setenv("TESSERACT_CHANNELS_YAML", str(path))

    app = web.Application()
    app["channels_config"] = load_channels_config()
    app["server_sessions"] = {}
    app["config_reload_toasts_enabled"] = False
    clear_registry()
    assert app["channels_config"].telegram.attachments.voice.max_seconds == 100

    _write(
        path,
        "telegram:\n  attachments:\n    voice: { max_seconds: 222 }\n",
    )
    await reload_channels(app)
    assert app["channels_config"].telegram.attachments.voice.max_seconds == 222
