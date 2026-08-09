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


# ── Identity write ──────────────────────────────────────────────────

# Long enough for a two-word name, short enough that the value stays
# usable as a cockpit header and as half of a spoken wake phrase.
_MAX_IDENTITY_LEN = 40


def _clean_identity_value(raw: object, field: str) -> str:
    """Collapse to a single-line, single-spaced name or reject it.

    Whitespace is collapsed rather than merely stripped because every
    consumer is a one-line surface: the cockpit header, a chat bubble, and
    the wake phrase, which tokenizes `<prefix> <name>` and would silently
    fold an embedded newline into the match window.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    value = " ".join(raw.split())
    if not value:
        raise ValueError(f"{field} must not be blank")
    if len(value) > _MAX_IDENTITY_LEN:
        raise ValueError(f"{field} must be at most {_MAX_IDENTITY_LEN} characters")
    return value


def _clean_wake_word(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("wake_word must be an object")
    updates: dict[str, object] = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("wake_word.enabled must be a boolean")
        updates["enabled"] = raw["enabled"]
    if "prefix" in raw:
        updates["prefix"] = _clean_identity_value(raw["prefix"], "wake_word.prefix")
    if not updates:
        raise ValueError("wake_word must carry 'enabled' or 'prefix'")
    return updates


def _clean_gender(raw: object) -> str:
    """Validate against the derivation table rather than a literal list here.

    Pronouns are derived from this value, so a gender nothing can map is not
    a preference to store — it is a write that would leave the agent
    describing itself one way and referred to another.
    """
    from tesseract.config_seed import PRONOUNS

    value = str(raw or "").strip().lower()
    if value not in PRONOUNS:
        raise ValueError(f"gender must be one of: {', '.join(sorted(PRONOUNS))}")
    return value


async def set_identity(request: web.Request) -> web.Response:
    """POST /api/identity — rename the agent, the operator, or the wake phrase.

    `mirror.yaml` stays in `file_write`'s `_LOCKED_CONFIG_FILES`, so a tool
    cannot reach these keys; this operator-attended route is the sanctioned
    writer. Every field is optional and only the ones present are written —
    the Identity tab saves one control at a time.

    Workspace documents the operator or the assistant has EDITED are never
    rewritten by a rename: that prose is theirs, and editing it under them
    is the thing this route has always refused to do.

    A document still byte-identical to what was seeded is not authored prose,
    and `config_seed.refresh_seeded_docs` re-renders those with the new values
    on the next boot — so a rename or a gender change does reach an untouched
    IDENTITY.md. The protection is against overwriting authorship, not against
    the documents ever being correct.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    try:
        updates = {
            key: _clean_identity_value(body[key], key)
            for key in ("name", "operator_name")
            if key in body
        }
        if "gender" in body:
            updates["gender"] = _clean_gender(body["gender"])
        wake = _clean_wake_word(body["wake_word"]) if "wake_word" in body else {}
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not updates and not wake:
        return web.json_response(
            {
                "error": "nothing to update: send 'name', 'operator_name', "
                "'gender' or 'wake_word'"
            },
            status=400,
        )

    return await apply_identity_updates(request.app, updates, wake)


async def apply_identity_updates(
    app: web.Application,
    updates: dict[str, object],
    wake: dict[str, object],
) -> web.Response:
    """Persist already-validated identity keys, reload, broadcast.

    Shared with the Voice settings panel's wake-word toggle so there is one
    writer for `mirror.yaml::identity` rather than two that drift.
    """
    from tesseract.lib.yaml_io import round_trip_yaml
    from tesseract.mirror.server.config_watcher import refresh_identity
    from tesseract.mirror.server.routes.settings import mirror_yaml_path

    def _apply(doc: object) -> None:
        identity = doc.get("identity")  # type: ignore[union-attr]
        if identity is None:
            raise KeyError("identity")
        identity.update(updates)
        if wake:
            # Write into the existing block rather than creating one: the
            # threshold beside it is a required key, and a half-block here
            # is refused by `load_identity` at the next read.
            block = identity.get("wake_word")
            if block is None:
                raise KeyError("identity.wake_word")
            block.update(wake)

    path = mirror_yaml_path(app)
    try:
        round_trip_yaml(path, _apply)
    except KeyError as exc:
        return web.json_response({"error": f"mirror.yaml missing key: {exc}"}, status=500)
    except (OSError, ValueError) as exc:
        return web.json_response({"error": f"failed to write mirror.yaml: {exc}"}, status=500)

    try:
        applied = refresh_identity(app, path)
    except Exception as exc:
        # The file is written but the live config still holds the old value.
        # Saying so beats a success the running process does not honour.
        log.exception("set_identity: live reload failed after writing %s", path)
        return web.json_response(
            {"error": f"saved, but the live reload failed: {exc}"}, status=500
        )

    log.info("identity: %s", applied)
    await _broadcast_identity_changed(app, applied)
    return web.json_response(applied)


async def _broadcast_identity_changed(
    app: web.Application, applied: dict[str, object]
) -> None:
    sessions = app.get("sessions") or {}
    for session_id, ws in list(sessions.items()):
        envelope = make_envelope("identity_changed", "routing", session_id, applied)
        try:
            await ws.send_json(envelope)
        except Exception:
            log.debug("identity_changed broadcast skipped for %s (likely closed)", session_id)


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
