"""Voice REST surface — providers list, catalog, selection, synthesis test.

``GET /api/voice/providers`` returns the active voice subsystem state:
the STT and TTS primary refs and their fallback chains. The Settings →
Models picker calls this when rendering the current selection.

``GET /api/voice/catalog`` lists every ``kind: tts`` entry the catalog
holds, with the lane's current primary + fallbacks. The Identity tab's
voice picker renders this — a voice added to ``providers.yaml`` appears
without a code change, and nothing here names a provider.

``POST /api/voice/primary`` writes the operator's pick to
``roles.yaml::voice.tts.primary`` and rebuilds the voice runtime.

``POST /api/voice/test`` synthesizes the configured sample line via the
TTSEngine and returns the audio body — the "click to hear how it sounds"
affordance, and an end-to-end check of the engine. Both the voice and
its character are config (the resolved `voice.tts` ref and that entry's
per-surface `synthesis_presets`), so neither is adjustable per call.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import yaml
from aiohttp import web

from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.voice.lane_config import apply_tts_primary

log = logging.getLogger(__name__)

_TEST_MAX_CHARS = 500
_TTS_KIND = "tts"


def _mirror_yaml_path(app: web.Application):
    from tesseract.mirror.server.routes.settings import mirror_yaml_path

    return mirror_yaml_path(app)


def _roles_yaml_path(app: web.Application):
    from tesseract.mirror.server.routes.settings import _roles_yaml_path

    return _roles_yaml_path(app)


def sample_line(request: web.Request) -> str:
    """The line a voice audition speaks, from ``mirror.yaml::voice.test_sample``.

    Read from disk rather than `app["config"]` for the same reason the
    voice settings panel does: the operator may have just renamed the
    agent, and the live `ServerConfig` only catches up when the watcher's
    debounce fires. `{name}` renders the current name; a template without
    it is spoken verbatim. Missing key raises — a sample line the operator
    can't edit is a hardcoded default by another name.
    """
    raw = yaml.safe_load(_mirror_yaml_path(request.app).read_text(encoding="utf-8")) or {}
    block = raw.get("voice") if isinstance(raw, dict) else None
    template = (block or {}).get("test_sample") if isinstance(block, dict) else None
    if not isinstance(template, str) or not template.strip():
        raise KeyError("mirror.yaml missing required 'voice.test_sample'")
    identity = (raw.get("identity") or {}) if isinstance(raw, dict) else {}
    return template.replace("{name}", str(identity.get("name") or "").strip())


async def get_providers(request: web.Request) -> web.Response:
    cfg = request.app["config"].models.get("voice") or {}
    if not cfg:
        return web.json_response(
            {"enabled": False, "reason": "no `voice:` block in roles.yaml"},
            status=200,
        )
    def _summarize_chain(block: dict | None) -> dict | None:
        if not block or not block.get("chain"):
            return None
        chain = block["chain"]
        return {
            "mode": block.get("mode", "active"),
            "primary": chain[0]["ref"],
            "fallbacks": [e["ref"] for e in chain[1:]],
            "chain": [
                {
                    "ref": e["ref"],
                    "adapter": e.get("adapter"),
                    "provider": e.get("provider"),
                    "model": e.get("model"),
                    "daily_budget_usd": e.get("daily_budget_usd", 0.0),
                }
                for e in chain
            ],
        }

    out = {
        "enabled": True,
        "stt": _summarize_chain(cfg.get("stt")),
        "tts": _summarize_chain(cfg.get("tts")),
    }
    return web.json_response(out)


def _tts_lane_from_disk(app: web.Application) -> dict[str, Any]:
    """Read ``voice.tts`` straight from roles.yaml.

    The picker saves and immediately re-reads; the in-memory bundle only
    catches up when the config watcher's debounce fires, so reading it
    would hand the operator back the voice they just changed away from.
    """
    raw = yaml.safe_load(_roles_yaml_path(app).read_text(encoding="utf-8")) or {}
    voice = (raw.get("voice") or {}) if isinstance(raw, dict) else {}
    lane = voice.get("tts") if isinstance(voice, dict) else None
    return lane if isinstance(lane, dict) else {}


def _tts_catalog_rows() -> list[dict[str, Any]]:
    """Every ``kind: tts`` catalog entry, whatever provider carries it.

    `label` / `gender` are optional catalog fields — a provider that
    doesn't set them renders by ref, rather than the picker inventing a
    display name for a model it doesn't know.
    """
    from tesseract.brain.boot import load_bundle

    bundle = load_bundle()
    rows: list[dict[str, Any]] = []
    for ref, conn, model in bundle.all_models():
        if model.kind != _TTS_KIND:
            continue
        fields = model.fields
        rows.append({
            "ref": ref,
            "tier": conn.tier,
            "provider": conn.name,
            "model_id": model.id,
            "adapter": conn.adapter,
            "label": str(fields.get("label") or ""),
            "gender": str(fields.get("gender") or ""),
            # A ref whose tier or provider switch is off is skipped by the
            # voice-runtime build, so it cannot be selected — say so here
            # rather than letting the operator pick a silent lane.
            "enabled": bool(conn.tier_enabled and conn.enabled),
        })
    rows.sort(key=lambda row: row["ref"])
    return rows


async def get_catalog(request: web.Request) -> web.Response:
    """GET /api/voice/catalog — selectable voices + the live TTS lane."""
    try:
        rows = await asyncio.to_thread(_tts_catalog_rows)
    except Exception as exc:  # noqa: BLE001 — config errors must surface
        log.exception("voice/catalog: load_bundle failed")
        return web.json_response({"error": f"voice catalog: {exc}"}, status=503)

    try:
        lane = _tts_lane_from_disk(request.app)
    except (OSError, yaml.YAMLError) as exc:
        return web.json_response({"error": f"failed to read roles.yaml: {exc}"}, status=500)

    try:
        sample = sample_line(request)
    except (OSError, KeyError, yaml.YAMLError) as exc:
        return web.json_response({"error": str(exc)}, status=500)

    return web.json_response({
        "voices": rows,
        "primary": str(lane.get("primary") or ""),
        "fallbacks": [str(f) for f in (lane.get("fallbacks") or [])],
        "sample_text": sample,
    })


async def set_primary(request: web.Request) -> web.Response:
    """POST /api/voice/primary — pick the voice that speaks.

    Body: ``{"ref": "<tier>.<provider>.<model_id>"}``. The ref must name a
    ``kind: tts`` catalog entry whose tier and provider are both enabled;
    anything else would write a lane the runtime then skips, leaving the
    operator with a saved selection that never speaks.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    ref = body.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return web.json_response({"error": "ref must be a non-empty string"}, status=400)
    ref = ref.strip()

    try:
        rows = await asyncio.to_thread(_tts_catalog_rows)
    except Exception as exc:  # noqa: BLE001
        log.exception("voice/primary: load_bundle failed")
        return web.json_response({"error": f"voice catalog: {exc}"}, status=503)

    row = next((r for r in rows if r["ref"] == ref), None)
    if row is None:
        return web.json_response(
            {"error": f"ref {ref!r} is not a tts entry in providers.yaml"},
            status=400,
        )
    if not row["enabled"]:
        return web.json_response(
            {
                "error": (
                    f"ref {ref!r} is disabled in providers.yaml — enable its "
                    f"tier and provider in Settings → Capabilities first"
                )
            },
            status=400,
        )

    try:
        lane = _tts_lane_from_disk(request.app)
    except (OSError, yaml.YAMLError) as exc:
        return web.json_response({"error": f"failed to read roles.yaml: {exc}"}, status=500)
    if lane.get("primary") == ref:
        # Re-picking the current voice must not touch the file. The
        # round-trip re-serializes the whole document (it reflows long
        # block scalars elsewhere in roles.yaml), which would churn the
        # operator's config and wake the watcher for no change.
        return web.json_response({
            "primary": ref,
            "fallbacks": [str(f) for f in (lane.get("fallbacks") or [])],
            "applied": False,
            "live_update_failed": False,
            "live_update_error": None,
        })

    try:
        await asyncio.to_thread(
            round_trip_yaml, _roles_yaml_path(request.app), lambda d: apply_tts_primary(d, ref)
        )
    except KeyError as exc:
        return web.json_response({"error": f"roles.yaml missing key: {exc}"}, status=500)
    except (OSError, ValueError) as exc:
        return web.json_response({"error": f"failed to write roles.yaml: {exc}"}, status=500)

    # YAML is canonical; a rebuild failure is reported without pretending the
    # write didn't land (same contract as `set_role_models`).
    rebuild_error: str | None = None
    try:
        from tesseract.mirror.server.app import _build_voice_runtime

        await asyncio.to_thread(_build_voice_runtime, request.app)
    except Exception as exc:  # noqa: BLE001
        log.exception("voice/primary: voice runtime rebuild failed after YAML committed")
        rebuild_error = f"live rebuild failed: {exc}"

    lane = _tts_lane_from_disk(request.app)
    return web.json_response({
        "primary": str(lane.get("primary") or ""),
        "fallbacks": [str(f) for f in (lane.get("fallbacks") or [])],
        "applied": True,
        "live_update_failed": rebuild_error is not None,
        "live_update_error": rebuild_error,
    })


