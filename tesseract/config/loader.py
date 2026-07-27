"""Single-pass loader for `providers.yaml` + `roles.yaml`.

Replaces the ad-hoc `yaml.safe_load(<config>.read_text())` calls scattered
across `boot.py`, `cost/ledger.py`, `mirror/server/`, and friends. One read,
one resolution pass, one typed bundle.

Reference shape used in `roles.yaml` and `agents/INDEX.md`:

    <tier>.<provider>.<model_id>

e.g. ``api.openai.gpt54_mini``, ``cli.claude.opus_47``, ``local.ollama.nomic_embed``.
The loader resolves every reference up-front so a typo surfaces at boot,
not at first use. Missing required keys raise :class:`ConfigError` naming
the file + dotted path — no silent fallbacks (CLAUDE.md §Hard Rules).

Persisting edits round-trips through :func:`tesseract.lib.yaml_io.round_trip_yaml`,
preserving operator comments and key order.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import yaml

from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.paths import CONFIG_DIR

PROVIDERS_YAML = CONFIG_DIR / "providers.yaml"
ROLES_YAML = CONFIG_DIR / "roles.yaml"

_SHELL_VAR_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}$")
_REF_RE = re.compile(r"^(api|cli|local)\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)$")

ConfigFile = Literal["providers", "roles"]


class ConfigError(RuntimeError):
    """Raised when the on-disk config is malformed or references don't resolve."""


