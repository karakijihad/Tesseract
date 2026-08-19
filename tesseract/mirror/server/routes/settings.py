from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.brain.boot import rebuild_adapters
from tesseract.config import factory_reset
from tesseract.config.loader import ROLE_MODES, ROLE_MODE_INACTIVE
from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.mirror.server.routes._localhost import is_localhost_request
from tesseract.mirror.server.config import VOICE_RATE_FIELDS, project_voice_cost_view
from tesseract.mirror.server.ws import emit_stats
from tesseract.permissions import bash_security

log = logging.getLogger(__name__)

_MIN_RATIO = 0.10
_MAX_RATIO = 0.95
_MIN_KEEP_RECENT = 2
_MAX_KEEP_RECENT = 200
# Loop-limit guards. The lower bounds are deliberate: at least 1 tool iteration
# (otherwise no tool can ever run) and at least 1 consecutive adapter error
# before the breaker trips. Upper bounds prevent runaway loops without being so
# tight they break legitimate long workflows.
_MIN_TOOL_ITER = 1
_MAX_TOOL_ITER = 200
_MIN_CONSEC_ERR = 1
_MAX_CONSEC_ERR = 20
_VALID_COST_ROLES = frozenset({"chat_brain", "claude_cli", "codex_cli", "observer_agent"})

# Config files the "Raw config" section may expose. Anything not here is
# off-limits (e.g. `.env`, workspace/ system prompts, logs/). Adding a new
# file here is a one-line allow, but the default is deny.
_SAFE_CONFIG_FILES = (
    "providers.yaml",
    "roles.yaml",
    "permissions.yaml",
    "mirror.yaml",
    "schedule.yaml",
    "vault.yaml",
    "conscience.yaml",
    "terminal.yaml",
)
_MAX_CONFIG_BYTES = 200_000

_VALID_POSTURES = frozenset({"auto", "ask", "deny"})

_VALID_ROLE_MODES = ROLE_MODES
# `chat_brain` is load-bearing — every session needs an active conversational
# adapter. Allow operator to change which model serves the role; refuse to
# leave the role mode-disabled (would brick new sessions).
_LOAD_BEARING_ROLES = frozenset({"chat_brain"})
# Equivalent kinds — picking a model with the same family as the current
# primary keeps the role coherent (an stt model can be swapped for an
# audio_stt model). Targets whose kind is not listed here use a singleton
# family of {kind}: e.g. ``image_generation`` only swaps for image_generation.
_KIND_FAMILIES: dict[str, frozenset[str]] = {
    "chat": frozenset({"chat"}),
    "embedding": frozenset({"embedding"}),
    "stt": frozenset({"stt", "audio_stt"}),
    "audio_stt": frozenset({"stt", "audio_stt"}),
    "tts": frozenset({"tts"}),
    "image_generation": frozenset({"image_generation"}),
}


def _permissions_yaml_path(app: web.Application) -> Path:
    return app["tesseract_dir"] / "config" / "permissions.yaml"


async def set_tool_permission(request: web.Request) -> web.Response:
    """Update a single tool's default posture in `permissions.yaml.tools`.

    The hard security layer (`bash_security.py`) is unaffected — its checks
    fire before any posture lookup and no setting reaches them. No count is
    restated here: the last one drifted by two, and `bash_security.rules()`
    is the only thing entitled to say how many there are.
    mode overrides in `permissions.yaml.modes.<mode>.overrides` still apply
    on top of this default; path overrides still win. This endpoint only
    tunes the baseline `tools.<name>` posture.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    name = body.get("name")
    posture = body.get("posture")
    if not isinstance(name, str) or not name:
        return web.json_response({"error": "name must be a non-empty string"}, status=400)
    if not isinstance(posture, str) or posture.lower() not in _VALID_POSTURES:
        return web.json_response(
            {"error": f"posture must be one of {sorted(_VALID_POSTURES)}"},
            status=400,
        )
    posture = posture.lower()

    registry = request.app.get("tool_registry")
    if registry is not None and name not in registry.tools:
        return web.json_response({"error": f"unknown tool '{name}'"}, status=400)

    def _apply(d: Any) -> None:
        tools = d.setdefault("tools", {})
        tools[name] = posture

    yaml_path = _permissions_yaml_path(request.app)
    try:
        _round_trip_yaml(yaml_path, _apply)
    except KeyError as exc:
        return web.json_response(
            {"error": f"permissions.yaml missing key: {exc}"}, status=500
        )

    request.app["config"].permissions.tools_defaults[name] = posture

    return web.json_response({"name": name, "posture": posture})


#: Which panes own a reset button, and the scope each one restores. Named here
#: rather than taken from the request so a caller cannot ask for a scope no
#: screen renders — `factory_reset.SCOPES` also carries `capabilities`, which
#: has its own route because a switch reset records consent as well.
_RESETTABLE = ("session", "loop_limits", "cost", "tools")


async def reset_defaults(request: web.Request) -> web.Response:
    """POST /api/settings/reset-defaults — one pane back to shipped values.

    Body: `{"scope": "session"|"loop_limits"|"cost"|"tools"}`. Returns
    `{changed, missing}` as `file::key.path` strings; the pane re-fetches its
    own state rather than this route learning to render four different shapes.

    Nothing is synced in memory here on purpose. `config_watcher` watches all
    four files and reloads on the write — the same path a hand edit takes, and
    the one these panes already tell the operator is equivalent.
    """
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    scope = body.get("scope")
    if scope not in _RESETTABLE:
        return web.json_response(
            {"error": f"scope must be one of {list(_RESETTABLE)}"}, status=400
        )
    try:
        changed, missing = await asyncio.to_thread(factory_reset.restore, scope)
        return web.json_response(
            {"changed": [str(c) for c in changed], "missing": missing}
        )
    except (OSError, ValueError) as exc:
        # ValueError covers yaml.YAMLError. Refused rather than half-applied:
        # `restore` writes nothing when any file in the scope fails to parse.
        return web.json_response(
            {"error": f"could not read the shipped config: {exc}"}, status=500
        )


def _providers_yaml_path(app: web.Application) -> Path:
    return app["tesseract_dir"] / "config" / "providers.yaml"


def _roles_yaml_path(app: web.Application) -> Path:
    return app["tesseract_dir"] / "config" / "roles.yaml"


# `_round_trip_yaml` is a thin alias to keep the existing call sites in this
# file readable. The shared implementation lives in `tesseract/lib/yaml_io.py`.
_round_trip_yaml = round_trip_yaml


async def set_compact_threshold(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    role = body.get("role")
    if role != "chat_brain":
        return web.json_response(
            {"error": "observer compaction not supported; role must be 'chat_brain'"},
            status=400,
        )

    ratio: float | None = None
    if "ratio" in body:
        try:
            ratio = float(body["ratio"])
        except (TypeError, ValueError):
            return web.json_response({"error": "ratio must be a number"}, status=400)
        if not (_MIN_RATIO <= ratio <= _MAX_RATIO):
            return web.json_response(
                {"error": f"ratio must be between {_MIN_RATIO} and {_MAX_RATIO}"},
                status=400,
            )

    keep_recent: int | None = None
    if "keep_recent_turns" in body:
        raw = body["keep_recent_turns"]
        try:
            keep_recent = int(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "keep_recent_turns must be an integer"}, status=400
            )
        if not (_MIN_KEEP_RECENT <= keep_recent <= _MAX_KEEP_RECENT):
            return web.json_response(
                {
                    "error": (
                        f"keep_recent_turns must be between "
                        f"{_MIN_KEEP_RECENT} and {_MAX_KEEP_RECENT}"
                    )
                },
                status=400,
            )

    if ratio is None and keep_recent is None:
        return web.json_response(
            {"error": "at least one of ratio or keep_recent_turns is required"},
            status=400,
        )

    yaml_path = _roles_yaml_path(request.app)
    try:
        doc = _round_trip_yaml(
            yaml_path, lambda d: _apply_compaction_updates(d, ratio, keep_recent)
        )
    except KeyError as exc:
        return web.json_response({"error": f"roles.yaml missing key: {exc}"}, status=500)

    _sync_in_memory_compaction(request.app, ratio, keep_recent)
    _update_live_chat_sessions(request.app, ratio, keep_recent)

    chat_brain_role = (doc.get("roles") or {}).get("chat_brain") or {}
    compact_source = chat_brain_role
    try:
        context_source = _resolve_primary_model_fields(request.app, chat_brain_role)
    except KeyError as exc:
        return web.json_response(
            {"error": f"providers.yaml missing key: {exc}"}, status=500,
        )
    try:
        context_window = int(context_source["context_window"])
        effective_ratio = (
            ratio if ratio is not None else float(compact_source["compact_threshold"])
        )
        effective_keep = (
            keep_recent if keep_recent is not None else int(compact_source["keep_recent_turns"])
        )
    except KeyError as exc:
        return web.json_response(
            {"error": f"config chat_brain missing key: {exc}"}, status=500,
        )
    tokens = int(round(effective_ratio * context_window))

    await _emit_stats_for_all_sessions(request.app)

    return web.json_response({
        "role": "chat_brain",
        "ratio": effective_ratio,
        "context_window": context_window,
        "tokens": tokens,
        "keep_recent_turns": effective_keep,
    })


def _apply_compaction_updates(
    doc: Any, ratio: float | None, keep_recent: int | None
) -> None:
    """Write compact knobs to `roles.chat_brain` so the role applies them to
    whichever catalog model the primary/fallback chain currently resolves to.
    """
    roles = doc.get("roles")
    if not roles or "chat_brain" not in roles:
        raise KeyError("roles.chat_brain")
    chat_brain = roles["chat_brain"]
    if ratio is not None:
        chat_brain["compact_threshold"] = ratio
    if keep_recent is not None:
        chat_brain["keep_recent_turns"] = keep_recent


def _sync_in_memory_compaction(
    app: web.Application, ratio: float | None, keep_recent: int | None
) -> None:
    roles = app["config"].models.get("roles") or {}
    chat_brain = roles.get("chat_brain") or {}
    if not chat_brain:
        return
    if ratio is not None:
        chat_brain["compact_threshold"] = ratio
    if keep_recent is not None:
        chat_brain["keep_recent_turns"] = keep_recent
    # Legacy synthesized shape also carries `resolution[0]` for compat —
    # keep it in lockstep so reads from either path see the same value.
    resolution = chat_brain.get("resolution") or []
    if resolution:
        primary = resolution[0]
        if ratio is not None:
            primary["compact_threshold"] = ratio
        if keep_recent is not None:
            primary["keep_recent_turns"] = keep_recent


def _resolve_primary_model_fields(app: web.Application, chat_brain_role: dict) -> dict:
    """Return the catalog model `fields` dict for the role's `primary` ref.

    Used by routes that need decoding params (context_window, max_output_tokens)
    when the role config no longer inlines them. Looks up via the loader so the
    canonical resolution path is used.
    """
    primary_ref = chat_brain_role.get("primary")
    if not primary_ref:
        raise KeyError("roles.chat_brain.primary")
    from tesseract.config.loader import load_config

    bundle = load_config(
        providers_path=_providers_yaml_path(app),
        roles_path=_roles_yaml_path(app),
    )
    return dict(bundle.resolve(str(primary_ref)).model.fields)


def _update_live_chat_sessions(
    app: web.Application, ratio: float | None, keep_recent: int | None
) -> None:
    sessions = app.get("server_sessions") or {}
    for sess in sessions.values():
        cs = getattr(sess, "chat_session", None)
        if cs is None:
            continue
        if ratio is not None:
            cs.compact_threshold = ratio
        if keep_recent is not None:
            cs.keep_recent_turns = keep_recent


async def _emit_stats_for_all_sessions(app: web.Application) -> None:
    sessions = app.get("server_sessions") or {}
    for sess in sessions.values():
        if getattr(sess, "chat_session", None) is None:
            continue
        try:
            await emit_stats(app, sess)
        except Exception:
            log.exception(
                "session_stats emit failed for %s", getattr(sess, "session_id", "?")
            )


async def get_session_caps(request: web.Request) -> web.Response:
    """GET /api/settings/session-caps — current loop limits for chat_brain.

    Returns the two operator-tunable caps (tool iteration, consecutive
    adapter error) plus the DENY rules themselves — read from
    ``bash_security.rules()`` rather than restated here, so the panel cannot
    go on claiming a count this module has grown past.
    """
    roles = request.app["config"].models.get("roles") or {}
    chat_brain = roles.get("chat_brain") or {}
    if "tool_iteration_cap" not in chat_brain or "consecutive_error_cap" not in chat_brain:
        return web.json_response(
            {"error": "chat_brain caps missing from roles.yaml — check loader sync"},
            status=503,
        )
    return web.json_response({
        "tool_iteration_cap": int(chat_brain["tool_iteration_cap"]),
        "consecutive_error_cap": int(chat_brain["consecutive_error_cap"]),
        "deny_rules_locked": True,
        "deny_rules": bash_security.rules(),
    })


async def set_session_caps(request: web.Request) -> web.Response:
    """POST /api/settings/session-caps — update tool_iteration_cap and/or
    consecutive_error_cap on `roles.chat_brain`.

    Writes to roles.yaml, syncs in-memory config, and propagates the new
    values to every live ChatSession so the change lands without a
    restart (mirrors the `set_compact_threshold` pattern).

    DENY rules from `permissions.yaml::bash_security` are intentionally
    NOT exposed here — they are a fixed constant no policy can relax.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    tool_cap: int | None = None
    if "tool_iteration_cap" in body:
        try:
            tool_cap = int(body["tool_iteration_cap"])
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "tool_iteration_cap must be an integer"}, status=400
            )
        if not (_MIN_TOOL_ITER <= tool_cap <= _MAX_TOOL_ITER):
            return web.json_response(
                {
                    "error": (
                        f"tool_iteration_cap must be between "
                        f"{_MIN_TOOL_ITER} and {_MAX_TOOL_ITER}"
                    )
                },
                status=400,
            )

    err_cap: int | None = None
    if "consecutive_error_cap" in body:
        try:
            err_cap = int(body["consecutive_error_cap"])
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "consecutive_error_cap must be an integer"}, status=400
            )
        if not (_MIN_CONSEC_ERR <= err_cap <= _MAX_CONSEC_ERR):
            return web.json_response(
                {
                    "error": (
                        f"consecutive_error_cap must be between "
                        f"{_MIN_CONSEC_ERR} and {_MAX_CONSEC_ERR}"
                    )
                },
                status=400,
            )

    if tool_cap is None and err_cap is None:
        return web.json_response(
            {
                "error": (
                    "at least one of tool_iteration_cap or "
                    "consecutive_error_cap is required"
                )
            },
            status=400,
        )

    yaml_path = _roles_yaml_path(request.app)
    try:
        _round_trip_yaml(yaml_path, lambda d: _apply_session_caps(d, tool_cap, err_cap))
    except KeyError as exc:
        return web.json_response({"error": f"roles.yaml missing key: {exc}"}, status=500)

    _sync_in_memory_session_caps(request.app, tool_cap, err_cap)
    _update_live_session_caps(request.app, tool_cap, err_cap)

    roles = request.app["config"].models.get("roles") or {}
    chat_brain = roles.get("chat_brain") or {}
    if "tool_iteration_cap" not in chat_brain or "consecutive_error_cap" not in chat_brain:
        return web.json_response(
            {"error": "chat_brain caps missing from roles.yaml — check loader sync"},
            status=503,
        )
    return web.json_response({
        "tool_iteration_cap": int(chat_brain["tool_iteration_cap"]),
        "consecutive_error_cap": int(chat_brain["consecutive_error_cap"]),
        "deny_rules_locked": True,
        "deny_rules": bash_security.rules(),
    })


