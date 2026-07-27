from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from tesseract.config.loader import (
    ConfigBundle,
    PROVIDERS_YAML,
    ROLES_YAML,
    ResolvedRef,
    load_config,
)
from tesseract.paths import CONFIG_DIR
from tesseract.permissions.policy import PermissionPolicy, load_permission_policy

_TESSERACT_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _TESSERACT_DIR.parent
_CONFIG_DIR = CONFIG_DIR

MIRROR_YAML = _CONFIG_DIR / "mirror.yaml"
PERMISSIONS_YAML = _CONFIG_DIR / "permissions.yaml"


@dataclass(frozen=True)
class ShellProfile:
    argv: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class TerminalServerConfig:
    default_shell: str
    max_tabs: int
    max_panes_per_tab: int
    shell_profiles: Mapping[str, ShellProfile]
    coalesce_flush_ms: float
    coalesce_flush_chars: int
    reattach_grace_s: float
    pause_buffer_cap_chars: int


@dataclass(frozen=True)
class UploadConfig:
    max_file_mb: int
    max_total_mb: int
    max_files_per_message: int
    allowed_mime_types: tuple[str, ...]


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    entity_name: str
    operator_name: str
    cors_origins: tuple[str, ...]
    models: dict[str, Any]
    permissions: PermissionPolicy
    terminal: TerminalServerConfig
    uploads: UploadConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"config {path} did not parse to a mapping")
    return raw


def load_server_config() -> ServerConfig:
    mirror = _load_yaml(MIRROR_YAML)
    server = mirror.get("server")
    if not isinstance(server, dict):
        raise RuntimeError(f"{MIRROR_YAML} missing required 'server' block")
    host = server["host"]
    port = int(server["port"])

    identity = mirror.get("identity") or {}
    entity_name = str(identity.get("name") or "").strip()
    if not entity_name:
        raise RuntimeError(f"{MIRROR_YAML} missing required 'identity.name'")
    operator_name = str(identity.get("operator_name") or "").strip() or "Operator"

    cors = mirror.get("cors") or {}
    origins = tuple(cors.get("origins") or ())

    bundle = load_config(providers_path=PROVIDERS_YAML, roles_path=ROLES_YAML)
    models = synthesize_legacy_models_dict(bundle)
    permissions = load_permission_policy(PERMISSIONS_YAML, workspace_root=str(_REPO_ROOT))
    terminal = _load_terminal_config(mirror)
    uploads = _load_upload_config(mirror)

    return ServerConfig(
        host=host,
        port=port,
        entity_name=entity_name,
        operator_name=operator_name,
        cors_origins=origins,
        models=models,
        permissions=permissions,
        terminal=terminal,
        uploads=uploads,
    )


def _ref_to_legacy_entry(ref: ResolvedRef) -> dict[str, Any]:
    """Flatten a ResolvedRef into the inline-resolution-entry shape that
    legacy code reads from `models.yaml::roles.<role>.resolution[i]`."""
    conn = ref.connection
    entry: dict[str, Any] = {
        "tier": conn.tier,
        "provider": conn.name,
        "model": ref.model.model,
    }
    fields = dict(ref.model.fields)
    for k, v in fields.items():
        entry.setdefault(k, v)
    if conn.base_url:
        entry.setdefault("base_url", conn.base_url)
    if conn.api_key_env:
        entry.setdefault("api_key_env", conn.api_key_env)
    if conn.command:
        entry.setdefault("command", conn.command)
    return entry


def _synthesize_voice_block(bundle: ConfigBundle) -> dict[str, Any]:
    voice = bundle.voice
    if voice is None:
        return {}
    out: dict[str, Any] = {
        "default_voice_id": voice.default_voice_id,
        "default_tone_prompt": voice.default_tone_prompt,
    }

    def _materialize(provider) -> dict[str, Any]:  # noqa: ANN001
        materialized = _ref_to_legacy_entry(provider.ref)
        # `provider` keyed by catalog model id matches CostLedger pricing keys
        # and the boot.load_voice_config() synthesis. Required for the
        # `_identity_cost_tracking` voice merge path.
        materialized["provider"] = provider.ref.model.id
        materialized.update(dict(provider.settings))
        if provider.daily_budget_usd:
            materialized.setdefault("daily_budget_usd", provider.daily_budget_usd)
        return materialized

    def _chain_view(chain) -> dict[str, Any]:  # noqa: ANN001
        if chain is None:
            return {}
        view: dict[str, Any] = {
            "mode": chain.mode,
            "chain": [
                {**_materialize(p), "ref": p.ref.ref}
                for p in chain.chain()
            ],
            "primary": chain.primary.ref.ref,
            "fallbacks": [p.ref.ref for p in chain.fallbacks],
        }
        return view

    out["stt"] = _chain_view(voice.stt)
    out["tts"] = _chain_view(voice.tts)
    return out