def resolve_env(value: Any) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in string values.

    Non-string values pass through unchanged. ``${VAR}`` without a default
    raises ConfigError when the env var is unset; ``${VAR:-x}`` falls back
    to the literal default.
    """
    if not isinstance(value, str):
        return value
    m = _SHELL_VAR_RE.match(value)
    if not m:
        return value
    var, default = m.group(1), m.group(2)
    env = os.environ.get(var)
    if env:
        return env
    if default is None:
        raise ConfigError(f"environment variable ${{{var}}} is unset and has no default")
    return default


def _require(d: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"missing required key '{key}' in {where}")
    return d[key]


# ── Typed views ──────────────────────────────────────────


@dataclass(frozen=True)
class ProviderConnection:
    """One ``providers.<tier>.<name>`` block — connection settings only.

    ``enabled`` reflects the per-provider boolean (``providers.<tier>.<name>.
    enabled``); ``tier_enabled`` reflects the tier-level boolean
    (``providers.<tier>.enabled``). Both default to ``true`` when the key is
    absent so existing fixtures don't break. Adapter builders and delegate
    tools consult these to short-circuit before hitting the network.
    """
    tier: str
    name: str
    adapter: str
    timeout_seconds: float
    max_retries: int
    enabled: bool = True
    tier_enabled: bool = True
    base_url: str | None = None
    api_key_env: str | None = None
    command: str | None = None          # cli tier
    stream_json_capable: bool = False   # cli tier
    # Whether this connection's API accepts OpenAI's `prompt_cache_key`
    # param. True only for genuine OpenAI; openai-COMPATIBLE providers
    # (NIM, etc.) 400 on it, so the adapter must omit it for them.
    supports_prompt_cache_key: bool = False
    # Whether this connection accepts `stream_options: {include_usage: true}`
    # on streamed chat completions (OpenAI spec; xAI/DeepSeek/vLLM-backed NIM
    # all honor it). Without it, spec-faithful providers report NO token
    # usage on streams and the cost ledger records $0. Default ON; set
    # false only for a compat endpoint that 400s on the param.
    supports_stream_usage: bool = True
    # Optional HTTP header name for cache-node routing on providers whose
    # prompt cache is automatic but node-local (xAI: `x-grok-conv-id`).
    # Without it, requests scatter across servers and the auto-cache never
    # hits (observed 128/41k cached on back-to-back grok turns, 2026-07-16).
    # The adapter sends a stable key derived from the system-prompt prefix.
    cache_routing_header: str | None = None
    # Optional per-provider override of the chain-level retry policy
    # (`providers.yaml::chain.transient_retries` / `transient_backoff_ms`).
    # `None` = inherit the global. Set per-provider when the failure shape
    # diverges — e.g. Anthropic 529 overloads can wedge for minutes, so
    # `transient_retries: 0` skips the wait and falls over immediately;
    # OpenAI 5xx blips usually clear in <1s, so `transient_retries: 3`
    # is fine.
    transient_retries: int | None = None
    transient_backoff_ms: int | None = None
    # Optional per-provider override of the chain-level cooldown breaker
    # (`providers.yaml::chain.cooldown_max_failures` / `cooldown_seconds`).
    # `None` = inherit the global. Set per-provider when one provider's
    # outages are notably longer than the chain default — e.g. raise
    # Anthropic's `cooldown_seconds` to 300 if 529s typically last ~5min.
    cooldown_max_failures: int | None = None
    cooldown_seconds: float | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderModel:
    """One named model entry under ``<provider>.models.<id>``."""
    id: str
    model: str
    kind: str                           # 'chat' (default), 'embedding', 'tts', 'stt', 'audio_stt'
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedRef:
    """A ``<tier>.<provider>.<model>`` reference fully resolved to its objects."""
    ref: str
    connection: ProviderConnection
    model: ProviderModel


@dataclass(frozen=True)
class RoleConfig:
    """One ``roles.<name>`` entry.

    `primary` is None when ``mode == "inactive"`` — the role is kept as a
    schema stub but has no provider wired. Consumers MUST gate on
    ``role.mode == "active"`` before dereferencing `primary`.
    """
    name: str
    mode: str
    primary: ResolvedRef | None
    fallbacks: tuple[ResolvedRef, ...]
    overrides: Mapping[str, Any]
    notes: str = ""


@dataclass(frozen=True)
class VoiceProvider:
    """One STT or TTS provider entry — catalog ref + per-ref settings.

    Replaces the old lane-name-keyed shape (``voice.tts.cloud``,
    ``voice.tts.elevenlabs``) with a flat list keyed by catalog ref.
    Each entry carries everything the engine needs to invoke that
    specific provider: the resolved ref (so the runtime knows which
    adapter to drive) plus a free-form ``settings`` mapping that the
    engine reads directly (``voice_id`` for TTS, ``language`` for
    Whisper, ``prompt`` for Gemini STT, etc.).
    """
    ref: ResolvedRef
    daily_budget_usd: float
    settings: Mapping[str, Any]


@dataclass(frozen=True)
class VoiceChain:
    """One side of the voice subsystem (STT or TTS) with primary +
    optional fallbacks. Mirrors ``RoleConfig`` so any operator who
    can read the chat-brain wiring can read the voice wiring."""
    mode: str
    primary: VoiceProvider
    fallbacks: tuple[VoiceProvider, ...]

    def chain(self) -> tuple[VoiceProvider, ...]:
        return (self.primary, *self.fallbacks)


@dataclass(frozen=True)
class VoiceConfig:
    default_voice_id: str
    default_tone_prompt: str
    stt: VoiceChain | None
    tts: VoiceChain | None


@dataclass(frozen=True)
class ConfigBundle:
    providers_path: Path
    roles_path: Path
    providers_raw: Mapping[str, Any]
    roles_raw: Mapping[str, Any]
    availability: Mapping[str, Any]
    cost_tracking: Mapping[str, Any]
    embeddings: ResolvedRef
    roles: Mapping[str, RoleConfig]
    voice: VoiceConfig | None

    def role(self, name: str) -> RoleConfig:
        if name not in self.roles:
            raise ConfigError(f"role '{name}' missing from roles.yaml")
        return self.roles[name]

    def resolve(self, ref: str) -> ResolvedRef:
        """Look up a ``<tier>.<provider>.<model>`` ref. Raises if invalid."""
        return _resolve_ref(ref, self.providers_raw)

    def all_models(self) -> list[tuple[str, ProviderConnection, ProviderModel]]:
        """Flatten every catalog model entry. Used by cost ledger to build
        the (model_name, in, out) price table."""
        out: list[tuple[str, ProviderConnection, ProviderModel]] = []
        for tier in ("api", "cli", "local"):
            tier_block = self.providers_raw.get(tier) or {}
            tier_on = _tier_enabled(tier_block)
            for prov_name, prov_block in tier_block.items():
                if prov_name in _TIER_RESERVED_KEYS:
                    continue
                conn = _build_connection(tier, prov_name, prov_block, tier_on)
                for model_id, _model_block in (prov_block.get("models") or {}).items():
                    ref = f"{tier}.{prov_name}.{model_id}"
                    resolved = _resolve_ref(ref, self.providers_raw)
                    out.append((ref, conn, resolved.model))
        return out

    def is_provider_enabled(self, tier: str, provider: str) -> tuple[bool, str]:
        """Report whether a tier+provider pair is enabled in providers.yaml.

        Returns ``(True, "")`` when both the tier-level and provider-level
        ``enabled`` flags are on; otherwise ``(False, "<reason>")`` for the
        caller to surface to the operator. Used by `build_adapter`, the
        delegate CLI tools, and voice-runtime materialization to
        short-circuit before any network or subprocess call.
        """
        tier_block = self.providers_raw.get(tier) or {}
        if not _tier_enabled(tier_block):
            return False, f"tier '{tier}' disabled in providers.yaml ({tier}.enabled=false)"
        prov_block = tier_block.get(provider) or {}
        if not bool(prov_block.get("enabled", True)):
            return (
                False,
                f"provider '{tier}.{provider}' disabled in providers.yaml "
                f"({tier}.{provider}.enabled=false)",
            )
        return True, ""


# ── Resolution ───────────────────────────────────────────


def _build_connection(
    tier: str,
    name: str,
    block: Mapping[str, Any],
    tier_enabled: bool,
) -> ProviderConnection:
    where = f"providers.yaml {tier}.{name}"
    return ProviderConnection(
        tier=tier,
        name=name,
        adapter=str(_require(block, "adapter", where)),
        timeout_seconds=float(_require(block, "timeout_seconds", where)),
        max_retries=int(_require(block, "max_retries", where)),
        enabled=bool(block.get("enabled", True)),
        tier_enabled=tier_enabled,
        base_url=resolve_env(block.get("base_url")) if block.get("base_url") else None,
        api_key_env=block.get("api_key_env"),
        command=block.get("command"),
        stream_json_capable=bool(block.get("stream_json_capable", False)),
        supports_prompt_cache_key=bool(block.get("supports_prompt_cache_key", False)),
        supports_stream_usage=bool(block.get("supports_stream_usage", True)),
        cache_routing_header=block.get("cache_routing_header"),
        transient_retries=(
            int(block["transient_retries"]) if "transient_retries" in block else None
        ),
        transient_backoff_ms=(
            int(block["transient_backoff_ms"]) if "transient_backoff_ms" in block else None
        ),
        cooldown_max_failures=(
            int(block["cooldown_max_failures"]) if "cooldown_max_failures" in block else None
        ),
        cooldown_seconds=(
            float(block["cooldown_seconds"]) if "cooldown_seconds" in block else None
        ),
        extra={k: v for k, v in block.items() if k not in (
            "adapter", "timeout_seconds", "max_retries", "base_url",
            "api_key_env", "command", "stream_json_capable", "models", "enabled",
            "transient_retries", "transient_backoff_ms",
            "cooldown_max_failures", "cooldown_seconds", "supports_prompt_cache_key",
            "supports_stream_usage", "cache_routing_header",
        )},
    )


def _build_model(tier: str, prov_name: str, model_id: str, block: Mapping[str, Any]) -> ProviderModel:
    where = f"providers.yaml {tier}.{prov_name}.models.{model_id}"
    model_name = str(_require(block, "model", where))
    kind = str(block.get("kind", "chat"))
    fields = {k: v for k, v in block.items() if k not in ("model", "kind")}
    return ProviderModel(id=model_id, model=model_name, kind=kind, fields=fields)


_TIER_RESERVED_KEYS = frozenset({"enabled"})


def _tier_enabled(tier_block: Mapping[str, Any]) -> bool:
    return bool(tier_block.get("enabled", True))


def _resolve_ref(ref: str, providers_raw: Mapping[str, Any]) -> ResolvedRef:
    if not isinstance(ref, str):
        raise ConfigError(f"reference must be a string, got {type(ref).__name__}: {ref!r}")
    m = _REF_RE.match(ref)
    if not m:
        raise ConfigError(
            f"reference '{ref}' must match shape <tier>.<provider>.<model> "
            f"with tier in (api, cli, local) and lowercase identifiers"
        )
    tier, prov_name, model_id = m.group(1), m.group(2), m.group(3)
    tier_block = providers_raw.get(tier)
    if not tier_block or prov_name not in tier_block or prov_name in _TIER_RESERVED_KEYS:
        raise ConfigError(f"provider '{tier}.{prov_name}' missing from providers.yaml")
    prov_block = tier_block[prov_name]
    models = prov_block.get("models") or {}
    if model_id not in models:
        raise ConfigError(f"model '{model_id}' missing from providers.yaml {tier}.{prov_name}.models")
    return ResolvedRef(
        ref=ref,
        connection=_build_connection(tier, prov_name, prov_block, _tier_enabled(tier_block)),
        model=_build_model(tier, prov_name, model_id, models[model_id]),
    )


def _build_role(name: str, block: Mapping[str, Any], providers_raw: Mapping[str, Any]) -> RoleConfig:
    where = f"roles.yaml roles.{name}"
    mode = str(block.get("mode", "active"))
    # Inactive roles are kept as schema stubs — primary/fallbacks may be
    # blank or reference a removed provider. Skip resolution; consumers
    # MUST gate on `role.mode == "active"` before reading `role.primary`.
    if mode == "inactive":
        primary = None
        fallbacks: tuple[ResolvedRef, ...] = ()
    else:
        primary_ref = str(_require(block, "primary", where))
        primary = _resolve_ref(primary_ref, providers_raw)
        fallbacks = tuple(
            _resolve_ref(str(r), providers_raw)
            for r in (block.get("fallbacks") or [])
        )
    overrides = {
        k: v for k, v in block.items()
        if k not in ("mode", "primary", "fallbacks", "notes")
    }
    return RoleConfig(
        name=name,
        mode=mode,
        primary=primary,
        fallbacks=fallbacks,
        overrides=overrides,
        notes=str(block.get("notes", "") or ""),
    )


def _build_voice_provider(
    ref_str: str,
    settings_for_ref: Mapping[str, Any],
    providers_raw: Mapping[str, Any],
    where: str,
) -> VoiceProvider:
    """Resolve one voice provider entry: catalog ref + the settings
    block keyed under that ref. ``daily_budget_usd`` is hoisted out
    of ``settings`` since the engine + cost ledger read it directly,
    everything else is free-form passthrough the engine consumes."""
    ref = _resolve_ref(ref_str, providers_raw)
    settings = {k: v for k, v in settings_for_ref.items() if k != "daily_budget_usd"}
    return VoiceProvider(
        ref=ref,
        daily_budget_usd=float(settings_for_ref.get("daily_budget_usd", 0.0) or 0.0),
        settings=settings,
    )


def _build_voice_chain(
    lane: str, block: Mapping[str, Any], providers_raw: Mapping[str, Any],
) -> VoiceChain | None:
    """Parse one ``voice.<stt|tts>`` block in the new
    primary+fallbacks+settings shape. Returns None when the block is
    empty/missing — voice-disabled deployments stay valid."""
    if not block:
        return None
    where = f"roles.yaml voice.{lane}"
    primary_ref = _require(block, "primary", where)
    if not isinstance(primary_ref, str) or not primary_ref:
        raise ConfigError(f"{where}.primary must be a catalog ref string")
    settings_block = block.get("settings") or {}
    if not isinstance(settings_block, dict):
        raise ConfigError(f"{where}.settings must be a mapping keyed by catalog ref")

    fallback_refs: list[str] = []
    for raw in (block.get("fallbacks") or []):
        if not isinstance(raw, str) or not raw:
            raise ConfigError(f"{where}.fallbacks entries must be catalog ref strings")
        fallback_refs.append(raw)

    primary = _build_voice_provider(
        primary_ref,
        dict(settings_block.get(primary_ref) or {}),
        providers_raw,
        where,
    )
    fallbacks = tuple(
        _build_voice_provider(
            ref,
            dict(settings_block.get(ref) or {}),
            providers_raw,
            where,
        )
        for ref in fallback_refs
    )
    return VoiceChain(
        mode=str(block.get("mode", "active")),
        primary=primary,
        fallbacks=fallbacks,
    )


def _build_voice(block: Mapping[str, Any] | None, providers_raw: Mapping[str, Any]) -> VoiceConfig | None:
    if not block:
        return None
    stt_chain = _build_voice_chain("stt", block.get("stt") or {}, providers_raw)
    tts_chain = _build_voice_chain("tts", block.get("tts") or {}, providers_raw)
    return VoiceConfig(
        default_voice_id=str(block.get("default_voice_id", "")),
        default_tone_prompt=str(block.get("default_tone_prompt", "")),
        stt=stt_chain,
        tts=tts_chain,
    )


# ── Public entry point ───────────────────────────────────


def load_config(
    providers_path: Path | None = None,
    roles_path: Path | None = None,
) -> ConfigBundle:
    """Read both files, resolve every reference, return the typed bundle.

    Missing files raise :class:`ConfigError` naming the path. Broken refs,
    missing required keys, and bad shape all raise at this point — not
    later when something tries to use the value.
    """
    pp = providers_path or PROVIDERS_YAML
    rp = roles_path or ROLES_YAML
    if not pp.exists():
        raise ConfigError(f"providers.yaml missing at {pp}")
    if not rp.exists():
        raise ConfigError(f"roles.yaml missing at {rp}")

    providers_raw = yaml.safe_load(pp.read_text(encoding="utf-8")) or {}
    roles_raw = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}

    embeddings_block = roles_raw.get("embeddings") or {}
    embeddings_ref = str(_require(embeddings_block, "primary", "roles.yaml embeddings"))
    embeddings_resolved = _resolve_ref(embeddings_ref, providers_raw)

    roles_block = roles_raw.get("roles") or {}
    if not roles_block:
        raise ConfigError("roles.yaml has no `roles:` section")
    roles = {
        name: _build_role(name, block, providers_raw)
        for name, block in roles_block.items()
    }

    voice = _build_voice(roles_raw.get("voice"), providers_raw)

    return ConfigBundle(
        providers_path=pp,
        roles_path=rp,
        providers_raw=providers_raw,
        roles_raw=roles_raw,
        availability=dict(providers_raw.get("availability") or {}),
        cost_tracking=dict(providers_raw.get("cost_tracking") or {}),
        embeddings=embeddings_resolved,
        roles=roles,
        voice=voice,
    )


# ── Persist ──────────────────────────────────────────────


def _set_path(doc: Any, dotted: str, value: Any) -> None:
    """Walk `dotted` (e.g. 'roles.chat_brain.compact_threshold') into `doc`
    and assign `value` at the leaf. Missing intermediate keys raise — we
    don't auto-create config nodes (would mask typos)."""
    parts = dotted.split(".")
    cursor = doc
    for key in parts[:-1]:
        if not isinstance(cursor, Mapping) or key not in cursor:
            raise ConfigError(f"path '{dotted}' has no node at '{key}'")
        cursor = cursor[key]
    if not isinstance(cursor, Mapping):
        raise ConfigError(f"path '{dotted}' parent is not a mapping")
    cursor[parts[-1]] = value


