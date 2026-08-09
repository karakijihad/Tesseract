"""GET /api/capabilities — capability report, not a setup gate.

Nothing in TESSERACT requires an API key or a specific provider (CLAUDE.md:
"none of the api are requirement"). This route reports, per provider and per
chat_brain candidate, its `status` and — when not `ready` — WHY: no API key
set, disabled via `providers.yaml`'s `enabled` bools, or (cli tier) the
binary isn't on PATH or isn't signed in. It never gates the UI (no
`ready`-for-the-whole-app flag) and never returns a secret VALUE, only key
NAMES and presence booleans.

`status` is one of three states, not a bool — collapsing to true/false lost
information (review fix-pass, Important-2): a keyless local provider
(ollama/whisper/piper) that is merely `enabled: true` in providers.yaml is
NOT the same claim as "verified working" (no binary, no model files, no
reachable server checked here — that live diagnostic already exists per-
provider in Settings -> Local Models, and a network probe does not belong
in a settings-read endpoint). So:
  - "ready": actually checked and good (key present; or, cli tier, the
    binary was found on PATH AND the cached auth probe says signed in —
    see `tesseract/brain/cli_auth.py` and `Docs/Plan/cli-auth/DESIGN.md`).
  - "unavailable": checked and NOT good (disabled, missing key, binary not
    found, or cli tier signed out / probe failed) — `reason` says which.
  - "unverified": enabled and nothing cheap here can confirm or deny it
    further — local keyless providers, or a cli provider whose boot auth
    probe hasn't landed in the cache yet.

`roles` extends the report (cli-auth DESIGN.md §4/§5): per `roles.yaml`
role, whether it is `broken` — its `primary` resolves to an unauthenticated
`cli` provider and no `fallback` resolves to something usable. Scoped
strictly to cli auth: an api-tier primary is never reported broken by this
field, even if its key is missing (that's the existing `chat` section's
concern for chat_brain specifically).

`POST /api/capabilities/reverify` forces a fresh cli-auth probe (invalidate
+ refresh) and returns the same report shape.

`notice_dismissed` reflects whether the operator dismissed the frontend's
first-run notice (DESIGN.md §5) — persisted via `POST /api/capabilities/
dismiss` as a marker under `<TESSERACT_HOME>/runtime/`, so it survives a
restart. It is independent of `roles`: the frontend self-suppresses the
notice whenever no role is broken, regardless of this flag.

`POST /api/capabilities/provider-enabled` writes the `enabled` bools this
report reads — the tier switch (`<tier>.enabled`) or one provider's
(`<tier>.<provider>.enabled`). It does not predict what a toggle breaks:
`boot.py::build_adapter` already raises a message naming the exact flag
that is false, and fallback chains already skip a disabled ref the way
they skip a missing key. A pre-flight simulator would be a second source
of truth for resolution that can drift from the resolver itself.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from tesseract.lib.yaml_io import atomic_write_text, round_trip_yaml
from tesseract.paths import config_dir, home_dir, runtime_dir

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

# The three tier blocks in providers.yaml. Everything else at the top level
# (`chain`, `cost_tracking`, `availability`) is not a tier and carries no
# provider blocks — hence an explicit tuple rather than "every top-level
# mapping". Providers *within* a tier are discovered, never listed.
_TIERS = ("api", "cli", "local")


def _key_present(name: str) -> bool:
    value = os.environ.get(name)
    return bool(value and value.strip())


def _cli_binary_found(command: str) -> bool:
    # Same PATH probe `tesseract/brain/boot.py::build_adapter` runs for the
    # `cli` adapter before spawning it — cheap, no subprocess, no network.
    return shutil.which(command) is not None or shutil.which(f"{command}.cmd") is not None


def _cli_auth_status(command: str, provider_name: str) -> tuple[str, str | None, dict | None]:
    """(status, reason, auth_detail) for one `cli`-tier provider row.

    Layers the process-wide auth cache (`tesseract/brain/cli_auth.py`) on
    top of the existing PATH check — see DESIGN.md §2's state table.
    `auth_detail` carries the richer per-provider detail (login_hint,
    checked_at) for the frontend; `None` when there's nothing cached yet.
    """
    from tesseract.brain import cli_auth

    if not command:
        return "unavailable", "missing 'command' in providers.yaml", None
    if not _cli_binary_found(command):
        return "unavailable", f"cli binary '{command}' not found on PATH", None

    state = cli_auth.get(provider_name)
    if state is None:
        return "unverified", "signed-in status not yet verified", None

    auth_detail = {
        "status": state.status,
        "reason": state.reason,
        "login_hint": state.login_hint,
        "checked_at": state.checked_at,
    }
    if state.status == "ready":
        return "ready", None, auth_detail
    return "unavailable", state.reason, auth_detail


def _provider_rows() -> list[dict]:
    from tesseract.brain.boot import load_bundle

    providers_raw = load_bundle().providers_raw
    rows: list[dict] = []
    for tier in _TIERS:
        tier_block = providers_raw.get(tier) or {}
        tier_enabled = bool(tier_block.get("enabled", True))
        for name, block in tier_block.items():
            if name in _RESERVED_TIER_KEYS or not isinstance(block, dict):
                continue
            provider_enabled = bool(block.get("enabled", True))
            enabled = tier_enabled and provider_enabled
            key_name = block.get("api_key_env")
            key_present = _key_present(key_name) if key_name else None
            auth_detail: dict | None = None

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
                status, reason, auth_detail = _cli_auth_status(block.get("command"), name)
            else:
                # Local, keyless, non-cli provider (ollama reachability,
                # whisper/piper model files) — enabled but not cheaply
                # verifiable here. See module docstring.
                status, reason = "unverified", "enabled — see Settings -> Local Models for live status"

            rows.append({
                "tier": tier,
                "provider": name,
                "enabled": enabled,
                # `enabled` above is the AND. The two flags are also reported
                # separately because the toggles write them separately: a
                # provider keeps its own `true` while its tier is off, and
                # turning the tier back on must restore exactly that.
                "tier_enabled": tier_enabled,
                "provider_enabled": provider_enabled,
                "key_name": key_name,
                "key_present": key_present,
                "status": status,
                "reason": reason,
                "auth": auth_detail,
            })
    return rows


def _ref_covers(ref) -> bool:
    """Whether a role's primary/fallback ref is usable enough to keep the
    role off the `broken` list (DESIGN.md §4).

    Scoped strictly to cli auth: a non-cli ref counts as covering the role
    whenever its tier + provider are enabled — general per-tier readiness
    (missing api key, etc.) is a separate, pre-existing concern (the `chat`
    section, `providers` rows above) that this field does not re-litigate.
    A cli ref only counts when the auth cache says `ready`.
    """
    conn = ref.connection
    if not conn.tier_enabled or not conn.enabled:
        return False
    if conn.tier != "cli":
        return True
    from tesseract.brain import cli_auth

    state = cli_auth.get(conn.name)
    return state is not None and state.status == "ready"


def _evaluate_role(role) -> dict:
    base = {"role": role.name, "broken": False, "reason": None, "login_hint": None}
    if role.primary is None or role.primary.connection.tier != "cli":
        return base
    if _ref_covers(role.primary) or any(_ref_covers(fb) for fb in role.fallbacks):
        return base

    from tesseract.brain import cli_auth

    conn = role.primary.connection
    state = cli_auth.get(conn.name)
    if state is None:
        # Boot's cli_auth refresh is fire-and-forget (app.py _init_background)
        # — a request landing before the probe lands must not read as
        # "broken". "Not yet verified" is the `unverified` state, not
        # `unavailable` (DESIGN.md §2); only a completed probe that came
        # back signed-out/failed marks the role broken.
        return base
    return {
        "role": role.name,
        "broken": True,
        "reason": state.reason,
        "login_hint": state.login_hint or (
            conn.auth_check.login_hint if conn.auth_check is not None else None
        ),
    }


def _dismissal_marker_path():
    """`<TESSERACT_HOME>/runtime/cli_auth_notice_dismissed.json` — call-time
    resolution (never a module-level constant), matching the idiom in
    `scheduler/alarms.py::alarms_state_path`: an app update that replaces the
    code tree must not touch operator state living under TESSERACT_HOME.
    """
    return runtime_dir() / "cli_auth_notice_dismissed.json"


def _notice_dismissed() -> bool:
    return _dismissal_marker_path().is_file()


def _role_rows() -> list[dict]:
    from tesseract.brain.boot import load_bundle

    bundle = load_bundle()
    return [_evaluate_role(role) for role in bundle.roles.values()]


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


def _build_report() -> dict:
    candidates, chat_available, chat_reason = _chat_candidates()
    return {
        "env_path": str(home_dir() / ".env"),
        "chat": {
            "available": chat_available,
            "reason": chat_reason,
            "candidates": candidates,
        },
        "providers": _provider_rows(),
        "roles": _role_rows(),
        "notice_dismissed": _notice_dismissed(),
        "integrations": [
            {"name": name, "key_name": key_name, "key_present": _key_present(key_name)}
            for name, key_name in _INTEGRATIONS
        ],
    }


async def capabilities_status(request: web.Request) -> web.Response:
    return web.json_response(_build_report())


async def capabilities_reverify(request: web.Request) -> web.Response:
    """Force a fresh cli-auth probe (DESIGN.md §3/§5), then return the same
    report shape as GET /api/capabilities so the caller doesn't need a
    follow-up GET."""
    from tesseract.brain import cli_auth

    cli_auth.invalidate()
    await cli_auth.refresh()
    return web.json_response(_build_report())


async def capabilities_dismiss(request: web.Request) -> web.Response:
    """POST /api/capabilities/dismiss — persist the first-run notice's
    dismissal under <TESSERACT_HOME>/runtime (DESIGN.md §5). Returns the
    same report shape so the caller doesn't need a follow-up GET."""
    atomic_write_text(
        _dismissal_marker_path(),
        json.dumps({"dismissed_at": datetime.now(timezone.utc).isoformat()}),
    )
    return web.json_response(_build_report())


