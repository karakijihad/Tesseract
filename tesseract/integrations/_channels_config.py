"""Typed ``channels.yaml`` model.

Two-tier shape (2026-05-18 refactor):

* **Global ``defaults:``** — settings that apply to every channel
  adapter unless the channel block sparse-overrides them. Today:
  ``attachments``, ``extract``, ``cost``, ``gate_policy``.
* **Per-channel block (``telegram:``, ``whatsapp:``, …)** — fields
  intrinsic to ONE channel: ``enabled``, ``display_name``, ``brief_push``,
  ``outbound_rate``, ``muted_categories``. Any of the global blocks
  above can ALSO live here as a per-channel override (sparse — only
  the keys you list are overridden; the rest still inherit from
  ``defaults``).

Adding a new channel adapter (whatsapp, signal, discord) only requires
a new top-level block with the channel-specific fields; the global
limits are picked up automatically.

Consumers read via :meth:`ChannelsConfig.resolved` (alias:
:meth:`channel_block`) which returns a :class:`ResolvedChannel` —
the merged view with every field populated. Existing call-sites that
previously read ``cfg.telegram.attachments`` continue to work
unchanged: the ``cfg.telegram`` property routes through ``resolved("telegram")``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tesseract.paths import CONFIG_DIR

log = logging.getLogger(__name__)


_FROZEN = ConfigDict(frozen=True, extra="ignore")


# -- Shared blocks (live under ``defaults:`` AND optionally per-channel) --


class AttachmentSecondsCap(BaseModel):
    model_config = _FROZEN
    max_seconds: int = Field(ge=0)


class AttachmentBytesCap(BaseModel):
    model_config = _FROZEN
    max_bytes: int = Field(ge=0)


class AttachmentVideoCap(BaseModel):
    model_config = _FROZEN
    max_seconds: int = Field(ge=0)
    max_bytes: int = Field(ge=0)


class AttachmentCaps(BaseModel):
    model_config = _FROZEN
    voice: AttachmentSecondsCap = AttachmentSecondsCap(max_seconds=600)
    audio: AttachmentSecondsCap = AttachmentSecondsCap(max_seconds=1800)
    photo: AttachmentBytesCap = AttachmentBytesCap(max_bytes=10_485_760)
    document: AttachmentBytesCap = AttachmentBytesCap(max_bytes=26_214_400)
    video: AttachmentVideoCap = AttachmentVideoCap(max_seconds=60, max_bytes=52_428_800)


class ExtractCaps(BaseModel):
    model_config = _FROZEN
    document_chars: int = Field(default=6000, ge=0)
    image_caption_chars: int = Field(default=800, ge=0)


class CostCaps(BaseModel):
    model_config = _FROZEN
    daily_max_usd: float = Field(default=1.50, ge=0)
    per_role: dict[str, float] = Field(default_factory=dict)

    @field_validator("per_role")
    @classmethod
    def _per_role_non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        for role, cap in value.items():
            if cap < 0:
                raise ValueError(f"per_role[{role}] must be >= 0")
        return value


GatePolicyKind = Literal["workspace_nudge", "deny"]


class GatePolicy(BaseModel):
    model_config = _FROZEN
    on_ask: GatePolicyKind = "workspace_nudge"
    #: How long a gated call waits for the operator before refusing itself.
    #: The turn is parked on that wait and the bridge serialises a chat's
    #: turns, so this is also the longest the bot could stay busy on one
    #: prompt — except that a new inbound message cancels the wait, which is
    #: what keeps a long value safe. Was `approve_next_turn_ttl_s` when a tap
    #: armed a token for a later turn instead of answering the call in hand.
    decision_timeout_s: int = Field(default=1800, ge=60, le=86_400)


# -- Per-channel-only blocks (no inheritance — these are intrinsic) ---


class OutboundRate(BaseModel):
    """AU-10 — per-(category, channel) sliding-window rate cap."""

    model_config = _FROZEN
    default_per_hour: int = Field(default=6, ge=0)
    per_category: dict[str, int] = Field(default_factory=dict)


# -- Defaults + per-channel override block ----------------------------


class Defaults(BaseModel):
    """Global defaults — every channel inherits these unless overridden."""

    model_config = _FROZEN
    attachments: AttachmentCaps = AttachmentCaps()
    extract: ExtractCaps = ExtractCaps()
    cost: CostCaps = CostCaps()
    gate_policy: GatePolicy = GatePolicy()


class ChannelOverrides(BaseModel):
    """Per-channel block. Channel-specific fields PLUS optional sparse
    overrides for any global block (``attachments`` /
    ``extract`` / ``cost`` / ``gate_policy``). A ``None`` override —
    or omitting the key entirely — inherits from ``defaults``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")
    enabled: bool = True
    display_name: str = ""
    # MO-10-3 — daily-brief Telegram push (channel-specific gate).
    brief_push: bool = False
    # AU-10 — autonomous outbound notification knobs (channel-specific).
    outbound_rate: OutboundRate = OutboundRate()
    muted_categories: list[str] = Field(default_factory=list)
    # Sparse overrides — None means inherit from ``defaults``.
    attachments: AttachmentCaps | None = None
    extract: ExtractCaps | None = None
    cost: CostCaps | None = None
    gate_policy: GatePolicy | None = None