def _apply_session_caps(
    doc: Any, tool_cap: int | None, err_cap: int | None
) -> None:
    roles = doc.get("roles")
    if not roles or "chat_brain" not in roles:
        raise KeyError("roles.chat_brain")
    chat_brain = roles["chat_brain"]
    if tool_cap is not None:
        chat_brain["tool_iteration_cap"] = tool_cap
    if err_cap is not None:
        chat_brain["consecutive_error_cap"] = err_cap


def _sync_in_memory_session_caps(
    app: web.Application, tool_cap: int | None, err_cap: int | None
) -> None:
    roles = app["config"].models.get("roles") or {}
    chat_brain = roles.get("chat_brain") or {}
    if not chat_brain:
        return
    if tool_cap is not None:
        chat_brain["tool_iteration_cap"] = tool_cap
    if err_cap is not None:
        chat_brain["consecutive_error_cap"] = err_cap
    # Mirror `_sync_in_memory_compaction`: keep the legacy synthesized
    # `resolution[0]` shape in lockstep so any reader walking the
    # legacy path sees the same value as the top-level dict key.
    resolution = chat_brain.get("resolution") or []
    if resolution:
        primary = resolution[0]
        if tool_cap is not None:
            primary["tool_iteration_cap"] = tool_cap
        if err_cap is not None:
            primary["consecutive_error_cap"] = err_cap


def _update_live_session_caps(
    app: web.Application, tool_cap: int | None, err_cap: int | None
) -> None:
    sessions = app.get("server_sessions") or {}
    for sess in sessions.values():
        cs = getattr(sess, "chat_session", None)
        if cs is None:
            continue
        if tool_cap is not None:
            cs.max_tool_iterations = tool_cap
        if err_cap is not None:
            cs.max_consecutive_adapter_errors = err_cap