def _synthesize_voice_cost_block(bundle: ConfigBundle) -> dict[str, Any]:
    """Build the legacy `cost_tracking.voice.{tts,stt}.<provider>` view —
    unit pricing from the catalog, daily caps from the role voice chain
    entries. Provider keys = catalog model id (matches CostLedger pricing
    keys)."""
    out: dict[str, dict[str, dict[str, float]]] = {"tts": {}, "stt": {}}
    if bundle.voice is None:
        return out
    tts_chain = bundle.voice.tts.chain() if bundle.voice.tts is not None else ()
    stt_chain = bundle.voice.stt.chain() if bundle.voice.stt is not None else ()
    for kind_in, entries, rate_key in (
        ("tts", tts_chain, "cost_per_million_chars"),
        ("stt", stt_chain, "cost_per_audio_hour"),
    ):
        for entry in entries:
            cap = float(entry.daily_budget_usd or 0)
            if cap <= 0:
                continue
            model = entry.ref.model
            rate_raw = model.fields.get(rate_key)
            if rate_raw is None:
                continue
            out[kind_in][model.id] = {
                rate_key: float(rate_raw),
                "daily_budget_usd": cap,
            }
    return out


def synthesize_legacy_models_dict(bundle: ConfigBundle) -> dict[str, Any]:
    """Project the typed loader bundle back to the legacy `models.yaml`
    dict shape, so existing consumers (settings routes, system route,
    cost ledger) keep working unchanged. Source of truth is providers.yaml +
    roles.yaml; this view is read-only and rebuilt at boot.
    """
    roles_out: dict[str, Any] = {}
    per_role_caps: dict[str, float] = {}
    for name, role in bundle.roles.items():
        # Inactive roles have no primary — emit a stub so downstream consumers
        # that iterate the dict don't crash. Anything reading the role MUST
        # check `mode == "active"` before treating the resolution as wired.
        if role.primary is None:
            roles_out[name] = {"mode": role.mode, "resolution": [], "notes": role.notes}
            continue
        primary_entry = _ref_to_legacy_entry(role.primary)
        # Layer role-level overrides onto the primary entry so legacy
        # consumers reading `resolution[0].reasoning_effort` still see them.
        for ov_key, ov_val in role.overrides.items():
            if ov_key.endswith("_override"):
                primary_entry[ov_key.removesuffix("_override")] = ov_val
        # Surface role-level knobs on resolution[0] too so legacy `system.py`
        # reads land on the active primary's view.
        for k in (
            "compact_threshold",
            "keep_recent_turns",
            "tool_iteration_cap",
            "consecutive_error_cap",
        ):
            if k in role.overrides:
                primary_entry[k] = role.overrides[k]
        resolution = [primary_entry] + [_ref_to_legacy_entry(r) for r in role.fallbacks]
        role_dict: dict[str, Any] = {
            "mode": role.mode,
            "resolution": resolution,
        }
        for k in (
            "compact_threshold",
            "keep_recent_turns",
            "tool_iteration_cap",
            "consecutive_error_cap",
        ):
            if k in role.overrides:
                role_dict[k] = role.overrides[k]
        cap = role.overrides.get("daily_budget_usd")
        if cap is not None:
            try:
                per_role_caps[name] = float(cap)
            except (TypeError, ValueError):
                pass
        if role.notes:
            role_dict["notes"] = role.notes
        roles_out[name] = role_dict

    ct_raw = dict(bundle.cost_tracking)
    cost_tracking = {
        "enabled": bool(ct_raw.get("enabled", False)),
        "warning_at_pct": float(ct_raw.get("warning_at_pct", 0.75)),
        "log_file": str(ct_raw.get("log_file", "logs/cost-tracking.jsonl")),
        "per_role": per_role_caps,
        "voice": _synthesize_voice_cost_block(bundle),
    }

    embeddings_entry = _ref_to_legacy_entry(bundle.embeddings)

    return {
        "roles": roles_out,
        "voice": _synthesize_voice_block(bundle),
        "embeddings": embeddings_entry,
        "cost_tracking": cost_tracking,
        "availability": dict(bundle.availability),
    }


def _load_pty_thresholds() -> dict[str, Any]:
    perms = _load_yaml(PERMISSIONS_YAML)
    block = perms.get("pty_thresholds")
    if not isinstance(block, dict):
        raise RuntimeError(f"{PERMISSIONS_YAML} missing required 'pty_thresholds' block")
    return block


