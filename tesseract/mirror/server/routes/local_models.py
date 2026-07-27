from __future__ import annotations

from aiohttp import web


async def whisper_status(request: web.Request) -> web.Response:
    engine = request.app.get("stt_engine")
    if engine is None or not hasattr(engine, "local_status"):
        return web.json_response({
            "configured": False,
            "provider": "",
            "model": "",
            "device": "",
            "compute_type": "",
            "language": None,
            "timeout_seconds": None,
            "preload": False,
            "disabled": False,
            "disabled_reason": "",
            "loaded": False,
            "cached": [],
        })
    return web.json_response(engine.local_status())


async def whisper_action(request: web.Request) -> web.Response:
    engine = request.app.get("stt_engine")
    if engine is None or not hasattr(engine, "unload_local"):
        return web.json_response({"error": "local whisper unavailable"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    action = body.get("action")
    if action != "unload":
        return web.json_response({"error": "action must be 'unload'"}, status=400)
    engine.unload_local()
    return web.json_response({"ok": True, "message": "local Whisper model cache cleared"})


async def piper_status(request: web.Request) -> web.Response:
    engine = request.app.get("tts_engine")
    if engine is None or not hasattr(engine, "piper_status"):
        return web.json_response({
            "configured": False,
            "model_path": "",
            "config_path": "",
            "sample_rate": None,
            "preload": False,
            "presets": [],
            "disabled": False,
            "disabled_reason": "",
            "loaded": False,
            "cached": [],
            "provider_key": "",
        })
    return web.json_response(engine.piper_status())


async def piper_action(request: web.Request) -> web.Response:
    engine = request.app.get("tts_engine")
    if engine is None or not hasattr(engine, "unload_piper"):
        return web.json_response({"error": "local Piper unavailable"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    action = body.get("action")
    if action == "unload":
        engine.unload_piper()
        return web.json_response({"ok": True, "message": "local Piper model cache cleared"})
    if action == "warm":
        try:
            await engine.warm_up_piper()
        except Exception as exc:
            return web.json_response(
                {"error": f"piper warm-up failed: {exc}"},
                status=503,
            )
        return web.json_response({"ok": True, "message": "local Piper voice warmed"})
    return web.json_response({"error": "action must be 'unload' or 'warm'"}, status=400)


async def kokoro_status(request: web.Request) -> web.Response:
    engine = request.app.get("tts_engine")
    if engine is None or not hasattr(engine, "kokoro_status"):
        return web.json_response({
            "configured": False,
            "model_path": "",
            "voices_path": "",
            "mix": {},
            "lang": "",
            "device": "",
            "sample_rate": None,
            "preload": False,
            "presets": [],
            "disabled": False,
            "disabled_reason": "",
            "loaded": False,
            "cached": [],
            "provider_key": "",
        })
    return web.json_response(engine.kokoro_status())


async def kokoro_action(request: web.Request) -> web.Response:
    engine = request.app.get("tts_engine")
    if engine is None or not hasattr(engine, "unload_kokoro"):
        return web.json_response({"error": "local Kokoro unavailable"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    action = body.get("action")
    if action == "unload":
        engine.unload_kokoro()
        return web.json_response({"ok": True, "message": "local Kokoro model cache cleared"})
    if action == "warm":
        try:
            await engine.warm_up_kokoro()
        except Exception as exc:
            return web.json_response(
                {"error": f"kokoro warm-up failed: {exc}"},
                status=503,
            )
        return web.json_response({"ok": True, "message": "local Kokoro voice warmed"})
    return web.json_response({"error": "action must be 'unload' or 'warm'"}, status=400)
