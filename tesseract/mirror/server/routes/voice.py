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
from tesseract.mirror.server.routes._capability_report import key_present
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

    Cost is one number: dollars per hour of speech, which is how a voice is
    actually priced here and how spend is estimated — speech length times the
    rate. A voice with no `cost_per_audio_hour` is free, and that is the only
    distinction the picker draws.

    The key state is reported rather than used to hide the row: a voice the
    operator could have by adding one line to `.env` is worth naming, and
    naming it is the only way they learn the variable.
    """
    from tesseract.brain.boot import load_bundle

    bundle = load_bundle()
    rows: list[dict[str, Any]] = []
    for ref, conn, model in bundle.all_models():
        if model.kind != _TTS_KIND:
            continue
        fields = model.fields
        key_env = conn.api_key_env or ""
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
            "key_env": key_env,
            # The same check `/api/capabilities` reports with. A
            # whitespace-only value is not a key, and two surfaces
            # disagreeing about whether the operator holds one is worse
            # than either answer.
            "key_present": (not key_env) or key_present(key_env),
            "cost_per_hour_usd": float(fields.get("cost_per_audio_hour") or 0.0),
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
    if not row["key_present"] and lane.get("primary") != ref:
        # Same reasoning as the disabled case: writing this ref would leave a
        # saved selection that raises on its first sentence and latches the
        # lane off, and the operator would read that as a broken voice rather
        # than a missing key.
        #
        # Below the lane read, and skipped when the ref is ALREADY the
        # primary, so the no-op branch keeps its invariant: re-picking the
        # current voice never fails and never writes. A key removed after
        # the voice was chosen is a broken lane to repair, not a request to
        # refuse — refusing it would be the one call that reports a change
        # the config never asked for.
        return web.json_response(
            {
                "error": (
                    f"ref {ref!r} needs {row['key_env']} — add it in "
                    f"Settings → Keys before selecting this voice"
                )
            },
            status=400,
        )
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


# ── Wake-word calibration ────────────────────────────────────────────
#
# The spotter needs no enrollment — it can hear a phrase it was never trained
# on — so these routes are not teaching it anything. They report what state the
# wake word is in, confirm it fires for this voice in this room, and forget
# that confirmation again.
#
# What crosses the wire is audio the operator just recorded, and what comes
# back is a verdict and some counts. **No recording is written to disk and no
# recording is returned** — each is decoded in memory and dropped, and the only
# thing that persists is the phrase and two numbers. That is what makes "your
# voice never leaves the machine" a checkable claim rather than a slogan, and it
# is why this route reads the audio straight out of the request instead of
# staging it through a file.


def _wake_status_payload(app: web.Application) -> dict[str, Any]:
    from tesseract.mirror.server.wake_word import wake_phrase
    from tesseract.voice import wake_calibration
    from tesseract.voice.wake_spotter import models_present

    config = app.get("config")
    wake = getattr(config, "wake_word", None)
    calibration = wake_calibration.load()
    phrase = wake_phrase(config) if config is not None else ""
    stale = bool(calibration and phrase and not calibration.matches_phrase(phrase))
    return {
        # `enabled` is permission; `armed` is readiness. Reporting one number
        # for both is how an operator ends up believing a gate is live when it
        # is passing everything through.
        "enabled": bool(getattr(wake, "enabled", False)),
        "armed": bool(calibration) and not stale,
        "calibrated": bool(calibration),
        "stale": stale,
        "phrase": phrase,
        "calibrated_for": calibration.phrase if calibration else None,
        "samples": calibration.samples if calibration else 0,
        "threshold": calibration.threshold if calibration else None,
        "models_present": models_present(),
    }


async def get_wake(request: web.Request) -> web.Response:
    """What state the wake word is in, in the four ways it can differ."""
    return web.json_response(_wake_status_payload(request.app))


#: Total decoded audio one calibration may carry.
#:
#: Sized against the guided flow rather than against a round number: five
#: takes of the phrase plus three read sentences, with headroom for someone
#: who speaks slowly. 1.4 MB is ~45 s at 16 kHz 16-bit mono, and base64 costs
#: 4 bytes per 3, so the request lands near 1.9 MB — comfortably inside the
#: 4 MiB `app.py::_MAX_REQUEST_BYTES` ceiling, which is what stops the
#: framework answering first with a bare 413 nobody can read.
#:
#: This was 512 KB, sized against aiohttp's 1 MiB default before that ceiling
#: was raised, and the two were never reconciled — so a session that recorded
#: one take too many was refused for a reason that had stopped being true.
MAX_CALIBRATION_AUDIO_BYTES = 1_440_000


def _decode_clips(raw: Any) -> list[bytes]:
    out: list[bytes] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        try:
            out.append(base64.b64decode(item, validate=True))
        except Exception:  # noqa: BLE001 - one bad clip must not lose the rest
            log.warning("voice/wake/calibrate: skipping an undecodable clip")
    return out


def _audio_seconds(total_bytes: int) -> float:
    """16 kHz, 16-bit, mono — the shape the whole voice path uses."""
    return total_bytes / 32_000


def _calibrate_blocking(
    app: web.Application, positives: list[bytes], negatives: list[bytes], phrase: str
) -> tuple[Any, Any]:
    """The ladder, in one worker thread.

    Off the loop for the same reason the gate is, and more so: this builds a
    spotter per rung and decodes every recording against each, so it is the
    most expensive thing the voice surface does.

    Spotters are built directly rather than through the gate's cache. The
    cache holds the one the gate is currently using, and walking a ladder
    through it would evict that and leave the live gate rebuilding at the
    strictest rung the operator happened to fail at.
    """
    from tesseract.voice.wake_calibration import calibrate
    from tesseract.voice.wake_spotter import (
        SpotterKey,
        WakeModelsUnavailable,
        WakeSpotter,
        models_present,
    )

    if not models_present():
        raise WakeModelsUnavailable("the wake model is not installed")

    boost = float(getattr(getattr(app.get("config"), "wake_word", None), "boost", 1.0))

    def make_spotter(threshold: float) -> Any:
        return WakeSpotter(SpotterKey(phrase=phrase, threshold=threshold, boost=boost))

    return calibrate(
        positives, negatives, phrase=phrase, boost=boost, make_spotter=make_spotter
    )


async def post_wake_calibrate(request: web.Request) -> web.Response:
    """Recordings in, a stored reference or a stated refusal out.

    A refusal is a 200 with ``ok: false``, not an error status: the operator
    did nothing wrong, the recordings simply did not separate, and the reason
    is the useful part of the response.
    """
    from tesseract.mirror.server.routes._localhost import is_localhost_request
    from tesseract.mirror.server.wake_word import wake_phrase
    from tesseract.voice import wake_calibration
    from tesseract.voice.wake_spotter import PhraseUnspottable, WakeModelsUnavailable

    # Same-machine only. The bind is the practical gate, but this route
    # OVERWRITES the reference the gate wakes on — a calibration replaced with
    # someone else's voice is a trigger the operator does not control and
    # cannot see. That is worse than a read, which is the line `_localhost.py`
    # draws, and CORS does not cover it: a request with no Origin is allowed
    # through by design because that is what a native client sends.
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost_only"}, status=403)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid_json"}, status=400)

    positives = _decode_clips(body.get("phrase_clips"))
    negatives = _decode_clips(body.get("speech_clips"))
    if not positives:
        return web.json_response({"error": "no_recordings"}, status=400)

    total = sum(len(c) for c in positives) + sum(len(c) for c in negatives)
    if total > MAX_CALIBRATION_AUDIO_BYTES:
        return web.json_response(
            {
                "error": "too_much_audio",
                "reason": (
                    f"{_audio_seconds(total):.0f}s of audio, and calibration "
                    f"takes at most {_audio_seconds(MAX_CALIBRATION_AUDIO_BYTES):.0f}s. "
                    "Record the phrase in short takes and keep the ordinary "
                    "speech to a few sentences."
                ),
                "limit_seconds": round(
                    _audio_seconds(MAX_CALIBRATION_AUDIO_BYTES), 1
                ),
            },
            status=413,
        )

    phrase = wake_phrase(request.app.get("config"))
    if not phrase:
        return web.json_response({"error": "no_phrase_configured"}, status=409)

    try:
        calibration, report = await asyncio.to_thread(
            _calibrate_blocking, request.app, positives, negatives, phrase
        )
    except WakeModelsUnavailable as exc:
        return web.json_response(
            {"error": "models_missing", "reason": str(exc)}, status=409
        )
    except PhraseUnspottable as exc:
        # A configuration fault, not a failed run: the name cannot be built
        # from the model's vocabulary, so no recording could ever pass. Said
        # as a refusal with the reason, like every other thing the operator
        # can fix, rather than as a 500 they would read as a crash.
        return web.json_response({"error": "phrase_unspottable", "reason": str(exc)}, status=409)
    except Exception as exc:  # noqa: BLE001
        log.exception("voice/wake/calibrate failed")
        return web.json_response(
            {"error": "calibration_failed", "reason": str(exc)[:200]}, status=500
        )

    payload: dict[str, Any] = {
        "ok": report.ok,
        "reason": report.reason,
        "threshold": round(report.threshold, 3),
        "phrase_hits": report.phrase_hits,
        "phrase_takes": report.phrase_takes,
        "speech_hits": report.speech_hits,
        "speech_takes": report.speech_takes,
    }
    if calibration is not None:
        wake_calibration.save(calibration)
        # The gate caches the calibration on mtime, so the next utterance
        # picks this up with no restart and no cache poke from here.
        payload["status"] = _wake_status_payload(request.app)
    return web.json_response(payload)


async def delete_wake_calibration(request: web.Request) -> web.Response:
    """Forget the recording. This is the rollback the phase file promises —
    the gate arms on the presence of this file, so removing it returns the
    install to hearing everything, with no config edit and no migration."""
    from tesseract.mirror.server.routes._localhost import is_localhost_request
    from tesseract.voice import wake_calibration

    # Same reasoning as the calibrate route: this is a write, and disarming
    # someone's wake word remotely is not something the bind alone should be
    # the only thing preventing.
    if not is_localhost_request(request):
        return web.json_response({"error": "localhost_only"}, status=403)

    removed = wake_calibration.clear()
    request.app.pop("wake_calibration", None)
    return web.json_response(
        {"removed": removed, "status": _wake_status_payload(request.app)}
    )
