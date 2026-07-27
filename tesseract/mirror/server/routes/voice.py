"""Voice REST surface — providers list + synthesis smoke test.

``GET /api/voice/providers`` returns the active voice subsystem state:
the STT and TTS primary refs, their fallback chains, and the
operator's default voice. The Settings → Models picker calls this
when rendering the current selection.

``POST /api/voice/test`` synthesizes a short string via the TTSEngine
and returns the audio body. Useful for an in-cockpit "click to hear
how TARS sounds" affordance and for verifying the engine end-to-end.
Style/character is **config-only** (per-surface synthesis_presets in
roles.yaml) — not adjustable from this endpoint.
"""

from __future__ import annotations

import base64
import logging

from aiohttp import web

log = logging.getLogger(__name__)

_TEST_MAX_CHARS = 500


async def get_providers(request: web.Request) -> web.Response:
    cfg = request.app["config"].models.get("voice") or {}
    if not cfg:
        return web.json_response(
            {"enabled": False, "reason": "no `voice:` block in roles.yaml"},
            status=200,
        )
    voice_state = request.app.get("voice_state")

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
        "default_voice_id": cfg.get("default_voice_id"),
        "current_voice": (
            {"voice_id": voice_state.voice_id}
            if voice_state is not None
            else None
        ),
    }
    return web.json_response(out)


async def post_test(request: web.Request) -> web.Response:
    """Synthesize a short string via the configured TTS engine.

    Body: `{"text": "...", "voice_id"?: "..."}`.
    Defaults to "Hello, this is TARS." with the active voice_state.
    Returns JSON with `audio_b64` + `provider` + `byte_count` so the
    caller can decode locally without a separate Content-Type negotiation.
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

    text = (body.get("text") if isinstance(body, dict) else None) or "Hello, this is TARS."
    if not isinstance(text, str):
        return web.json_response({"error": "text must be a string"}, status=400)
    if len(text) > _TEST_MAX_CHARS:
        return web.json_response(
            {"error": f"text exceeds {_TEST_MAX_CHARS}-char smoke-test limit"},
            status=400,
        )

    voice_state = request.app.get("voice_state")
    from tesseract.voice import VoiceParams

    voice_id_override = body.get("voice_id") if isinstance(body, dict) else None

    base_voice_id = (
        voice_id_override
        if isinstance(voice_id_override, str) and voice_id_override
        else (voice_state.voice_id if voice_state is not None else "Charon")
    )
    params = VoiceParams(voice_id=base_voice_id)

    try:
        audio, provider = await engine.synthesize(text, params)
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