async def set_cost(request: web.Request) -> web.Response:
    """Update cost-tracking config — per-role sub-caps and the uniform
    `warning_at_pct`. The global daily cap is *derived* (sum of per_role +
    every voice provider's daily_budget_usd), so it's not editable here.
    Voice provider rates / caps are still updated via the voice-cost POST
    but contribute to the same umbrella when this route returns the
    full IdentityCostTracking shape.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    cost_cfg = request.app["config"].models.get("cost_tracking") or {}
    current_pct = float(cost_cfg.get("warning_at_pct", 0.75))
    current_per_role = {
        role: float(cap) for role, cap in (cost_cfg.get("per_role") or {}).items()
    }

    new_pct = current_pct
    if "warning_at_pct" in body:
        try:
            new_pct = float(body["warning_at_pct"])
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "warning_at_pct must be a number"}, status=400
            )
        if not (0.0 <= new_pct <= 1.0):
            return web.json_response(
                {"error": "warning_at_pct must be between 0 and 1"}, status=400
            )

    per_role_in = body.get("per_role")
    if per_role_in is not None and not isinstance(per_role_in, dict):
        return web.json_response({"error": "per_role must be an object"}, status=400)
    new_per_role = dict(current_per_role)
    if isinstance(per_role_in, dict):
        for role_name, cap_raw in per_role_in.items():
            if role_name not in _VALID_COST_ROLES:
                return web.json_response(
                    {"error": f"unknown role '{role_name}'"}, status=400
                )
            try:
                cap = float(cap_raw)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"per_role.{role_name} must be a number"}, status=400
                )
            if cap < 0:
                return web.json_response(
                    {"error": f"per_role.{role_name} must be >= 0"}, status=400
                )
            new_per_role[role_name] = cap

    try:
        _round_trip_yaml(
            _providers_yaml_path(request.app),
            lambda d: _apply_cost_update_providers(d, new_pct),
        )
        _round_trip_yaml(
            _roles_yaml_path(request.app),
            lambda d: _apply_cost_update_roles(d, new_per_role),
        )
    except KeyError as exc:
        return web.json_response({"error": f"config missing key: {exc}"}, status=500)

    _sync_in_memory_cost(request.app, new_pct, new_per_role)

    ledger = request.app.get("cost_ledger")
    if ledger is not None:
        try:
            ledger.reload()
        except Exception:
            log.exception("CostLedger.reload() failed after settings write")

    return web.json_response(_identity_cost_tracking(request.app))


async def set_voice_cost(request: web.Request) -> web.Response:
    """Update voice pricing + per-provider daily caps under
    `cost_tracking.voice.{tts,stt}.{provider}`.

    Body shape:
        {
          "tts": {
            "<provider>": {
              "cost_per_million_chars": float,  // optional
              "daily_budget_usd": float,        // optional
            },
            ...
          },
          "stt": {
            "<provider>": {
              "cost_per_audio_hour": float,     // optional
              "daily_budget_usd": float,        // optional
            },
            ...
          },
        }

    Provider keys must already exist in the yaml (no provider creation here —
    pricing for an unknown provider would be a config error). Each field is
    optional within a provider entry; partial edits are merged. The global
    daily cap is *derived* (sum of per_role + every voice provider cap),
    so there is no "must not exceed global" check here — bumping a voice
    cap simply raises the umbrella by the same amount. After write,
    `ledger.reload()` re-parses `cost_tracking.voice` so live sessions
    see the new rates without restart.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    tts_in = body.get("tts") or {}
    stt_in = body.get("stt") or {}
    if not isinstance(tts_in, dict) or not isinstance(stt_in, dict):
        return web.json_response(
            {"error": "tts and stt must be objects"}, status=400
        )
    if not tts_in and not stt_in:
        return web.json_response(
            {"error": "at least one of tts or stt is required"}, status=400
        )

    cost_cfg = request.app["config"].models.get("cost_tracking") or {}
    voice_cfg = (cost_cfg.get("voice") or {})
    known_tts = set((voice_cfg.get("tts") or {}).keys())
    known_stt = set((voice_cfg.get("stt") or {}).keys())

    parsed_tts: dict[str, dict[str, float]] = {}
    for provider, fields in tts_in.items():
        if provider not in known_tts:
            return web.json_response(
                {"error": f"unknown TTS provider '{provider}'; known: {sorted(known_tts)}"},
                status=400,
            )
        if not isinstance(fields, dict):
            return web.json_response(
                {"error": f"tts.{provider} must be an object"}, status=400
            )
        entry: dict[str, float] = {}
        # A TTS lane prices in one unit or the other, never both: a local
        # voice per character of text, a generative cloud one per second
        # of the speech it produced. Accepting only the first meant the
        # panel could write a per-audio-hour figure into the per-character
        # field — a rate wrong by five orders of magnitude that the ledger
        # would then bill against.
        rate_fields = [k for k in VOICE_RATE_FIELDS if k in fields]
        if len(rate_fields) > 1:
            return web.json_response(
                {
                    "error": (
                        f"tts.{provider} sets {' and '.join(sorted(rate_fields))} — "
                        f"a lane prices in one unit"
                    )
                },
                status=400,
            )
        for rate_field in rate_fields:
            try:
                v = float(fields[rate_field])
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"tts.{provider}.{rate_field} must be a number"},
                    status=400,
                )
            if v < 0:
                return web.json_response(
                    {"error": f"tts.{provider}.{rate_field} must be >= 0"},
                    status=400,
                )
            entry[rate_field] = v
        if "daily_budget_usd" in fields:
            try:
                v = float(fields["daily_budget_usd"])
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"tts.{provider}.daily_budget_usd must be a number"},
                    status=400,
                )
            if v <= 0:
                return web.json_response(
                    {"error": f"tts.{provider}.daily_budget_usd must be > 0"},
                    status=400,
                )
            entry["daily_budget_usd"] = v
        if entry:
            parsed_tts[provider] = entry

    parsed_stt: dict[str, dict[str, float]] = {}
    for provider, fields in stt_in.items():
        if provider not in known_stt:
            return web.json_response(
                {"error": f"unknown STT provider '{provider}'; known: {sorted(known_stt)}"},
                status=400,
            )
        if not isinstance(fields, dict):
            return web.json_response(
                {"error": f"stt.{provider} must be an object"}, status=400
            )
        entry = {}
        if "cost_per_audio_hour" in fields:
            try:
                v = float(fields["cost_per_audio_hour"])
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"stt.{provider}.cost_per_audio_hour must be a number"},
                    status=400,
                )
            if v < 0:
                return web.json_response(
                    {"error": f"stt.{provider}.cost_per_audio_hour must be >= 0"},
                    status=400,
                )
            entry["cost_per_audio_hour"] = v
        if "daily_budget_usd" in fields:
            try:
                v = float(fields["daily_budget_usd"])
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"stt.{provider}.daily_budget_usd must be a number"},
                    status=400,
                )
            if v <= 0:
                return web.json_response(
                    {"error": f"stt.{provider}.daily_budget_usd must be > 0"},
                    status=400,
                )
            entry["daily_budget_usd"] = v
        if entry:
            parsed_stt[provider] = entry

    if not parsed_tts and not parsed_stt:
        return web.json_response(
            {
                "error": (
                    "no recognized fields to update — each provider must include "
                    "at least one of: cost_per_million_chars or "
                    "cost_per_audio_hour (whichever unit the lane prices in), "
                    "or daily_budget_usd"
                )
            },
            status=400,
        )

    try:
        _round_trip_yaml(
            _providers_yaml_path(request.app),
            lambda d: _apply_voice_cost_providers(d, parsed_tts, parsed_stt),
        )
        _round_trip_yaml(
            _roles_yaml_path(request.app),
            lambda d: _apply_voice_cost_roles(d, parsed_tts, parsed_stt),
        )
    except KeyError as exc:
        return web.json_response({"error": f"config missing key: {exc}"}, status=500)

    _sync_in_memory_voice_cost(request.app, parsed_tts, parsed_stt)

    ledger = request.app.get("cost_ledger")
    if ledger is not None:
        try:
            ledger.reload()
        except Exception:
            log.exception("CostLedger.reload() failed after voice settings write")

    return web.json_response(_identity_cost_tracking(request.app))


def _find_provider_model(
    doc: Any, model_id: str, *, merged: bool = False
) -> dict | None:
    """Walk providers.yaml to locate the catalog entry whose key is `model_id`.

    Tier blocks may carry a scalar ``enabled:`` reserved key alongside the
    provider sub-blocks; skip anything that isn't a mapping.

    A model block carrying ``voices:`` is shared settings over several voice
    entries, so the voice's own sub-block is what a per-model write must
    land on — writing the parent would reprice every voice under it. That is
    the default, and it is the wrong answer for a READER: `loader.py::
    _expanded_models` materializes a voice as ``{**shared, **overrides}``, so
    a reader stopping at the sub-block misses every field the parent holds —
    `synthesis_presets` among them, which is the whole character of the voice.
    Pass ``merged=True`` to get the entry the runtime actually built.
    """
    for tier_name in ("api", "cli", "local"):
        tier = doc.get(tier_name) or {}
        for _provider_name, prov_block in tier.items():
            if not isinstance(prov_block, dict):
                continue
            models = prov_block.get("models") or {}
            if model_id in models:
                return models[model_id]
            for entry in models.values():
                if not isinstance(entry, dict):
                    continue
                voices = entry.get("voices") or {}
                if isinstance(voices, dict) and model_id in voices:
                    if not merged:
                        return voices[model_id]
                    shared = {k: v for k, v in entry.items() if k != "voices"}
                    return {**shared, **(voices[model_id] or {})}
    return None


def _apply_voice_cost_providers(
    doc: Any,
    tts: dict[str, dict[str, float]],
    stt: dict[str, dict[str, float]],
) -> None:
    """Write per-model unit pricing to providers.yaml. The `model_id` keys
    in the request match catalog entry names (e.g. `af_heart`,
    `gemini_flash_audio`)."""
    # The validator has already established that a lane names at most one
    # rate field, so writing whichever one arrived cannot leave an entry
    # carrying both — which the catalog reader would have to break a tie on.
    for parsed in (tts, stt):
        for model_id, fields in parsed.items():
            present = [k for k in VOICE_RATE_FIELDS if k in fields]
            if not present:
                continue
            entry = _find_provider_model(doc, model_id)
            if entry is None:
                raise KeyError(f"providers.yaml model '{model_id}' missing")
            for rate_key in present:
                entry[rate_key] = fields[rate_key]


def _apply_voice_cost_roles(
    doc: Any,
    tts: dict[str, dict[str, float]],
    stt: dict[str, dict[str, float]],
) -> None:
    """Write per-ref daily caps under roles.yaml::voice.{tts,stt}.settings.<ref>.

    The request is keyed by catalog model id (e.g. ``af_heart``);
    we resolve each model id back to its catalog ref by scanning the lane's
    chain (`primary` + `fallbacks`), then write `daily_budget_usd` into the
    `settings:` map keyed by that ref. The block is created on demand so an
    operator can land a cap on a ref that never had per-ref settings yet.
    """
    voice = doc.get("voice")
    if voice is None:
        raise KeyError("voice")
    for kind_in, parsed in (("tts", tts), ("stt", stt)):
        lane = voice.get(kind_in)
        if not isinstance(lane, dict):
            continue
        chain_refs: list[str] = []
        primary = lane.get("primary")
        if isinstance(primary, str):
            chain_refs.append(primary)
        for fb in lane.get("fallbacks") or []:
            if isinstance(fb, str):
                chain_refs.append(fb)
        settings_block = lane.get("settings")
        if not isinstance(settings_block, dict):
            settings_block = {}
            lane["settings"] = settings_block
        for model_id, fields in parsed.items():
            if "daily_budget_usd" not in fields:
                continue
            ref = next(
                (r for r in chain_refs if r.rsplit(".", 1)[-1] == model_id),
                None,
            )
            if ref is None:
                continue
            entry = settings_block.get(ref)
            if not isinstance(entry, dict):
                entry = {}
                settings_block[ref] = entry
            entry["daily_budget_usd"] = fields["daily_budget_usd"]


