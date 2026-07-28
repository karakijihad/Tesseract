"""GET /api/capabilities — capability report, not a setup gate.

Nothing in TESSERACT requires an API key or a specific provider (CLAUDE.md:
"none of the api are requirement"). This route reports, per provider and per
chat_brain candidate, its `status` and — when not `ready` — WHY: no API key
set, disabled via `providers.yaml`'s `enabled` bools, or (cli tier) the
binary isn't on PATH. It never gates the UI (no `ready`-for-the-whole-app
flag) and never returns a secret VALUE, only key NAMES and presence
booleans.

`status` is one of three states, not a bool — collapsing to true/false lost
information (review fix-pass, Important-2): a keyless local provider
(ollama/whisper/piper) that is merely `enabled: true` in providers.yaml is
NOT the same claim as "verified working" (no binary, no model files, no
reachable server checked here — that live diagnostic already exists per-
provider in Settings -> Local Models, and a network probe does not belong
in a settings-read endpoint). So:
  - "ready": actually checked and good (key present; or, cli tier, the
    binary was found on PATH via the same `shutil.which` check
    `build_adapter` runs before actually using it).
  - "unavailable": checked and NOT good (disabled, missing key, binary not
    found) — `reason` says which.
  - "unverified": enabled and nothing cheap here can confirm or deny it
    further (local, keyless, non-cli providers only).
"""

from __future__ import annotations

import os
import shutil

from aiohttp import web

from tesseract.paths import home_dir

# Integrations that aren't a `providers.yaml` provider block but are still
# optional, key-gated features (tool availability, channel bridges). Kept
# separate from `providers` below — these don't have an `enabled` bool of
# their own, just "present or not".
_INTEGRATIONS = (
    ("web_search (Tavily)", "TAVILY_API_KEY"),
    ("web_search (Brave)", "BRAVE_SEARCH_API_KEY"),
    ("telegram_channel", "TELEGRAM_BOT_TOKEN"),
)

_RESERVED_TIER_KEYS = frozenset({"enabled"})


def _key_present(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value and value.strip())


def _cli_binary_found(command: str) -> bool:
    # Same PATH probe `tesseract/brain/boot.py::build_adapter` runs for the
    # `cli` adapter before spawning it — cheap, no subprocess, no network.
    return shutil.which(command) is not None or shutil.which(f"{command}.cmd") is not None


def _provider_rows() -> list[dict]:
    from tesseract.brain.boot import load_bundle

    providers_raw = load_bundle().providers_raw
    rows: list[dict] = []
    for tier in ("api", "cli", "local"):
        tier_block = providers_raw.get(tier) or {}
        tier_enabled = bool(tier_block.get("enabled", True))
        for name, block in tier_block.items():
            if name in _RESERVED_TIER_KEYS or not isinstance(block, dict):
                continue
            provider_enabled = bool(block.get("enabled", True))
            enabled = tier_enabled and provider_enabled
            key_name = block.get("api_key_env")
            key_present = _key_present(key_name) if key_name else None

            if not tier_enabled:
                status, reason = "unavailable", f"tier '{tier}' disabled in providers.yaml"
            elif not provider_enabled:
                status, reason = "unavailable", f"disabled in providers.yaml ({tier}.{name}.enabled=false)"
            elif key_name is not None:
                if key_present:
                    status, reason = "ready", None
                else:
                    status, reason = "unavailable", f"{key_name} not set"
            elif tier == "cli":
                command = block.get("command")
                if not command:
                    status, reason = "unavailable", "missing 'command' in providers.yaml"
                elif _cli_binary_found(command):
                    status, reason = "ready", None
                else:
                    status, reason = "unavailable", f"cli binary '{command}' not found on PATH"
            else:
                # Local, keyless, non-cli provider (ollama reachability,
                # whisper/piper model files) — enabled but not cheaply
                # verifiable here. See module docstring.
                status, reason = "unverified", "enabled — see Settings -> Local Models for live status"

            rows.append({
                "tier": tier,
                "provider": name,
                "enabled": enabled,
                "key_name": key_name,
                "key_present": key_present,
                "status": status,
                "reason": reason,
            })
    return rows


def _chat_candidates() -> tuple[list[dict], bool, str | None]:
    from tesseract.brain.boot import build_chat_brain_adapter, load_chat_brain_chain

    try:
        chain_cfgs = load_chat_brain_chain()
    except Exception as exc:  # config itself broken — report, don't crash the route
        return [], False, str(exc)

    candidates: list[dict] = []
    any_available = False
    for cfg in chain_cfgs:
        try:
            build_chat_brain_adapter(cfg)
        except RuntimeError as exc:
            candidates.append({
                "provider": cfg.provider,
                "model": cfg.model,
                "available": False,
                "reason": str(exc),
            })
        else:
            any_available = True
            candidates.append({
                "provider": cfg.provider,
                "model": cfg.model,
                "available": True,
                "reason": None,
            })
    reason = None
    if not any_available:
        reason = "no chat provider available — " + "; ".join(
            f"{c['provider']} ({c['model']}): {c['reason']}" for c in candidates
        )
    return candidates, any_available, reason


async def capabilities_status(request: web.Request) -> web.Response:
    candidates, chat_available, chat_reason = _chat_candidates()
    return web.json_response({
        "env_path": str(home_dir() / ".env"),
        "chat": {
            "available": chat_available,
            "reason": chat_reason,
            "candidates": candidates,
        },
        "providers": _provider_rows(),
        "integrations": [
            {"name": name, "key_name": key_name, "key_present": _key_present(key_name)}
            for name, key_name in _INTEGRATIONS
        ],
    })


def register(app: web.Application) -> None:
    app.router.add_get("/api/capabilities", capabilities_status)


__all__ = ["register", "capabilities_status"]
