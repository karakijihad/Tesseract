"""Wake-word gate for the voice-input path.

The gate answers one question: did this utterance address the assistant by
name? It runs after the utterance is committed, for the mic modes that
dispatch a turn (``command`` / ``speak``). ``transcribe`` and ``terminal`` are
never gated — those modes hand the text to the operator, not to the brain.

**It listens to the audio, not to the transcript.** The phrase is spotted as
sound by a decoder restricted to those words (``voice/wake_spotter.py``). That
is the whole reason this exists in its current shape: the previous gate
fuzzy-matched the *text* Whisper produced, which meant it inherited every
spelling decision the speech engine made about a name it had never seen. No
threshold can fix that, and widening one buys false positives to pay for false
negatives.

Three states, and they are deliberately different things:

- **Off** (``enabled: false``) — every utterance dispatches.
- **On but unconfirmed** — every utterance dispatches. ``enabled`` is
  permission, not readiness; arming waits for the operator to have heard it
  fire once, so shipping the gate on can never leave a fresh install unable to
  hear anyone.
- **On and confirmed** — the utterance is decoded at the confirmed
  sensitivity, and one the decoder does not hear the phrase in is discarded.

Everything that can go wrong resolves to *dispatch*. A wake word that cannot
load its models, cannot read its calibration, or cannot score an utterance
must never become a mute — the operator would experience a dead microphone
with nothing to read. Only a confident, measured miss discards.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool
    prefix: str
    min_threshold: float
    boost: float


@dataclass(frozen=True)
class WakeWordDecision:
    """`matched` is the whole verdict, and there is nothing else to carry.

    No score: the decoder emits the keyword or it emits nothing, so there is
    no confidence number to report — sensitivity is set before decoding, not
    compared afterwards. No text either: the gate reads audio and runs
    *before* transcription, so there is no transcript and nothing to strip a
    phrase out of.
    """

    matched: bool


# ── Config ───────────────────────────────────────────────────────────


def parse_wake_word_config(identity: Mapping[str, Any], path: Path) -> WakeWordConfig:
    """Read ``identity.wake_word`` out of a parsed mirror.yaml.

    Every key is required — the threshold in particular has no in-source
    default, so a config missing it fails at load with the file named
    rather than silently gating on a number nobody chose.
    """
    where = f"{path} identity.wake_word"
    block = identity.get("wake_word")
    if not isinstance(block, dict):
        raise RuntimeError(f"{path} missing required 'identity.wake_word' block")
    for key in ("enabled", "prefix", "min_threshold", "boost"):
        if key not in block:
            raise RuntimeError(f"{where} missing required key: {key}")
    # Typed, not coerced. Every one of these is a YAML scalar an operator can
    # get subtly wrong, and coercion turns each mistake into a silent
    # behaviour change instead of an error:
    #   enabled: "false"      -> bool("false") is True  (gate silently ON)
    #   prefix: null          -> str(None) is "None"    (phrase becomes "None …")
    #   min_threshold: true   -> float(True) is 1.0     (nothing ever wakes it)
    # The last two pass every downstream check, so nothing else would catch them.
    if not isinstance(block["enabled"], bool):
        raise RuntimeError(
            f"{where}.enabled must be a boolean (true/false, unquoted), "
            f"got {block['enabled']!r}"
        )
    raw_prefix = block["prefix"]
    if not isinstance(raw_prefix, str):
        raise RuntimeError(
            f"{where}.prefix must be a string, got {raw_prefix!r}"
        )
    prefix = raw_prefix.strip()
    if not prefix:
        raise RuntimeError(f"{where}.prefix must be a non-empty string")
    def _number(key: str, upper: float) -> float:
        raw = block[key]
        # bool is an int subclass, so it must be refused before the numeric check.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise RuntimeError(f"{where}.{key} must be a number, got {raw!r}")
        value = float(raw)
        if not (0.0 < value <= upper):
            raise RuntimeError(f"{where}.{key} must be in (0.0, {upper}], got {value}")
        return value

    return WakeWordConfig(
        enabled=block["enabled"],
        prefix=prefix,
        min_threshold=_number("min_threshold", 1.0),
        boost=_number("boost", 10.0),
    )


def wake_phrase(config: Any) -> str:
    """The two words the gate was calibrated for, from live config.

    Built here rather than stored, so a rename is visible immediately — and
    so the calibration can be checked against it and invalidated when it no
    longer describes what was recorded.
    """
    wake = getattr(config, "wake_word", None)
    prefix = getattr(wake, "prefix", "") if wake else ""
    name = str(getattr(config, "entity_name", "") or "").strip()
    return f"{prefix} {name}".strip()


# ── Fail-open reporting ──────────────────────────────────────────────

_fail_open_logged: set[str] = set()


def _log_fail_open(reason: str) -> None:
    """One ERROR per distinct fault, not one per utterance.

    The gate runs on every utterance, so an unlatched log turns a standing
    misconfiguration into a repeating pulse row — the log forwarder's
    one-second dedupe only collapses utterances spoken back to back. A
    fault that repeats forever reads as noise; a fault stated once reads
    as a fault."""
    if reason in _fail_open_logged:
        return
    _fail_open_logged.add(reason)
    log.error(
        "wake_word: %s — gate disabled until this is fixed (logged once)", reason
    )


def reset_fail_open_log() -> None:
    """Test hook — the latch is process-wide by design, which would
    otherwise make the second test asserting on it order-dependent."""
    _fail_open_logged.clear()


# ── The gate ─────────────────────────────────────────────────────────


def _dispatch() -> WakeWordDecision:
    """Every failure resolves here. Named so each call site reads as the
    decision it is rather than as a constructor."""
    return WakeWordDecision(matched=True)


#: Cached in place of a spotter once a load has failed. Without it a broken
#: install rebuilds the ONNX sessions on EVERY utterance, and that constructor
#: holds the GIL for the length of the load — P1 measured 4.64 s of event-loop
#: lag for a 4.74 s load. A gate that cannot work must be cheap, not expensive.
_LOAD_FAILED = object()


def spotter_blocking(app: Any, key: Any) -> Any | None:
    """The process-wide spotter for `key`, built on first use and cached.

    **Called only from a worker thread.** Construction is the expensive,
    GIL-holding part; doing it inline would stall health probes and the WS
    heartbeat on the first gated utterance after every launch.

    Keyed on the phrase and both sensitivity parameters because sherpa bakes
    all three in at construction — so a rename or a re-run of calibration
    builds a new spotter rather than silently keeping the old one, and that is
    what makes "a rename re-teaches the gate with no restart" true.

    Not built at boot either: the model may be absent on a fresh install and
    arrive later without a restart, so absence is re-checked (cheaply, five
    stats) while a genuine load *failure* latches against that key.
    """
    from tesseract.voice.wake_spotter import load_spotter, models_present

    cached = app.get("wake_spotter") if hasattr(app, "get") else None
    if cached is not None and cached[0] == key:
        return None if cached[1] is _LOAD_FAILED else cached[1]
    if not models_present():
        return None
    spotter = load_spotter(key)
    app["wake_spotter"] = (key, spotter if spotter is not None else _LOAD_FAILED)
    return spotter


def _calibration(app: Any) -> Any | None:
    """The stored reference, re-read when the file changes.

    Cached on mtime rather than read per utterance: re-recording must take
    effect without a restart, but parsing a JSON blob on every commit is work
    the voice path does not need to repeat.
    """
    from tesseract.voice import wake_calibration

    path = wake_calibration.calibration_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        app.pop("wake_calibration", None) if hasattr(app, "pop") else None
        return None
    cached = app.get("wake_calibration") if hasattr(app, "get") else None
    if cached is not None and cached[0] == stamp:
        return cached[1]
    loaded = wake_calibration.load(path)
    app["wake_calibration"] = (stamp, loaded)
    return loaded


def _spotter_key(app: Any) -> Any | None:
    """What the gate would decode this session's audio with, or None when it
    would not gate at all. Shared by the live feed and the commit verdict so
    the two can never disagree about whether the gate is active."""
    from tesseract.voice import wake_calibration
    from tesseract.voice.wake_spotter import SpotterKey

    config = app.get("config") if hasattr(app, "get") else None
    wake = getattr(config, "wake_word", None)
    if not isinstance(wake, WakeWordConfig) or not wake.enabled:
        return None
    phrase = wake_phrase(config)
    if not phrase or not str(getattr(config, "entity_name", "") or "").strip():
        return None
    calibration = _calibration(app)
    if calibration is None or not calibration.matches_phrase(phrase):
        return None
    return SpotterKey(
        phrase=phrase,
        threshold=max(calibration.threshold, wake.min_threshold),
        boost=wake.boost,
    )


def _cached_spotter(app: Any, key: Any) -> Any | None:
    """The loaded spotter for `key`, or None. Reads the cache and nothing else.

    Separate from `spotter_blocking` because the live feed runs ON the event
    loop, where the only safe question is "is it ready".
    """
    cached = app.get("wake_spotter") if hasattr(app, "get") else None
    if cached is None or cached[0] != key:
        return None
    return None if cached[1] is _LOAD_FAILED else cached[1]


def _start_spotter_load(app: Any, key: Any) -> None:
    """Build the spotter in a worker thread, at most once per key.

    Fire-and-forget: the utterance that triggered it is already through, and
    the next one gates normally. The in-flight marker matters because frames
    arrive every 100 ms — without it, a 20 s load would be started two hundred
    times before the first one finished.
    """
    if not hasattr(app, "get"):
        return
    if app.get("wake_spotter_loading") == key:
        return
    app["wake_spotter_loading"] = key

    async def _load() -> None:
        try:
            await asyncio.to_thread(spotter_blocking, app, key)
        finally:
            if app.get("wake_spotter_loading") == key:
                app["wake_spotter_loading"] = None

    try:
        asyncio.get_running_loop().create_task(_load(), name="wake:load")
    except RuntimeError:
        # No loop (tests calling this synchronously). Nothing to schedule, and
        # nothing is broken — the gate stays open until something loads it.
        app["wake_spotter_loading"] = None


def note_wake_audio(app: Any, session: Any, pcm: bytes) -> bool:
    """Feed one live PCM frame to the decoder. True the FIRST time it fires.

    Returning true-once is the whole point: the caller emits the "heard you"
    envelope on that edge, so the operator learns mid-sentence rather than
    after they stop talking. Everything after the first hit is the same answer.

    Never raises. Every fault leaves `wake_decidable` False, which the commit
    reads as fail-open — a decoder that cannot run must not become a mute.
    """
    if session.wake_fired:
        return False
    key = _spotter_key(app)
    if key is None:
        return False
    try:
        if session.wake_stream is None or getattr(session, "_wake_key", None) != key:
            spotter = _cached_spotter(app, key)
            if spotter is None:
                # Not loaded yet. Start it OFF the loop and let this utterance
                # through undecided — constructing here would block the whole
                # backend for the 9-25 s the load takes, which is why the
                # commit-time gate always crossed a thread to do it. The path
                # that matters is arming from Settings: the calibration is
                # written and the operator speaks immediately, with no restart
                # in between to have warmed anything.
                _start_spotter_load(app, key)
                return False
            session.wake_stream = spotter.new_stream()
            session._wake_key = key
            session._wake_spotter = spotter
        # Decidable from the first frame a live decoder actually saw, not
        # from the first hit — otherwise an utterance that legitimately does
        # not contain the phrase would look like one nothing could decide.
        session.wake_decidable = True
        if session._wake_spotter.feed(session.wake_stream, pcm):
            session.wake_fired = True
            return True
    except Exception as exc:  # noqa: BLE001 - every fault is the same outcome
        log.warning("wake_word: live decode failed (%s) — this utterance fails open", exc)
        session.wake_decidable = False
        session.wake_stream = None
    return False


def reset_wake_stream(session: Any) -> None:
    """Drop the per-utterance decoding state.

    Called when an utterance ends however it ends — committed, cancelled, or
    abandoned. A stream carried into the next utterance would still hold the
    last one's decoder state, so a phrase said once could wake twice.
    """
    session.wake_stream = None
    session.wake_fired = False
    session.wake_decidable = False


def wake_verdict(app: Any, session: Any) -> WakeWordDecision:
    """The gate's answer for the utterance just committed.

    Reads what the live feed already decided rather than decoding again. The
    ordering guarantee is unchanged and slightly stronger: the decision is
    made from audio, before any transcription, and by the time the buffer
    closes it has already been made.
    """
    if _spotter_key(app) is None:
        return _dispatch()
    if not session.wake_decidable:
        # No live decoder saw this audio. Not a miss — an absence of evidence,
        # which must reach the same outcome as every other fault here.
        return _dispatch()
    return WakeWordDecision(matched=bool(session.wake_fired))


async def evaluate_wake_gate(app: Any, audio: bytes) -> WakeWordDecision:
    """Gate one committed utterance for the dispatching mic modes.

    ``audio`` is the raw PCM the session buffered — int16 LE mono at 16 kHz,
    the shape the decoder wants, so nothing is resampled.

    **This runs BEFORE transcription**, which is the whole point of a wake
    word and was impossible under the text gate. Speech that did not address
    the assistant never reaches an STT engine at all — including the cloud
    fallback `roles.yaml` ships by default — and the turn costs no
    transcription it was only going to throw away.

    Nothing is stripped from anything: the caller keeps its own transcript,
    which it obtains afterwards and only if this passed. Re-deriving where a
    phrase ends in text would reintroduce exactly the fuzzy matching this
    rebuild deleted, and addressing someone by name is ordinary language.
    """
    config = app.get("config") if hasattr(app, "get") else None
    wake = getattr(config, "wake_word", None)
    if not isinstance(wake, WakeWordConfig):
        _log_fail_open("no config on ServerConfig")
        return _dispatch()
    if not wake.enabled:
        return _dispatch()

    phrase = wake_phrase(config)
    if not phrase or not str(getattr(config, "entity_name", "") or "").strip():
        _log_fail_open("no entity name on ServerConfig")
        return _dispatch()

    calibration = _calibration(app)
    if calibration is None:
        # Not a fault: `enabled` is permission and this is readiness. Logging
        # it as an error every launch would train the operator to ignore the
        # one line that matters when something is actually broken.
        return _dispatch()
    if not calibration.matches_phrase(phrase):
        _log_fail_open(
            f"calibration was recorded for {calibration.phrase!r} but the phrase "
            f"is now {phrase!r} — re-record it from Settings → Voice"
        )
        return _dispatch()

    from tesseract.voice.wake_spotter import (
        PhraseUnspottable,
        SpotterKey,
        WakeModelsUnavailable,
        WakeUndecidable,
    )

    # The config threshold is a floor the operator can raise, not a second
    # opinion: calibration confirmed what this machine and this voice can
    # actually achieve, so the stricter of the two wins.
    key = SpotterKey(
        phrase=phrase,
        threshold=max(calibration.threshold, wake.min_threshold),
        boost=wake.boost,
    )

    try:
        # Everything expensive happens here, off the loop: building the ONNX
        # sessions the first time, and decoding every time. The worst case is
        # the LONG utterance the gate is most likely to reject — ordinary
        # speech that was never addressed to the assistant — so this stays off
        # the loop regardless of how cheap the common case is.
        matched = await asyncio.to_thread(_spot_blocking, app, audio, key)
    except WakeModelsUnavailable:
        _log_fail_open("the wake model is not installed")
        return _dispatch()
    except PhraseUnspottable as exc:
        _log_fail_open(str(exc))
        return _dispatch()
    except WakeUndecidable as exc:
        log.warning("wake_word: could not decide this utterance (%s) — dispatching", exc)
        return _dispatch()
    except Exception as exc:  # noqa: BLE001 - every failure is the same outcome
        log.exception("wake_word: spotting failed (%s) — dispatching", exc)
        return _dispatch()

    return WakeWordDecision(matched=matched)


def _spot_blocking(app: Any, audio: bytes, key: Any) -> bool:
    """The whole blocking half, in one worker thread."""
    from tesseract.voice.wake_spotter import WakeModelsUnavailable

    spotter = spotter_blocking(app, key)
    if spotter is None:
        raise WakeModelsUnavailable("the wake model is not installed")
    return spotter.spot(audio)