def _sync_in_memory_voice_cost(
    app: web.Application,
    tts: dict[str, dict[str, float]],
    stt: dict[str, dict[str, float]],
) -> None:
    models = app["config"].models
    ct = models.get("cost_tracking")
    if ct is None:
        models["cost_tracking"] = {}
        ct = models["cost_tracking"]
    voice = ct.get("voice")
    if voice is None:
        ct["voice"] = {}
        voice = ct["voice"]
    for kind, parsed in (("tts", tts), ("stt", stt)):
        block = voice.setdefault(kind, {})
        for provider, fields in parsed.items():
            entry = block.setdefault(provider, {})
            for k, v in fields.items():
                entry[k] = v


def _coerce_positive(
    body: dict, key: str, default: float, allow_zero: bool
) -> tuple[float, str | None]:
    if key not in body:
        return default, None
    raw = body.get(key)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0, f"{key} must be a number"
    if allow_zero and val < 0:
        return 0.0, f"{key} must be >= 0"
    if not allow_zero and val <= 0:
        return 0.0, f"{key} must be > 0"
    return val, None


def _apply_cost_update_providers(doc: Any, warning_at_pct: float) -> None:
    """Write `cost_tracking.warning_at_pct` to providers.yaml. Per-role caps
    live in roles.yaml — see `_apply_cost_update_roles`."""
    ct = doc.get("cost_tracking")
    if ct is None:
        raise KeyError("cost_tracking")
    ct["warning_at_pct"] = warning_at_pct


def _apply_cost_update_roles(doc: Any, per_role: dict[str, float]) -> None:
    """Write each role's per-day cap to `roles.<name>.daily_budget_usd`."""
    roles = doc.get("roles")
    if roles is None:
        raise KeyError("roles")
    for role, cap in per_role.items():
        if role not in roles:
            raise KeyError(f"roles.{role}")
        roles[role]["daily_budget_usd"] = cap


def _sync_in_memory_cost(
    app: web.Application, warning_at_pct: float, per_role: dict[str, float]
) -> None:
    models = app["config"].models
    ct = models.get("cost_tracking")
    if ct is None:
        models["cost_tracking"] = {}
        ct = models["cost_tracking"]
    ct["warning_at_pct"] = warning_at_pct
    ct["per_role"] = dict(per_role)


def _identity_cost_tracking(app: web.Application) -> dict[str, Any]:
    """Produce the same `IdentityCostTracking` shape that `/api/identity`
    returns. Both `set_cost` and `set_voice_cost` use this so the
    frontend's `useIdentityStore.costTracking` can be refreshed from
    either endpoint's response."""
    cost_cfg = app["config"].models.get("cost_tracking") or {}
    voice_cfg = cost_cfg.get("voice") or {}
    voice_tts, voice_stt = project_voice_cost_view(voice_cfg)
    per_role = {
        role: float(cap) for role, cap in (cost_cfg.get("per_role") or {}).items()
    }
    daily = (
        sum(per_role.values())
        + sum(p["cap_usd"] for p in voice_tts.values())
        + sum(p["cap_usd"] for p in voice_stt.values())
    )
    return {
        "enabled": bool(cost_cfg.get("enabled", False)),
        "warning_at_pct": float(cost_cfg.get("warning_at_pct", 0.75)),
        "daily_budget_usd": daily,
        "per_role": per_role,
        "voice": {"tts": voice_tts, "stt": voice_stt},
    }


async def get_config_files(request: web.Request) -> web.Response:
    """Read-only listing + content of safe-listed YAML config files.

    Every file is opt-in via `_SAFE_CONFIG_FILES`. The request never takes a
    path parameter — there is no way to read arbitrary filesystem content
    through this endpoint. Missing files are reported with `content: null`
    rather than 404'd, so the UI can render a placeholder row for any file
    that simply doesn't exist in the operator's checkout.
    """
    config_dir = request.app["tesseract_dir"] / "config"
    out: list[dict[str, Any]] = []
    for name in _SAFE_CONFIG_FILES:
        path = config_dir / name
        if not path.exists() or not path.is_file():
            out.append({
                "name": name,
                "path": f"tesseract/config/{name}",
                "content": None,
                "lines": 0,
                "bytes": 0,
                "missing": True,
            })
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            out.append({
                "name": name,
                "path": f"tesseract/config/{name}",
                "content": None,
                "lines": 0,
                "bytes": 0,
                "missing": False,
                "error": "read_failed",
            })
            continue
        size_bytes = len(raw.encode("utf-8"))
        truncated = False
        if size_bytes > _MAX_CONFIG_BYTES:
            raw = raw[:_MAX_CONFIG_BYTES]
            truncated = True
        out.append({
            "name": name,
            "path": f"tesseract/config/{name}",
            "content": raw,
            "lines": raw.count("\n") + (0 if raw.endswith("\n") else 1),
            "bytes": size_bytes,
            "missing": False,
            "truncated": truncated,
        })
    return web.json_response({"files": out})


async def set_role_models(request: web.Request) -> web.Response:
    """Mutate a role's `mode` and/or promote a `resolution[]` entry to primary.

    Body: `{role: str, mode?: 'active'|'inactive', primary_model?: str}`.
    Both edits are optional but at least one must be present.

    `primary_model` rotates the matching `resolution[]` entry to index 0 (the
    rest preserve relative order). After the YAML write succeeds we call
    `rebuild_adapters` synchronously so live ChatSession/observer instances
    pick up the new primary on their next turn — no session reopen needed.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    role = body.get("role")
    if not isinstance(role, str) or not role:
        return web.json_response(
            {"error": "role must be a non-empty string"}, status=400
        )
    # Validate against the live bundle so any role added to roles.yaml
    # works without a code change.
    try:
        from tesseract.config.loader import load_config as _load_config
        _live_bundle = _load_config(
            providers_path=_providers_yaml_path(request.app),
            roles_path=_roles_yaml_path(request.app),
        )
    except Exception as exc:  # noqa: BLE001 — surface to UI
        return web.json_response({"error": f"config load failed: {exc}"}, status=500)
    if role not in _live_bundle.roles:
        return web.json_response(
            {"error": f"role must be one of {sorted(_live_bundle.roles.keys())}"},
            status=400,
        )

    mode = body.get("mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in _VALID_ROLE_MODES:
            return web.json_response(
                {"error": f"mode must be one of {sorted(_VALID_ROLE_MODES)}"}, status=400
            )
        if mode == ROLE_MODE_INACTIVE and role in _LOAD_BEARING_ROLES:
            return web.json_response(
                {"error": f"role '{role}' is load-bearing and cannot be set inactive"},
                status=400,
            )

    primary_model = body.get("primary_model")
    if primary_model is not None and not isinstance(primary_model, str):
        return web.json_response(
            {"error": "primary_model must be a string"}, status=400
        )

    if mode is None and primary_model is None:
        return web.json_response(
            {"error": "at least one of mode or primary_model is required"}, status=400
        )

    # Validate primary_model against the live in-memory chain before touching
    # disk. Catches typos / stale UI state without a roundtrip. Supports both
    # legacy (`resolution[*].model`) and split (`primary` + `fallbacks` refs).
    if primary_model is not None:
        candidates = _candidate_models_for_role(request.app, role)
        if not candidates:
            # An inactive role resolves to no refs at all, so there is nothing
            # to validate against. Say that, rather than reporting the model
            # unavailable against an empty list — the operator's next move is
            # to switch the role on, and "candidates: []" does not suggest it.
            return web.json_response(
                {
                    "error": (
                        f"role={role} has no resolvable models — switch it "
                        "active before picking one"
                    )
                },
                status=400,
            )
        if primary_model not in candidates:
            return web.json_response(
                {
                    "error": (
                        f"primary_model '{primary_model}' is not available for "
                        f"role={role}; candidates: {candidates}"
                    )
                },
                status=400,
            )

    try:
        _round_trip_yaml(
            _roles_yaml_path(request.app),
            lambda d: _apply_role_models_update(
                d, role, mode, primary_model, _providers_yaml_path(request.app)
            ),
        )
        _sync_in_memory_role_models(request.app, role, mode, primary_model)
        head = _primary_summary_for_role(request.app, role)
    except KeyError as exc:
        return web.json_response(
            {"error": f"config missing key: {exc}"}, status=500,
        )

    # YAML is canonical; rebuild_adapters failure is surfaced via
    # `live_update_failed` so the UI can show a toast without lying about
    # whether the YAML write itself succeeded.
    rebuild_summary: dict[str, Any] = {}
    rebuild_error: str | None = None
    try:
        rebuild_summary = rebuild_adapters(request.app) or {}
    except Exception as exc:
        log.exception("set_role_models: rebuild_adapters raised after YAML committed")
        rebuild_error = f"live rebuild failed: {exc}"
    if rebuild_error is None and "chat_brain_error" in rebuild_summary and role == "chat_brain":
        rebuild_error = rebuild_summary["chat_brain_error"]

    return web.json_response({
        "role": role,
        "mode": head["mode"],
        "primary": {
            "model": head["model"],
            "provider": head["provider"],
            "context_window": head["context_window"],
        },
        "applied": True,
        "live_update_failed": rebuild_error is not None,
        "live_update_error": rebuild_error,
        "live_sessions_swapped": rebuild_summary.get("live_sessions_swapped", 0),
    })


def _candidate_models_for_role(app: web.Application, role: str) -> list[str]:
    """Return the list of model names the role could use as `primary_model`.

    Both shapes supported: legacy `resolution[*].model` strings, or new split
    shape where each ref's catalog entry's `model` field is the candidate.
    """
    from tesseract.config.loader import load_config

    try:
        bundle = load_config(
            providers_path=_providers_yaml_path(app),
            roles_path=_roles_yaml_path(app),
        )
        rc = bundle.role(role)
    except Exception:
        return []
    # `primary` is None for an inactive role — the loader keeps those as
    # unresolved stubs. The `return` sits outside the `try` above, so a None
    # here dereferences into an AttributeError and the handler answers 500.
    # Reachable from the UI as soon as a switched-off role is asked for a
    # model, which is an ordinary thing to do before switching it back on.
    return [r.model.model for r in (rc.primary, *rc.fallbacks) if r is not None]


def _primary_summary_for_role(app: web.Application, role: str) -> dict[str, Any]:
    """Return `{mode, model, provider, context_window}` for the role's primary."""
    roles_cfg = (app["config"].models.get("roles") or {})
    role_cfg = roles_cfg.get(role) or {}
    if "resolution" in role_cfg:
        resolution_after = role_cfg.get("resolution") or []
        if not resolution_after:
            raise KeyError(f"roles.{role}.resolution")
        head = resolution_after[0]
        return {
            "mode": role_cfg.get("mode", ""),
            "model": head.get("model", ""),
            "provider": head.get("provider", ""),
            "context_window": int(head.get("context_window", 0)),
        }
    from tesseract.config.loader import load_config

    bundle = load_config(
        providers_path=_providers_yaml_path(app),
        roles_path=_roles_yaml_path(app),
    )
    rc = bundle.role(role)
    if rc.primary is None:
        # Inactive role — kept as a schema stub. Surface that to the UI
        # rather than crashing on `rc.primary.model`.
        return {"mode": rc.mode, "model": "", "provider": "", "context_window": 0}
    return {
        "mode": rc.mode,
        "model": rc.primary.model.model,
        "provider": rc.primary.connection.name,
        "context_window": int(rc.primary.model.fields.get("context_window", 0)),
    }