def persist(
    file: ConfigFile,
    dotted_path: str,
    value: Any,
    *,
    providers_path: Path | None = None,
    roles_path: Path | None = None,
) -> Any:
    """Round-trip-edit one leaf in providers.yaml or roles.yaml.

    Comments and key order are preserved (ruamel). Returns the mutated doc
    so callers can re-parse the affected sub-tree if they need to.
    """
    target = (providers_path or PROVIDERS_YAML) if file == "providers" else (roles_path or ROLES_YAML)
    if not target.exists():
        raise ConfigError(f"{file}.yaml missing at {target}")

    def mutate(doc: Any) -> None:
        _set_path(doc, dotted_path, value)

    return round_trip_yaml(target, mutate)


def persist_many(
    file: ConfigFile,
    edits: list[tuple[str, Any]],
    *,
    providers_path: Path | None = None,
    roles_path: Path | None = None,
) -> Any:
    """Apply multiple edits to a single file in one round-trip.

    Use when several leaves change together (e.g. swapping primary +
    fallbacks at once) — avoids re-parsing/re-writing the file N times.
    """
    target = (providers_path or PROVIDERS_YAML) if file == "providers" else (roles_path or ROLES_YAML)
    if not target.exists():
        raise ConfigError(f"{file}.yaml missing at {target}")

    def mutate(doc: Any) -> None:
        for dotted, value in edits:
            _set_path(doc, dotted, value)

    return round_trip_yaml(target, mutate)