@dataclass(frozen=True)
class ResolvedChannel:
    """Merged view: per-channel overrides on top of ``defaults``. Every
    field is populated. This is the read-shape consumers should rely on."""

    name: str
    enabled: bool
    display_name: str
    brief_push: bool
    outbound_rate: OutboundRate
    muted_categories: list[str]
    attachments: AttachmentCaps
    extract: ExtractCaps
    cost: CostCaps
    gate_policy: GatePolicy


# -- Top-level config ------------------------------------------------


# Channel names known to the system today. Operator can add more
# entries to ``channels.yaml`` — anything in the YAML top-level (other
# than ``defaults`` / known meta keys) is treated as a channel block.
_RESERVED_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"defaults", "channels"})


class ChannelsConfig(BaseModel):
    """Typed image of ``tesseract/config/channels.yaml``.

    Resolved access via :meth:`resolved` (or its alias
    :meth:`channel_block`) is the public read API. Direct attribute
    access (``cfg.telegram`` / ``cfg.whatsapp``) is preserved as a
    backward-compat shorthand and routes through :meth:`resolved`.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    defaults: Defaults = Defaults()
    channels: dict[str, ChannelOverrides] = Field(default_factory=dict)

    @classmethod
    def defaults_payload(cls) -> "ChannelsConfig":
        return cls()

    def known_channels(self) -> list[str]:
        return sorted(self.channels.keys())

    def resolved(self, name: str) -> ResolvedChannel | None:
        """Merged config for ``name`` — overrides on top of defaults.

        Returns ``None`` when the channel block is absent. Callers that
        want a fallback even for unknown channels can use
        ``cfg.resolved(name) or cfg.defaults_only(name)``.
        """
        override = self.channels.get(name)
        if override is None:
            return None
        return ResolvedChannel(
            name=name,
            enabled=override.enabled,
            display_name=override.display_name or name.title(),
            brief_push=override.brief_push,
            outbound_rate=override.outbound_rate,
            muted_categories=list(override.muted_categories),
            attachments=override.attachments or self.defaults.attachments,
            extract=override.extract or self.defaults.extract,
            cost=override.cost or self.defaults.cost,
            gate_policy=override.gate_policy or self.defaults.gate_policy,
        )

    def defaults_only(self, name: str) -> ResolvedChannel:
        """Synthetic resolved view for a channel name not in the YAML.
        Returns a record with every global default + minimal channel-
        specific fields zeroed (``enabled=False``, empty mute list).
        Useful for adapters that boot before their YAML block lands."""
        return ResolvedChannel(
            name=name,
            enabled=False,
            display_name=name.title(),
            brief_push=False,
            outbound_rate=OutboundRate(),
            muted_categories=[],
            attachments=self.defaults.attachments,
            extract=self.defaults.extract,
            cost=self.defaults.cost,
            gate_policy=self.defaults.gate_policy,
        )

    def channel_block(self, name: str) -> ResolvedChannel | None:
        """Backward-compat alias for :meth:`resolved`."""
        return self.resolved(name)

    def __getattr__(self, item: str) -> Any:
        """``cfg.telegram`` / ``cfg.whatsapp`` etc. — routes through
        :meth:`resolved`. Returns a ``defaults_only`` synthetic view
        when the channel block is missing so callers that read
        ``cfg.<channel>.attachments`` etc. never face an AttributeError.
        """
        # Pydantic v2 dunder + model-field lookups must fall through to
        # the framework's default __getattr__. Only intercept names that
        # look like a YAML channel block (no leading underscore, not a
        # declared model field).
        if item.startswith("_") or item in self.__class__.model_fields:
            raise AttributeError(item)
        return self.resolved(item) or self.defaults_only(item)


# -- Loader ---------------------------------------------------------


def channel_key_env(channel: str, default: str = "") -> str:
    """The env var name `channels.yaml::<channel>.api_key_env` declares.

    The typed model above ignores unknown keys, so this reads the raw file:
    the declaration exists for the capability report and the first-run form,
    and the runtime that actually reads the credential must resolve it from
    the same place or the file is describing something it does not control.

    Falls back to `default` when the block or the key is absent, so a config
    predating the declaration still starts the channel.
    """
    try:
        raw = yaml.safe_load(_channels_yaml_path().read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return default
    block = raw.get(channel)
    if not isinstance(block, dict):
        return default
    return str(block.get("api_key_env") or default)


def _channels_yaml_path() -> Path:
    override = os.environ.get("TESSERACT_CHANNELS_YAML")
    if override:
        return Path(override)
    return CONFIG_DIR / "channels.yaml"


def _split_top_level(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the YAML root into ``defaults`` block + per-channel blocks.

    Any top-level key that is not ``defaults`` (or the historical
    ``channels`` legacy compat block, dropped in this refactor) is
    treated as a channel name. Channel dicts of arbitrary names are
    fine — adding a ``whatsapp:`` block tomorrow works without code.
    """
    defaults_block = raw.get("defaults") or {}
    if not isinstance(defaults_block, dict):
        defaults_block = {}
    channels: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _RESERVED_TOP_LEVEL_KEYS:
            continue
        if isinstance(value, dict):
            channels[key] = value
    return defaults_block, channels