def _materialize_role_chain(doc: Any, role_cfg: Any) -> None:
    """Expand a `chain:`-backed role into its own primary + fallbacks.

    Picking a model in Settings is a decision about ONE role, so the first
    such edit detaches it from the shared chain rather than repointing every
    role that names the chain. Without this the write paths below would read
    an absent `primary`/`fallbacks` off the raw doc and flatten the chain to
    a single entry.
    """
    if "chain" not in role_cfg:
        return
    chain_name = str(role_cfg["chain"])
    if role_cfg.get("primary") is not None:
        # Both keys set — a shape the loader refuses, so this file never
        # booted. Drop `chain` and keep the explicit refs rather than writing
        # the pair back out: committing a doc that still cannot load would
        # fail `rebuild_adapters` after the YAML is already on disk.
        del role_cfg["chain"]
        return
    refs = [str(r) for r in ((doc.get("chains") or {}).get(chain_name) or [])]
    if not refs:
        raise KeyError(f"chains.{chain_name}")
    del role_cfg["chain"]
    role_cfg["primary"] = refs[0]
    role_cfg["fallbacks"] = refs[1:]


def _apply_role_models_update(
    doc: Any,
    role: str,
    mode: str | None,
    primary_model: str | None,
    providers_path: Path,
) -> None:
    """Mutate `roles.yaml::roles.<role>` — toggle mode, promote a fallback
    ref to primary. The reference shape is `<tier>.<provider>.<model_id>`;
    `primary_model` is the catalog model *name* (e.g. ``gpt-5.4-mini``)
    which we resolve back to the matching ref by walking providers.yaml.
    """
    roles = doc.get("roles")
    if not roles or role not in roles:
        raise KeyError(f"roles.{role}")
    role_cfg = roles[role]
    if mode is not None:
        role_cfg["mode"] = mode
    if primary_model is None:
        return

    _materialize_role_chain(doc, role_cfg)
    primary_ref = role_cfg.get("primary")
    fallbacks = list(role_cfg.get("fallbacks") or [])
    if primary_ref is None:
        raise KeyError(f"roles.{role}.primary")

    # `_apply_role_models_update` runs inside _round_trip_yaml on roles.yaml
    # — providers.yaml is not in scope here. Read it once to resolve refs.
    # The path comes from the caller, off the same app-scoped tree the roles
    # file is being written to: reading a global CONFIG_DIR here would let a
    # non-default tree validate the chosen model against one catalog and map
    # its name against another.
    import yaml as _yaml

    providers_doc = _yaml.safe_load(providers_path.read_text(encoding="utf-8")) or {}

    def _model_name_of(ref: str) -> str:
        try:
            tier, prov, mid = ref.split(".")
        except ValueError:
            return ""
        return (
            ((providers_doc.get(tier) or {}).get(prov) or {})
            .get("models", {})
            .get(mid, {})
            .get("model", "")
        )

    if _model_name_of(str(primary_ref)) == primary_model:
        return

    new_primary_idx = next(
        (i for i, ref in enumerate(fallbacks) if _model_name_of(str(ref)) == primary_model),
        None,
    )
    if new_primary_idx is None:
        raise KeyError(f"roles.{role}[model={primary_model}]")
    new_primary = fallbacks.pop(new_primary_idx)
    fallbacks.insert(0, primary_ref)
    role_cfg["primary"] = new_primary
    role_cfg["fallbacks"] = fallbacks


def _sync_in_memory_role_models(
    app: web.Application, role: str, mode: str | None, primary_model: str | None
) -> None:
    roles = app["config"].models.get("roles") or {}
    role_cfg = roles.get(role)
    if not role_cfg:
        return
    if mode is not None:
        role_cfg["mode"] = mode
    if primary_model is None:
        return

    if "resolution" in role_cfg:
        resolution = role_cfg.get("resolution") or []
        idx = next(
            (i for i, e in enumerate(resolution) if e.get("model") == primary_model),
            None,
        )
        if idx is not None and idx != 0:
            picked = resolution.pop(idx)
            resolution.insert(0, picked)
        return

    # Re-derive in-memory primary/fallbacks from the just-written file.
    try:
        import yaml as pyyaml

        roles_doc = pyyaml.safe_load(_roles_yaml_path(app).read_text(encoding="utf-8")) or {}
        fresh = (roles_doc.get("roles") or {}).get(role) or {}
        if "primary" in fresh:
            role_cfg["primary"] = fresh["primary"]
        if "fallbacks" in fresh:
            role_cfg["fallbacks"] = list(fresh["fallbacks"] or [])
    except Exception:
        log.exception("set_role_models: in-memory sync of split-file shape failed")


# ── Voice section ───────────────────────────────────────────────────


async def set_voice(request: web.Request) -> web.Response:
    """POST /api/settings/voice — write the operator's voice settings.

    Body: `wake_word_enabled`, written to `mirror.yaml::identity.wake_word`
    where it sits beside the name its phrase is built from.

    The write itself belongs to the identity route (AS-4) — this panel is
    one of two surfaces onto the same key, and two writers would mean two
    reload paths to keep in step. AS-5 moves the control to the Identity
    tab; this stays until it does, so the toggle is never homeless.

    There is no timbre knob — a local voice IS its model file, named per
    provider in providers.yaml. `default_rate` was removed rather than
    kept: nothing read it (`VoiceConfig` carries only the stt/tts lanes),
    so the route persisted a value that changed no speech while reporting
    success for it.
    """
    from tesseract.mirror.server.routes.system import apply_identity_updates

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    wake_word_enabled = body.get("wake_word_enabled")

    if wake_word_enabled is None:
        return web.json_response(
            {"error": "wake_word_enabled is required"}, status=400
        )
    if not isinstance(wake_word_enabled, bool):
        return web.json_response(
            {"error": "wake_word_enabled must be a boolean"}, status=400
        )

    response = await apply_identity_updates(
        request.app, {}, {"enabled": wake_word_enabled}
    )
    if response.status != 200:
        return response
    return web.json_response({"wake_word_enabled": wake_word_enabled})


#: The knobs each TTS adapter actually reads, and the shape each accepts.
#:
#: Keyed by the `adapter` on the provider's CONNECTION block, because that is
#: what decides which dataclass a preset is parsed into — `KokoroPreset` takes
#: two floats, `GeminiPreset` takes two strings, and neither reads the other's
#: keys (`app.py::_build_voice_runtime`).
#:
#: This is what makes the editor per-adapter rather than a free-text box. A
#: single editor would let `speed: 1.05` be saved against the cloud lane and
#: shown back on the panel while synthesis ignored it entirely — a control that
#: appears to work and does nothing, which is the defect the voice contract
#: already ruled against once.
#:
#: A lane whose adapter is absent here exposes no editable knobs and says so.
#: Adding one is this table plus the parse in `_build_voice_runtime`; a knob in
#: one and not the other is a dead control in whichever direction is missing.
_PRESET_KNOBS: dict[str, dict[str, dict[str, Any]]] = {
    "kokoro": {
        # Kokoro's only pacing knob, and the post-pad silence between
        # sentences. Bounded because the adapter floats them straight into
        # synthesis: a speed of 0 produces no audio and a large one produces
        # noise, neither of which reports an error.
        "speed": {"kind": "number", "min": 0.5, "max": 2.0},
        "sentence_silence": {"kind": "number", "min": 0.0, "max": 2.0},
    },
    "gemini": {
        # Prose, because the model takes prose: both are prefixed to the text
        # as a direction line. Bounded in length rather than in content — a
        # direction longer than the sentence it directs is billed per second
        # of speech it did not change.
        "style": {"kind": "text", "max_chars": 200},
        "pace": {"kind": "text", "max_chars": 200},
    },
}


