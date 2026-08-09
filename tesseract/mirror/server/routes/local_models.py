"""Settings → Local models: status, load/unload, and model-file downloads.

The download half exists because first-run setup lets the operator decline a
lane, and declining must be reversible without a reinstall. Enabling the
provider again (Settings → Capabilities) restores the lane, but its model
files were never fetched — so the lane would latch on first use with nothing
in the UI explaining why. `files_present` on each status is what surfaces
that, and `POST /api/system/models/download` is what fixes it.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

log = logging.getLogger(__name__)

# lane -> (fetcher, presence probe, what to call it in a message). Both
# callables are the ones `provision.rs` runs, so a download from Settings and
# a download during first run cannot diverge in what they fetch or verify.
_MODEL_LANES: dict[str, tuple] = {}


def _lanes() -> dict[str, tuple]:
    """Resolved lazily: importing the fetch scripts pulls in the config
    loader, and this module is imported at route-registration time on every
    boot, including ones that never open Settings."""
    if not _MODEL_LANES:
        from tesseract.scripts import (
            fetch_kokoro_voice,
            fetch_piper_voice,
            fetch_whisper_model,
        )

        _MODEL_LANES.update(
            whisper=(
                fetch_whisper_model.ensure_whisper_model,
                fetch_whisper_model.snapshot_present,
                "speech recognition model",
            ),
            kokoro=(
                fetch_kokoro_voice.ensure_kokoro_models,
                fetch_kokoro_voice.models_present,
                "Kokoro voice model",
            ),
            piper=(
                fetch_piper_voice.ensure_configured_voices,
                fetch_piper_voice.voices_present,
                "Piper voice model",
            ),
        )
    return _MODEL_LANES


def _download_state(app) -> dict:
    return app.setdefault("model_downloads", {})


def _present(lane: str) -> bool | None:
    """Never raises: a broken catalog must not blank the whole panel, which
    is the one screen an operator opens to find out what is wrong."""
    try:
        return _lanes()[lane][1]()
    except Exception:  # noqa: BLE001
        log.exception("local models: presence probe failed for %s", lane)
        return None


async def _download_fields(app, lane: str) -> dict:
    """The presence probe reads the catalog to resolve the lane's pin, so it
    runs off the loop: Settings polls these three routes every 30s, and sync
    yaml parsing on the event loop is what makes health checks and WS
    heartbeats miss their deadlines."""
    state = _download_state(app).get(lane) or {}
    return {
        "files_present": await asyncio.to_thread(_present, lane),
        "downloading": bool(state.get("running")),
        "download_error": str(state.get("error") or ""),
    }


async def _run_download(app, lane: str) -> None:
    fetch, _probe, label = _lanes()[lane]
    state = _download_state(app)[lane]
    try:
        ok = await asyncio.to_thread(fetch)
        # The fetchers never raise; `False` means offline, a bad pin, or a
        # checksum mismatch, each already logged with its reason.
        state["error"] = "" if ok else f"could not download the {label} — see the log"
    except Exception as exc:  # noqa: BLE001
        log.exception("local models: %s download failed", lane)
        state["error"] = str(exc)
    finally:
        state["running"] = False


async def model_download(request: web.Request) -> web.Response:
    """POST /api/system/models/download — fetch one lane's model files.

    Body: ``{"lane": "whisper" | "kokoro" | "piper"}``.

    Returns once the work is SCHEDULED, matching the Ollama install route:
    the Whisper snapshot alone is 1.6 GB, and holding the request open for it
    would turn any client timeout into a false failure report. `downloading`
    and `download_error` on the lane's status carry the outcome.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    lane = (body or {}).get("lane")
    if lane not in _lanes():
        return web.json_response(
            {"error": f"lane must be one of {', '.join(sorted(_lanes()))}"}, status=400
        )

    state = _download_state(request.app).setdefault(lane, {"running": False, "error": ""})
    if state["running"]:
        return web.json_response(
            {"error": f"a {lane} download is already running"}, status=409
        )
    state.update(running=True, error="")
    asyncio.create_task(_run_download(request.app, lane))
    return web.json_response({"ok": True, **await _download_fields(request.app, lane)})


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
            **await _download_fields(request.app, "whisper"),
        })
    return web.json_response({**engine.local_status(), **await _download_fields(request.app, "whisper")})


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
            **await _download_fields(request.app, "piper"),
        })
    return web.json_response({**engine.piper_status(), **await _download_fields(request.app, "piper")})


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
            **await _download_fields(request.app, "kokoro"),
        })
    return web.json_response({**engine.kokoro_status(), **await _download_fields(request.app, "kokoro")})


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