def _providers_yaml_path():
    """Resolved at call time via `config_dir()` — the same resolution
    `load_bundle()` reads through, so a write and the report that follows it
    can never address different files (and tests can point both at a fixture
    tree with `monkeypatch.setenv("TESSERACT_HOME", ...)`).
    """
    return config_dir() / "providers.yaml"


def _set_enabled_flag(block: Any, enabled: bool) -> None:
    """Write `enabled` into a tier or provider block.

    Blocks that never carried the key explicitly (the readers all default it
    to True) get it inserted at the top, where every hand-written block in
    providers.yaml already keeps it, rather than appended below the models.
    """
    if "enabled" in block:
        block["enabled"] = enabled
    elif hasattr(block, "insert"):  # ruamel CommentedMap
        block.insert(0, "enabled", enabled)
    else:
        block["enabled"] = enabled


async def capabilities_set_provider_enabled(request: web.Request) -> web.Response:
    """POST /api/capabilities/provider-enabled — flip a tier or provider switch.

    Body: `{tier, provider, enabled}`. `provider: null` targets the tier
    switch itself. Returns the same report shape as GET /api/capabilities,
    which `load_bundle()` re-reads from disk, so the response already
    reflects the write. The config watcher rebuilds adapters and the voice
    runtime off the same file change — no restart.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    tier = body.get("tier")
    provider = body.get("provider")
    enabled = body.get("enabled")

    if tier not in _TIERS:
        return web.json_response(
            {"error": f"tier must be one of {', '.join(_TIERS)}"}, status=400
        )
    if not isinstance(enabled, bool):
        return web.json_response({"error": "enabled must be a boolean"}, status=400)
    if provider is not None and not isinstance(provider, str):
        return web.json_response(
            {"error": "provider must be a string or null"}, status=400
        )
    if provider in _RESERVED_TIER_KEYS:
        return web.json_response(
            {"error": f"'{provider}' is a reserved key, not a provider"}, status=400
        )

    path = _providers_yaml_path()
    if not path.is_file():
        return web.json_response({"error": f"{path} not found"}, status=500)

    def _apply(doc: Any) -> None:
        tier_block = doc.get(tier)
        if not isinstance(tier_block, dict):
            raise KeyError(tier)
        if provider is None:
            _set_enabled_flag(tier_block, enabled)
            return
        block = tier_block.get(provider)
        if not isinstance(block, dict):
            raise KeyError(f"{tier}.{provider}")
        _set_enabled_flag(block, enabled)

    try:
        round_trip_yaml(path, _apply)
    except KeyError as exc:
        return web.json_response(
            {"error": f"providers.yaml has no {exc}"}, status=404
        )

    return web.json_response(_build_report())


def register(app: web.Application) -> None:
    app.router.add_get("/api/capabilities", capabilities_status)
    app.router.add_post("/api/capabilities/reverify", capabilities_reverify)
    app.router.add_post("/api/capabilities/dismiss", capabilities_dismiss)
    app.router.add_post(
        "/api/capabilities/provider-enabled", capabilities_set_provider_enabled
    )


__all__ = [
    "register",
    "capabilities_status",
    "capabilities_reverify",
    "capabilities_dismiss",
    "capabilities_set_provider_enabled",
]