def _adapter_for_ref(providers_raw: Any, ref: str) -> str:
    """The adapter a catalog ref resolves to, from its connection block.

    `adapter` is a connection-level key (`loader.py` requires it there and
    nowhere else), so a ref's knobs are decided by its provider rather than by
    the voice entry underneath it.
    """
    parts = str(ref).split(".")
    if len(parts) != 3:
        return ""
    tier, provider, _model = parts
    block = ((providers_raw.get(tier) or {}).get(provider)) or {}
    return str(block.get("adapter") or "") if isinstance(block, dict) else ""


def _clean_preset(
    knobs: dict[str, dict[str, Any]], settings: Any
) -> tuple[dict[str, Any], str]:
    """Validate one preset against the knobs its lane actually reads.

    Returns `(cleaned, error)`. A value the lane cannot honour is refused here
    rather than written: the whole point of a per-adapter editor is that saving
    it and ignoring it is not one of the outcomes.
    """
    if not isinstance(settings, dict):
        return {}, "settings must be an object"
    unknown = sorted(set(settings) - set(knobs))
    if unknown:
        return {}, (
            f"this lane does not read {', '.join(unknown)} — it reads "
            f"{', '.join(sorted(knobs))}"
        )
    cleaned: dict[str, Any] = {}
    for name, spec in knobs.items():
        if name not in settings:
            continue
        value = settings[name]
        if spec["kind"] == "number":
            # `bool` is an `int` in Python and would sail through `float()` as
            # 0.0 or 1.0 — a checkbox posted into a speed field.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {}, f"{name} must be a number"
            value = float(value)
            if not spec["min"] <= value <= spec["max"]:
                return {}, f"{name} must be between {spec['min']} and {spec['max']}"
            cleaned[name] = value
        else:
            if not isinstance(value, str):
                return {}, f"{name} must be text"
            value = value.strip()
            if len(value) > spec["max_chars"]:
                return {}, f"{name} must be {spec['max_chars']} characters or fewer"
            cleaned[name] = value
    if not cleaned:
        return {}, "no knobs were given"
    return cleaned, ""


