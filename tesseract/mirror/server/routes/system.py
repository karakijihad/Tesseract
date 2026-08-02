from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

import tesseract
from tesseract.mirror.server.envelope import make_envelope
from tesseract.permissions.policy import DEFAULT_POSTURE

log = logging.getLogger(__name__)

from tesseract.paths import log_dir, workspace_dir


def breakers_dir() -> Path:
    """Circuit breakers are machine ops, so they live under `runtime/logs`.
    Resolved at call time — an import-time constant freezes the path before
    a relocated home is known."""
    return log_dir("circuit-breakers")


def soul_path() -> Path:
    """SOUL.md under the operator's workspace, resolved at call time so an
    app update replacing the code tree never touches it."""
    return workspace_dir() / "SOUL.md"


async def soul(request: web.Request) -> web.Response:
    path = soul_path()
    if not path.exists():
        return web.json_response({"content": "", "last_reflected_at": None})
    last = request.app.get("last_reflected_at")
    if last is None:
        mtime = path.stat().st_mtime
        last = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return web.json_response({
        "content": path.read_text(encoding="utf-8"),
        "last_reflected_at": last,
    })


async def terminal_config(request: web.Request) -> web.Response:
    cfg = request.app["config"].terminal
    return web.json_response({
        "terminal": {
            "default_shell": cfg.default_shell,
            "max_tabs": cfg.max_tabs,
            "max_panes_per_tab": cfg.max_panes_per_tab,
            "shell_profiles": {
                name: {"argv": list(p.argv), "label": p.label}
                for name, p in cfg.shell_profiles.items()
            },
        },
    })


async def breakers(request: web.Request) -> web.Response:
    out: list[dict] = []
    breakers = breakers_dir()
    if breakers.exists():
        for log_file in sorted(breakers.glob("*.jsonl")):
            out.append(_project_breaker(log_file))
    return web.json_response({"breakers": out})


def _project_breaker(log_file: Path) -> dict:
    events: list[dict] = []
    with log_file.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    last_reset_idx = _last_index(events, "reset")
    failures_since_reset = sum(
        1 for e in events[last_reset_idx + 1 :] if e.get("event") == "tripped"
    )
    last_trip = next((e for e in reversed(events) if e.get("event") == "tripped"), None)
    last_reset = next((e for e in reversed(events) if e.get("event") == "reset"), None)
    is_tripped = bool(events and events[-1].get("event") == "tripped")
    return {
        "name": log_file.stem,
        "state": "open" if is_tripped else "closed",
        "failure_count": failures_since_reset,
        "last_failure": last_trip.get("timestamp") if last_trip else None,
        "last_reset": last_reset.get("timestamp") if last_reset else None,
    }


def _last_index(events: list[dict], event_type: str) -> int:
    for i in range(len(events) - 1, -1, -1):
        if events[i].get("event") == event_type:
            return i
    return -1


_ROLE_KEYS = ("chat_brain", "claude_cli", "codex_cli", "observer_agent")