def load_channels_config(path: Path | None = None) -> ChannelsConfig:
    """Read ``channels.yaml`` into a typed :class:`ChannelsConfig`.

    Missing file → built-in defaults. Malformed YAML / schema violations
    raise ``RuntimeError`` so boot fails loudly rather than running on
    a silent infrastructure default."""
    target = path or _channels_yaml_path()
    if not target.exists():
        log.info("channels.yaml not found at %s — using built-in defaults", target)
        return ChannelsConfig()
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"channels.yaml invalid: read/parse failed ({exc})") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("channels.yaml invalid: top-level node must be a mapping")
    defaults_block, channel_blocks = _split_top_level(raw)
    try:
        return ChannelsConfig.model_validate(
            {"defaults": defaults_block, "channels": channel_blocks}
        )
    except ValidationError as exc:
        raise RuntimeError(f"channels.yaml invalid: {exc}") from exc


__all__ = [
    "AttachmentBytesCap",
    "AttachmentCaps",
    "AttachmentSecondsCap",
    "AttachmentVideoCap",
    "ChannelOverrides",
    "ChannelsConfig",
    "CostCaps",
    "Defaults",
    "ExtractCaps",
    "GatePolicy",
    "GatePolicyKind",
    "OutboundRate",
    "ResolvedChannel",
    "RetentionBlock",
    "load_channels_config",
]