async def get_voice(request: web.Request) -> web.Response:
    """GET /api/settings/voice — the operator-facing voice settings.

    Each per-surface synthesis preset carries the adapter it belongs to and
    the knobs that adapter reads, so the panel can render a typed field per
    knob instead of a free-text box that could save a key the lane ignores.

    The wake-word toggle is a UI control; how forgiving the gate is stays out
    of this payload entirely, because it is confirmed per voice rather than
    configured, and `GET /api/voice/wake` is the one place that reports it.

    Wake-word values are read from **mirror.yaml, not `app["config"]`**.
    The panel saves and immediately re-reads, while the live config only
    catches up when the watcher's debounce fires ~250ms later — reading
    the in-memory copy would hand the operator back the value they just
    changed away from and visibly flip their checkbox back."""
    import yaml

    try:
        raw = yaml.safe_load(_roles_yaml_path(request.app).read_text(encoding="utf-8")) or {}
        mirror_raw = yaml.safe_load(
            mirror_yaml_path(request.app).read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        return web.json_response({"error": f"failed to read config: {exc}"}, status=500)
    voice_block = raw.get("voice") or {}
    tts_block = voice_block.get("tts") or {}
    tts_settings = (tts_block.get("settings")) or {}

    # Presets live on the catalog entry — a voice's character travels with
    # the voice, not with the lane wiring. A per-ref block in roles.yaml may
    # replace them, and REPLACE is the word: `boot.load_voice_config`
    # materializes a chain entry as catalog-then-settings and its
    # `merged.update(provider.settings)` swaps the whole `synthesis_presets`
    # map, so an override naming only `intent` leaves `answer` on the
    # adapter's built-in defaults rather than on the catalog's.
    #
    # This panel used to lay the override over the catalog surface by surface,
    # which showed a character the engine was not using in exactly that case.
    # It reports what the runtime reads now, and the writer below always emits
    # the complete map so the two cannot drift apart again.
    try:
        providers_raw = yaml.safe_load(
            _providers_yaml_path(request.app).read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        return web.json_response({"error": f"failed to read catalog: {exc}"}, status=500)

    refs: list[str] = []
    primary = tts_block.get("primary")
    if isinstance(primary, str):
        refs.append(primary)
    for fb in tts_block.get("fallbacks") or []:
        if isinstance(fb, str):
            refs.append(fb)

    style_presets = []
    for ref in refs:
        entry = (
            _find_provider_model(providers_raw, ref.rsplit(".", 1)[-1], merged=True)
            or {}
        )
        shipped = dict((entry.get("synthesis_presets") or {}))
        override = ((tts_settings.get(ref) or {}).get("synthesis_presets")) or {}
        adapter = _adapter_for_ref(providers_raw, ref)
        live = dict(override) if override else shipped
        for surface, spec in live.items():
            style_presets.append({
                "ref": str(ref),
                "surface": str(surface),
                "adapter": adapter,
                # Whatever knobs this provider exposes — no shape is
                # assumed, so a new provider's presets render unchanged.
                "settings": {str(k): v for k, v in (spec or {}).items()},
                # What this lane READS, so the panel can offer a field per
                # knob and nothing else. Empty for an adapter with no entry in
                # `_PRESET_KNOBS`, which the panel renders read-only.
                "knobs": _PRESET_KNOBS.get(adapter, {}),
                # The shipped character, so "reset" is a value rather than a
                # second round trip — and so the panel can say when a surface
                # has been moved off it.
                "shipped": {
                    str(k): v for k, v in ((shipped.get(surface) or {})).items()
                },
                "overridden": bool(override),
            })

    identity = (mirror_raw.get("identity") or {}) if isinstance(mirror_raw, dict) else {}
    wake = identity.get("wake_word") or {}
    return web.json_response({
        "style_presets": style_presets,
        "wake_word_enabled": wake.get("enabled") is True,
        "wake_word_prefix": str(wake.get("prefix") or ""),
        # No sensitivity number here. What the gate uses is confirmed per
        # voice and reported by `GET /api/voice/wake`; a second figure on this
        # payload could only ever disagree with it.
        "entity_name": str(identity.get("name") or ""),
    })


async def set_voice_preset(request: web.Request) -> web.Response:
    """POST /api/settings/voice/preset — retune one surface of one lane.

    Body: ``{"ref": "<tier>.<provider>.<model>", "surface": "answer",
    "settings": {...}}``. Sending ``"settings": null`` resets that surface to
    the character the catalog ships.

    **An operator surface, and only that.** No tool reaches it: a preset the
    assistant could write would be the per-utterance tone control the voice
    contract refuses. What this adds is a second way for the OPERATOR to make
    an edit they could already make by hand in `roles.yaml`.

    Gated on `is_localhost_request`, and this docstring used to claim it was
    gated "like the rest of this module" — which was false. Nothing else in
    `settings.py` carries the check, so the sentence advertised a guard that
    did not exist, which is worse than having no guard: it is the reason
    nobody would go looking. The bind is 127.0.0.1 by default and that is the
    whole threat model, but `mirror.yaml::server.host` is a setting, and CORS
    deliberately passes an `Origin`-less request because that is what a native
    client sends. A writer of config belongs behind the same check
    `env_keys.py` uses.

    The whole `synthesis_presets` map is written, never one surface of it,
    because that is how the runtime reads it: `boot.load_voice_config` swaps
    the catalog's map for the settings block's, so writing one surface would
    silently drop the other onto the adapter's built-in defaults.

    Written to `roles.yaml`, not to the catalog: `providers.yaml` carries the
    shipped character, and leaving it alone is what makes reset a deletion
    rather than a guess at what was there before.
    """
    import yaml

    if not is_localhost_request(request):
        return web.json_response({"error": "localhost only"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    ref = str(body.get("ref") or "")
    surface = str(body.get("surface") or "")
    if not ref or not surface:
        return web.json_response({"error": "ref and surface are required"}, status=400)

    try:
        roles_raw = yaml.safe_load(
            _roles_yaml_path(request.app).read_text(encoding="utf-8")
        ) or {}
        providers_raw = yaml.safe_load(
            _providers_yaml_path(request.app).read_text(encoding="utf-8")
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        return web.json_response({"error": f"failed to read config: {exc}"}, status=500)

    tts_block = ((roles_raw.get("voice") or {}).get("tts")) or {}
    chain = [tts_block.get("primary"), *(tts_block.get("fallbacks") or [])]
    if ref not in [r for r in chain if isinstance(r, str)]:
        # A ref off the chain is a preset nothing would ever read — the lane
        # is not wired, so the block would sit in `roles.yaml` looking applied.
        return web.json_response(
            {"error": f"{ref} is not in the voice chain"}, status=400
        )

    entry = (
        _find_provider_model(providers_raw, ref.rsplit(".", 1)[-1], merged=True) or {}
    )
    shipped = dict(entry.get("synthesis_presets") or {})
    if surface not in shipped:
        # The adapters ask for `intent` and `answer` by name. A surface the
        # catalog does not declare is one nothing will ever request.
        return web.json_response(
            {
                "error": f"{ref} has no {surface} surface — it has "
                f"{', '.join(sorted(shipped)) or 'none'}"
            },
            status=400,
        )

    adapter = _adapter_for_ref(providers_raw, ref)
    knobs = _PRESET_KNOBS.get(adapter)
    if not knobs:
        return web.json_response(
            {"error": f"the {adapter or 'unknown'} lane exposes no editable knobs"},
            status=400,
        )

    reset = body.get("settings") is None
    cleaned: dict[str, Any] = {}
    if not reset:
        cleaned, error = _clean_preset(knobs, body.get("settings"))
        if error:
            return web.json_response({"error": error}, status=400)

    def _apply(doc: Any) -> None:
        voice = doc.get("voice")
        if voice is None:
            raise KeyError("voice")
        lane = voice.get("tts")
        if not isinstance(lane, dict):
            raise KeyError("voice.tts")
        settings_block = lane.get("settings")
        per_ref = (
            settings_block.get(ref) if isinstance(settings_block, dict) else None
        )
        # Read from the document this write is about to change, not from the
        # copy loaded to validate against. `round_trip_yaml` performs its own
        # read, so computing the new map up there and assigning it here would
        # overwrite anything that landed in between with a value derived from
        # a file that is one read out of date — and this is a whole-map
        # replacement, so what it loses is the whole preset block.
        existing = (
            (per_ref.get("synthesis_presets") or {})
            if isinstance(per_ref, dict)
            else {}
        )
        live = {
            str(name): dict(spec or {})
            for name, spec in (existing if existing else shipped).items()
        }

        if reset:
            live[surface] = dict(shipped.get(surface) or {})
        else:
            # Merged onto what this surface currently reads, not assigned over
            # it. `_clean_preset` accepts a subset — and the runtime reads each
            # knob with a dataclass default, so an omitted `pace` became the
            # empty string rather than the catalog's line. A retune of one knob
            # silently changed the other.
            current = live.get(surface) or {}
            live[surface] = {
                **{name: current[name] for name in knobs if name in current},
                **cleaned,
            }

        # Back to the shipped character on every surface: drop the override
        # entirely rather than writing a copy of the catalog into `roles.yaml`,
        # where it would then stop tracking a catalog the next update changes.
        if live == shipped:
            if isinstance(per_ref, dict):
                per_ref.pop("synthesis_presets", None)
                # A per-ref block that held nothing but the override goes with
                # it, so declining a retune leaves the file as it was found.
                if not per_ref:
                    settings_block.pop(ref, None)
            return

        if not isinstance(settings_block, dict):
            settings_block = {}
            lane["settings"] = settings_block
        if not isinstance(per_ref, dict):
            per_ref = {}
            settings_block[ref] = per_ref
        per_ref["synthesis_presets"] = live

    try:
        _round_trip_yaml(_roles_yaml_path(request.app), _apply)
    except KeyError as exc:
        return web.json_response({"error": f"config missing key: {exc}"}, status=500)
    except OSError as exc:
        return web.json_response({"error": f"failed to write config: {exc}"}, status=500)

    # Re-read rather than echo: the config watcher is what makes this take
    # effect, and handing back what was posted would show the operator their
    # own input whether or not it landed.
    return await get_voice(request)


# ── Phase 18 Task C — System section (capability detection) ─────────


async def get_system(request: web.Request) -> web.Response:
    """GET /api/settings/system?refresh=1 — this machine's facts.

    Reads `runtime/capability-state.json`, which the reconcile pass writes on
    every launch. With `?refresh=1` it runs a fresh pass first.

    It used to keep its own cache at `runtime/logs/capability-snapshot.json`,
    which was wrong twice over: that tree is the janitor's, pruned by age — so
    a cache of machine state silently expired — and it made two writers of one
    kind of fact. The reconcile pass is the single writer now; this route is a
    reader of the artifact, and the response shape is unchanged because the
    System tab is compiled into the installed `.exe` and does not reach an
    install through update.
    """
    from tesseract.capability.reconcile import legacy_system_payload, run
    from tesseract.capability.state import read_state

    refresh = request.query.get("refresh", "").lower() in ("1", "true", "yes")
    state = None if refresh else read_state()
    if state is None:
        # The pass already runs its blocking probes — `collect()` spawns
        # nvidia-smi and enumerates audio devices, 10-15s of it — in threads,
        # so nothing here stalls the Mirror event loop. A blocked loop times
        # out concurrent requests AND starves the voice and chat turn paths.
        state = await run()
    return web.json_response(legacy_system_payload(state.hardware))


# ── Phase 18 Task C — Session-resume policy ─────────────────────────


_VALID_RESUME_POLICIES = frozenset({"today_only", "today_plus_yesterday", "n_days", "always"})


def mirror_yaml_path(app: web.Application) -> Path:
    return app["tesseract_dir"] / "config" / "mirror.yaml"


async def get_session_policy(request: web.Request) -> web.Response:
    """GET /api/settings/session-policy — current resume policy."""
    import yaml

    try:
        raw = yaml.safe_load(mirror_yaml_path(request.app).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return web.json_response({"error": f"failed to read mirror.yaml: {exc}"}, status=500)
    session_block = raw.get("session") or {}
    return web.json_response({
        "policy": str(session_block.get("resume_policy") or "today_plus_yesterday"),
        "days": int(session_block.get("resume_days") or 1),
        "autosave": bool(session_block.get("autosave", True)),
        "autosave_interval_seconds": int(
            session_block.get("autosave_interval_seconds") or 60
        ),
        "show_config_reload_toasts": bool(
            (raw.get("ui") or {}).get("show_config_reload_toasts", True)
        ),
    })


async def set_session_policy(request: web.Request) -> web.Response:
    """POST /api/settings/session-policy — write resume + toast keys."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)

    policy = body.get("policy")
    days = body.get("days")
    show_toasts = body.get("show_config_reload_toasts")
    autosave = body.get("autosave")
    autosave_interval = body.get("autosave_interval_seconds")

    if autosave is not None and not isinstance(autosave, bool):
        return web.json_response({"error": "autosave must be a boolean"}, status=400)
    if autosave_interval is not None:
        try:
            autosave_interval = int(autosave_interval)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "autosave_interval_seconds must be an integer"}, status=400
            )
        # Bounds imported, not restated: the pump clamps to the same pair, and
        # a range the UI accepts but the pump silently overrides is worse than
        # either bound alone.
        from tesseract.mirror.server.session_autosave import (
            AUTOSAVE_MAX_SECONDS,
            AUTOSAVE_MIN_SECONDS,
        )
        if not (AUTOSAVE_MIN_SECONDS <= autosave_interval <= AUTOSAVE_MAX_SECONDS):
            return web.json_response(
                {
                    "error": "autosave_interval_seconds must be between "
                    f"{AUTOSAVE_MIN_SECONDS} and {AUTOSAVE_MAX_SECONDS}"
                },
                status=400,
            )
    if policy is not None and policy not in _VALID_RESUME_POLICIES:
        return web.json_response(
            {"error": f"policy must be one of {sorted(_VALID_RESUME_POLICIES)}"},
            status=400,
        )
    if days is not None:
        try:
            days = int(days)
        except (TypeError, ValueError):
            return web.json_response({"error": "days must be an integer"}, status=400)
        if not (1 <= days <= 365):
            return web.json_response(
                {"error": "days must be between 1 and 365"}, status=400
            )
    if show_toasts is not None and not isinstance(show_toasts, bool):
        return web.json_response(
            {"error": "show_config_reload_toasts must be a boolean"}, status=400
        )

    if (
        policy is None
        and days is None
        and show_toasts is None
        and autosave is None
        and autosave_interval is None
    ):
        return web.json_response({"error": "no fields to update"}, status=400)

    def _apply(doc: Any) -> None:
        session = doc.setdefault("session", {})
        if policy is not None:
            session["resume_policy"] = policy
        if days is not None:
            session["resume_days"] = days
        if autosave is not None:
            session["autosave"] = autosave
        if autosave_interval is not None:
            session["autosave_interval_seconds"] = autosave_interval
        if show_toasts is not None:
            ui = doc.setdefault("ui", {})
            ui["show_config_reload_toasts"] = show_toasts

    try:
        _round_trip_yaml(mirror_yaml_path(request.app), _apply)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)

    # Sync the in-memory toast flag immediately. The watcher's
    # `reload_mirror` only emits a "restart required" toast for bind/CORS
    # changes — but the toast-toggle itself is hot-reloadable.
    if show_toasts is not None:
        request.app["config_reload_toasts_enabled"] = show_toasts

    return web.json_response({
        "policy": policy,
        "days": days,
        "autosave": autosave,
        "autosave_interval_seconds": autosave_interval,
        "show_config_reload_toasts": show_toasts,
    })


# ── Catalog-backed model picker (per-target ref swap) ───────────────
#
# `GET /api/settings/catalog` returns the full providers.yaml catalog plus
# the current selection for each of the seven swappable targets the
# Settings → Models tab shows. `POST /api/settings/model-ref` writes a
# single target's `provider_ref` (or `roles.<name>.primary` / `embeddings.primary`)
# back to roles.yaml and dedupes the new ref out of the role's fallback
# list. The config_watcher picks the file change up and rebuilds adapters
# so live mirrors reflect repo edits and vice versa.

# Targets are discovered from the live bundle. Order: embeddings, voice
# lanes, then every entry in roles.yaml::roles in declaration order. A new
# role added to roles.yaml automatically appears in the Settings UI on the
# next /api/settings/catalog read — no code change required.

def _discover_ref_targets(bundle) -> list[str]:
    out: list[str] = ["embeddings"]
    if bundle.voice is not None and bundle.voice.stt is not None:
        out.append("voice_stt")
    if bundle.voice is not None and bundle.voice.tts is not None:
        out.append("voice_tts")
    out.extend(bundle.roles.keys())
    return out


def _current_ref_for_target(bundle, target: str) -> str | None:
    if target == "embeddings":
        return bundle.embeddings.ref
    if target == "voice_stt":
        v = bundle.voice
        return v.stt.primary.ref.ref if v and v.stt is not None else None
    if target == "voice_tts":
        v = bundle.voice
        return v.tts.primary.ref.ref if v and v.tts is not None else None
    rc = bundle.roles.get(target)
    return rc.primary.ref if rc and rc.primary is not None else None


def _allowed_kinds_for_target(bundle, target: str) -> frozenset[str]:
    """Compat family of the current primary's kind, or empty when unset.

    Returning an empty set short-circuits the swap: no model satisfies the
    filter, so the dropdown surfaces "(not configured)" rather than a list
    of models that would all reject on save.
    """
    ref = _current_ref_for_target(bundle, target)
    if not ref:
        return frozenset()
    try:
        kind = bundle.resolve(ref).model.kind
    except Exception:  # noqa: BLE001 — schema drift; treat as unswappable
        return frozenset()
    return _KIND_FAMILIES.get(kind, frozenset({kind}))


async def get_catalog(request: web.Request) -> web.Response:
    """GET /api/settings/catalog — every catalog entry + current selections.

    Response:
        {
          "entries": [{ref, tier, provider, model, kind, context_window}],
          "current": {chat_brain: ref, ..., voice_tts: ref|null},
          "voice_lanes": {stt_primary: "<catalog ref>", tts_primary: "<catalog ref>"},
        }
    """
    from tesseract.config.loader import load_config, ConfigError

    try:
        bundle = load_config(
            providers_path=_providers_yaml_path(request.app),
            roles_path=_roles_yaml_path(request.app),
        )
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=500)

    entries = []
    for ref, conn, model in bundle.all_models():
        ctx = model.fields.get("context_window")
        entries.append({
            "ref": ref,
            "tier": conn.tier,
            "provider": conn.name,
            "model": model.model,
            "kind": model.kind,
            "context_window": int(ctx) if ctx else 0,
            # Advisory. The picker shows these so the operator can see what a
            # model is for before wiring it — `allowed_kinds` below is what
            # actually constrains the choice.
            "good_for": [str(tag) for tag in (model.fields.get("good_for") or ())],
        })

    targets_in_order = _discover_ref_targets(bundle)
    current = {t: _current_ref_for_target(bundle, t) for t in targets_in_order}
    # Per-target metadata — kind, allowed kind family, mode (when the
    # target is a roles.yaml entry that carries one), whether the row
    # supports the active/disabled toggle. Frontend renders one row per
    # entry; adding a new role to roles.yaml surfaces a row automatically.
    targets_meta: list[dict[str, Any]] = []
    for t in targets_in_order:
        ref = current.get(t)
        kind: str | None = None
        if ref:
            try:
                kind = bundle.resolve(ref).model.kind
            except Exception:  # noqa: BLE001 — broken ref still surfaces
                kind = None
        allowed = sorted(_allowed_kinds_for_target(bundle, t))
        role_cfg = bundle.roles.get(t) if t not in {"embeddings", "voice_stt", "voice_tts"} else None
        mode = role_cfg.mode if role_cfg is not None else "active"
        targets_meta.append({
            "target": t,
            "kind": kind,
            "allowed_kinds": allowed,
            "current_ref": ref,
            "mode": mode,
            "allow_toggle": role_cfg is not None,
            "load_bearing": t in _LOAD_BEARING_ROLES,
        })

    # Catalog refs of the active voice primaries — replaces the old
    # lane-name fields (`stt_primary: "cloud"`). Frontend uses these to
    # render the current selection in the picker.
    voice_lanes = {
        "stt_primary": (
            bundle.voice.stt.primary.ref.ref
            if bundle.voice and bundle.voice.stt is not None else ""
        ),
        "tts_primary": (
            bundle.voice.tts.primary.ref.ref
            if bundle.voice and bundle.voice.tts is not None else ""
        ),
    }
    return web.json_response({
        "entries": entries,
        "current": current,
        "voice_lanes": voice_lanes,
        "targets": targets_meta,
    })


def _apply_model_ref_update(doc: Any, target: str, ref: str) -> None:
    """Write `ref` to the right path in roles.yaml for the given target.

    For any role under ``roles.<name>``, the new ref is promoted to
    ``primary`` and the old primary is pushed onto the front of
    ``fallbacks`` (deduped against the new ref). This works for any role
    declared in roles.yaml — adding a new role doesn't require a code
    change here.

    Voice lane writes resolve the active lane name (e.g. ``cloud``) from
    the *live* doc rather than a request-time snapshot — this avoids a
    TOCTOU where a concurrent edit to ``voice.<lane>.primary`` would land
    the new ref on the wrong (now-non-primary) lane key.
    """
    roles = doc.get("roles") or {}
    if target in roles:
        role_cfg = roles[target]
        _materialize_role_chain(doc, role_cfg)
        old_primary = role_cfg.get("primary")
        if old_primary == ref:
            return
        fallbacks = [str(f) for f in (role_cfg.get("fallbacks") or []) if str(f) != ref]
        if old_primary and str(old_primary) not in fallbacks:
            fallbacks.insert(0, str(old_primary))
        role_cfg["primary"] = ref
        role_cfg["fallbacks"] = fallbacks
        return
    if target == "embeddings":
        emb = doc.setdefault("embeddings", {})
        emb["primary"] = ref
        return
    if target in {"voice_stt", "voice_tts"}:
        voice = doc.setdefault("voice", {})
        lane_key = "stt" if target == "voice_stt" else "tts"
        block = voice.setdefault(lane_key, {})
        old_primary = block.get("primary")
        if old_primary == ref:
            return
        # Mirror the chat-brain swap: the new ref is promoted to
        # primary and the old primary is pushed onto the front of
        # `fallbacks` (deduped) so the operator can revert with one
        # click. Existing per-ref settings under `settings:` survive
        # untouched — the catalog ref is the key, the picker only
        # changes which entry is currently primary.
        fallbacks = [str(f) for f in (block.get("fallbacks") or []) if str(f) != ref]
        if old_primary and str(old_primary) not in fallbacks:
            fallbacks.insert(0, str(old_primary))
        block["primary"] = ref
        block["fallbacks"] = fallbacks
        return
    raise KeyError(f"unknown target: {target}")


async def set_model_ref(request: web.Request) -> web.Response:
    """POST /api/settings/model-ref — swap any target to any compatible catalog ref.

    Body: `{target, ref}`. Validates the ref against providers.yaml + the
    target's allowed `kind` set. Writes to roles.yaml then calls
    `rebuild_adapters` synchronously so live ChatSession adapters update on
    the next turn — the config_watcher remains as a safety net for external
    edits (CLI, git pull) but is no longer the primary propagation path.
    """
    from tesseract.config.loader import load_config, ConfigError

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    target = body.get("target")
    ref = body.get("ref")
    if not isinstance(target, str) or not target:
        return web.json_response({"error": "target must be a non-empty string"}, status=400)
    if not isinstance(ref, str) or not ref:
        return web.json_response({"error": "ref must be a non-empty string"}, status=400)

    try:
        bundle = load_config(
            providers_path=_providers_yaml_path(request.app),
            roles_path=_roles_yaml_path(request.app),
        )
        resolved = bundle.resolve(ref)
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    valid_targets = _discover_ref_targets(bundle)
    if target not in valid_targets:
        return web.json_response(
            {"error": f"target must be one of {sorted(valid_targets)}"},
            status=400,
        )

    allowed = _allowed_kinds_for_target(bundle, target)
    if not allowed:
        # Target has no current primary so we can't infer a kind family.
        # Accept any kind in this case — the operator is configuring a
        # role for the first time.
        pass
    elif resolved.model.kind not in allowed:
        return web.json_response(
            {
                "error": (
                    f"target '{target}' requires kind in {sorted(allowed)}, "
                    f"got '{resolved.model.kind}' for ref '{ref}'"
                )
            },
            status=400,
        )

    try:
        _round_trip_yaml(
            _roles_yaml_path(request.app),
            lambda d: _apply_model_ref_update(d, target, ref),
        )
    except KeyError as exc:
        return web.json_response({"error": f"roles.yaml missing key: {exc}"}, status=500)

    # Synchronously propagate to live sessions. Without this, the swap depends
    # on config_watcher firing — a Windows watchdog miss leaves live ChatSession
    # adapters bound to the old model and the next turn reverts visually.
    #
    # If rebuild fails, the YAML write has already committed (disk is canonical
    # already). Return 200 with `live_update_failed: true` so the UI can
    # surface a toast without misrepresenting that the swap was rejected — the
    # next session reopen will pick up the new YAML.
    rebuild_summary: dict[str, Any] = {}
    rebuild_error: str | None = None
    try:
        rebuild_summary = rebuild_adapters(request.app) or {}
    except Exception as exc:
        log.exception("set_model_ref: rebuild_adapters raised after YAML committed")
        rebuild_error = f"live rebuild failed: {exc}"
    if rebuild_summary.get("voice"):
        # A rebuilt engine is a new chain, so the "a different voice is
        # speaking" notice must be able to fire against it. The latch is per
        # session and outlives the engine that set it; this is the third of
        # the three places a rebuild happens, and the one an operator reaches
        # by picking a voice directly.
        try:
            from tesseract.mirror.server.tts import clear_fallback_notices

            clear_fallback_notices(request.app)
        except Exception as exc:  # noqa: BLE001 — a courtesy must not fail the swap
            log.info("set_model_ref: could not reset the voice fallback notice (%s)", exc)
    if rebuild_error is None and "chat_brain_error" in rebuild_summary and target == "chat_brain":
        rebuild_error = rebuild_summary["chat_brain_error"]

    return web.json_response({
        "target": target,
        "ref": ref,
        "tier": resolved.connection.tier,
        "provider": resolved.connection.name,
        "model": resolved.model.model,
        "kind": resolved.model.kind,
        "applied": True,
        "live_update_failed": rebuild_error is not None,
        "live_update_error": rebuild_error,
        "live_sessions_swapped": rebuild_summary.get("live_sessions_swapped", 0),
    })