async def identity(request: web.Request) -> web.Response:
    config = request.app["config"]
    roles = config.models["roles"]
    chat_brain = roles["chat_brain"]["resolution"][0]
    observer_role = roles.get("observer_agent")
    observer_head = observer_role["resolution"][0] if observer_role else None

    models_summary: dict[str, dict] = {}
    roles_summary: dict[str, dict] = {}
    for key in _ROLE_KEYS:
        role_cfg = roles.get(key)
        if not role_cfg:
            continue
        head = role_cfg["resolution"][0]
        resolution_summary = [
            {
                "model": entry.get("model", ""),
                "provider": entry.get("provider", ""),
                "context_window": int(entry.get("context_window", 0)),
            }
            for entry in role_cfg["resolution"]
        ]
        models_summary[key] = {
            "name": head.get("model", ""),
            "provider": head.get("provider", ""),
            "context_window": int(head.get("context_window", 0)),
            "resolution": resolution_summary,
        }
        roles_summary[key] = {"mode": role_cfg.get("mode", "")}

    live_chat_ratio = _live_chat_attr(request.app, "compact_threshold", chat_brain.get("compact_threshold"), float)
    live_chat_keep = _live_chat_attr(request.app, "keep_recent_turns", chat_brain.get("keep_recent_turns"), int)
    chat_window = int(chat_brain.get("context_window", 0))
    compact_thresholds = {
        "chat_brain": {
            "ratio": live_chat_ratio,
            "context_window": chat_window,
            "tokens": int(round(live_chat_ratio * chat_window)),
            "keep_recent_turns": live_chat_keep,
        },
    }

    cost_cfg = config.models.get("cost_tracking") or {}
    voice_cfg = cost_cfg.get("voice") or {}
    # Project the voice sub-block in a frontend-friendly shape: per-provider
    # `rate` + `cap_usd`. This matches `CostLedger.snapshot()`'s
    # `voice_providers` shape (minus `spent_usd`, which stays on the cost
    # store), so the cost panel renders config from identity (REST, always
    # available) and merges in live spend from the WS-driven cost store
    # via `?? 0` fallback — exactly how the chat rows are populated.
    voice_tts: dict[str, dict[str, float]] = {}
    for provider, fields in (voice_cfg.get("tts") or {}).items():
        rate = fields.get("cost_per_million_chars")
        cap = fields.get("daily_budget_usd")
        if rate is None or cap is None:
            continue
        voice_tts[provider] = {"rate": float(rate), "cap_usd": float(cap)}
    voice_stt: dict[str, dict[str, float]] = {}
    for provider, fields in (voice_cfg.get("stt") or {}).items():
        rate = fields.get("cost_per_audio_hour")
        cap = fields.get("daily_budget_usd")
        if rate is None or cap is None:
            continue
        voice_stt[provider] = {"rate": float(rate), "cap_usd": float(cap)}
    per_role = {
        role: float(cap) for role, cap in (cost_cfg.get("per_role") or {}).items()
    }
    # Global daily cap is *derived* — sum of every inner cap. The umbrella
    # is whatever the channels add up to; there is no separate
    # `daily_budget_usd` line in yaml that could drift from the sum.
    daily_budget_usd = (
        sum(per_role.values())
        + sum(p["cap_usd"] for p in voice_tts.values())
        + sum(p["cap_usd"] for p in voice_stt.values())
    )
    cost_tracking = {
        "enabled": bool(cost_cfg.get("enabled", False)),
        "warning_at_pct": float(cost_cfg.get("warning_at_pct", 0.75)),
        "daily_budget_usd": daily_budget_usd,
        "per_role": per_role,
        "voice": {"tts": voice_tts, "stt": voice_stt},
    }

    return web.json_response({
        "name": config.entity_name,
        "operator_name": config.operator_name,
        "version": tesseract.__version__,
        "security_mode": config.permissions.mode,
        "model_role": "chat_brain",
        "model_name": chat_brain["model"],
        "provider": chat_brain["provider"],
        "observer_model": observer_head["model"] if observer_head else None,
        "observer_provider": observer_head["provider"] if observer_head else None,
        "models": models_summary,
        "roles": roles_summary,
        "compact_thresholds": compact_thresholds,
        "cost_tracking": cost_tracking,
    })


def _live_chat_attr(app: web.Application, attr: str, fallback: float | None, cast: type) -> int | float:
    """Return `attr` from the first live ChatSession, falling back to `fallback`.

    Settings edits propagate to every active ServerSession.chat_session, so
    any live session reflects current truth; if none are open the yaml value
    applies to the next fresh session.
    """
    sessions = app.get("server_sessions") or {}
    for sess in sessions.values():
        cs = getattr(sess, "chat_session", None)
        if cs is not None:
            try:
                return cast(getattr(cs, attr))
            except (TypeError, ValueError):
                continue
    return cast(fallback) if fallback is not None else cast(0)


async def set_mode(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    new_mode = body.get("mode")
    if not isinstance(new_mode, str):
        return web.json_response({"error": "missing 'mode'"}, status=400)

    policy = request.app["config"].permissions
    previous = policy.mode
    try:
        policy.set_mode(new_mode)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    log.info("security_mode: %s -> %s", previous, policy.mode)
    await _broadcast_mode_change(request.app, previous, policy.mode)
    return web.json_response({"mode": policy.mode, "previous": previous})


async def _broadcast_mode_change(app: web.Application, from_mode: str, to_mode: str) -> None:
    sessions = app.get("sessions") or {}
    for session_id, ws in list(sessions.items()):
        envelope = make_envelope(
            "mode_changed", "routing", session_id, {"from": from_mode, "to": to_mode},
        )
        try:
            await ws.send_json(envelope)
        except Exception:
            log.debug("mode_changed broadcast skipped for %s (likely closed)", session_id)


async def tools(request: web.Request) -> web.Response:
    registry = request.app.get("tool_registry")
    if registry is None:
        return web.json_response({"tools": []})
    policy = request.app["config"].permissions
    defaults = policy.tools_defaults
    out: list[dict] = []
    for tool in registry.tools.values():
        raw_default = defaults.get(tool.name, DEFAULT_POSTURE)
        out.append({
            "name": tool.name,
            "description": tool.description,
            "permission": policy.default_posture(tool.name),
            "default_posture": raw_default,
            "mode_override": policy.has_mode_override(tool.name),
            "path_sensitive": policy.has_path_overrides(tool.name),
        })
    return web.json_response({"tools": out, "mode": policy.mode})