def _load_terminal_config(mirror: dict[str, Any]) -> TerminalServerConfig:
    block = mirror.get("terminal")
    if not isinstance(block, dict):
        raise RuntimeError(f"{MIRROR_YAML} missing required 'terminal' block")
    try:
        default_shell = str(block["default_shell"])
        max_tabs = int(block["max_tabs"])
        max_panes_per_tab = int(block["max_panes_per_tab"])
        profiles_raw = block["shell_profiles"]
    except KeyError as exc:
        raise RuntimeError(f"{MIRROR_YAML} terminal.* missing key: {exc.args[0]}") from exc
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise RuntimeError(f"{MIRROR_YAML} terminal.shell_profiles must be a non-empty mapping")
    profiles: dict[str, ShellProfile] = {}
    for name, spec in profiles_raw.items():
        if not isinstance(spec, dict):
            raise RuntimeError(f"terminal.shell_profiles.{name} must be a mapping")
        argv = spec.get("argv")
        label = spec.get("label")
        if not isinstance(argv, list) or not argv:
            raise RuntimeError(f"terminal.shell_profiles.{name}.argv must be a non-empty list")
        if not isinstance(label, str) or not label:
            raise RuntimeError(f"terminal.shell_profiles.{name}.label must be a non-empty string")
        profiles[str(name)] = ShellProfile(argv=tuple(str(a) for a in argv), label=label)
    if default_shell not in profiles:
        raise RuntimeError(
            f"terminal.default_shell={default_shell!r} not in shell_profiles "
            f"(have {sorted(profiles)})"
        )
    thresholds = _load_pty_thresholds()
    try:
        coalesce_flush_ms = float(thresholds["coalesce_flush_ms"])
        coalesce_flush_chars = int(thresholds["coalesce_flush_chars"])
        reattach_grace_s = float(thresholds["reattach_grace_s"])
        pause_buffer_cap_chars = int(thresholds["pause_buffer_cap_chars"])
    except KeyError as exc:
        raise RuntimeError(f"{PERMISSIONS_YAML} pty_thresholds missing key: {exc.args[0]}") from exc
    return TerminalServerConfig(
        default_shell=default_shell,
        max_tabs=max_tabs,
        max_panes_per_tab=max_panes_per_tab,
        shell_profiles=MappingProxyType(profiles),
        coalesce_flush_ms=coalesce_flush_ms,
        coalesce_flush_chars=coalesce_flush_chars,
        reattach_grace_s=reattach_grace_s,
        pause_buffer_cap_chars=pause_buffer_cap_chars,
    )


def _load_upload_config(mirror: dict[str, Any]) -> UploadConfig:
    block = mirror.get("uploads")
    if not isinstance(block, dict):
        raise RuntimeError(f"{MIRROR_YAML} missing required 'uploads' block")
    try:
        chat = block["chat"]
    except KeyError as exc:
        raise RuntimeError(f"{MIRROR_YAML} uploads.chat.* missing key: {exc.args[0]}") from exc
    if not isinstance(chat, dict):
        raise RuntimeError(f"{MIRROR_YAML} uploads.chat must be a mapping")
    try:
        max_file_mb = int(chat["max_file_mb"])
        max_total_mb = int(chat["max_total_mb"])
        max_files_per_message = int(chat["max_files_per_message"])
        allowed_raw = chat["allowed_mime_types"]
    except KeyError as exc:
        raise RuntimeError(f"{MIRROR_YAML} uploads.chat.* missing key: {exc.args[0]}") from exc
    if max_file_mb <= 0:
        raise RuntimeError("uploads.chat.max_file_mb must be positive")
    if max_total_mb <= 0:
        raise RuntimeError("uploads.chat.max_total_mb must be positive")
    if max_file_mb > max_total_mb:
        raise RuntimeError("uploads.chat.max_file_mb must be <= max_total_mb")
    if max_files_per_message <= 0:
        raise RuntimeError("uploads.chat.max_files_per_message must be positive")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise RuntimeError("uploads.chat.allowed_mime_types must be a non-empty list")
    allowed = tuple(str(v).strip().lower() for v in allowed_raw if str(v).strip())
    if not allowed:
        raise RuntimeError("uploads.chat.allowed_mime_types must contain at least one MIME type")
    return UploadConfig(
        max_file_mb=max_file_mb,
        max_total_mb=max_total_mb,
        max_files_per_message=max_files_per_message,
        allowed_mime_types=allowed,
    )