async def post_test(request: web.Request) -> web.Response:
    """Synthesize the configured sample line via the TTS engine.

    Body: `{"text": "..."}` overrides the line for a one-off check; with
    no text it speaks `mirror.yaml::voice.test_sample`. The voice is
    whatever the `voice.tts` chain resolves to, so there is nothing to
    override per call. Returns JSON with `audio_b64` + `provider` +
    `byte_count` so the caller can decode locally without a separate
    Content-Type negotiation — the same payload shape the `tts_chunk`
    envelope carries during a spoken turn.
    """
    engine = request.app.get("tts_engine")
    if engine is None:
        return web.json_response(
            {"error": "tts_engine_unavailable",
             "reason": "voice subsystem disabled or TTS provider not configured"},
            status=503,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    text = (body.get("text") if isinstance(body, dict) else None) or None
    if text is None:
        try:
            text = sample_line(request)
        except (OSError, KeyError, yaml.YAMLError) as exc:
            return web.json_response({"error": str(exc)}, status=500)
    if not isinstance(text, str):
        return web.json_response({"error": "text must be a string"}, status=400)
    if len(text) > _TEST_MAX_CHARS:
        return web.json_response(
            {"error": f"text exceeds {_TEST_MAX_CHARS}-char smoke-test limit"},
            status=400,
        )

    try:
        audio, provider = await engine.synthesize(text)
    except Exception as exc:
        log.exception("voice/test synthesize failed")
        return web.json_response(
            {"error": "synthesis_failed", "reason": str(exc)[:200]},
            status=502,
        )

    return web.json_response({
        "provider": provider,
        "byte_count": len(audio),
        "audio_b64": base64.b64encode(audio).decode("ascii"),
        "char_count": len(text),
    })
