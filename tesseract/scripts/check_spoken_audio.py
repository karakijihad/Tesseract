"""Closed-loop live check for the `<spoken>` contract — MANUAL gate.

Not a unit test and deliberately not in CI: it spends a real chat_brain
call and needs the configured TTS lane's model files plus a Whisper model
on disk. Run it by hand after any change to the output contract, the
stream parser, or the TTS gate.

    python -m tesseract.scripts.check_spoken_audio

Exit 0 = the spoken line was heard and the answer was not read aloud.

model -> parser -> TTS gate -> real synthesis -> audio -> local Whisper
-> text. Then assert the operator would have HEARD the spoken line and not
the answer.

Everything before this proved the gate condition. Nothing produced audio.
This produces audio and reads it back, so "the answer is muted" stops being
an assertion about a boolean and becomes an assertion about sound.

Writes the captured WAV next to this script so the last mile (does it sound
right) can be confirmed by ear separately.
"""

from __future__ import annotations

import asyncio
import io
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from tesseract.env_file import INTERPOLATE  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env", interpolate=INTERPOLATE)

from tesseract.brain.boot import build_adapter, load_chat_brain_config  # noqa: E402
from tesseract.brain.prompt import assemble_system_prompt  # noqa: E402
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType  # noqa: E402
from tesseract.mirror.server.stream_parser import _split_text_for_surfaces  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "runtime" / "voice-checks"
QUESTION = (
    "Answer from your own knowledge only — no tools are available in this "
    "turn, so do not attempt any tool call. Explain how a cross-encoder "
    "reranker fits into a hybrid memory retrieval pipeline, why reciprocal "
    "rank fusion alone is not enough, and what the latency trade-off is. "
    "Be thorough — several paragraphs."
)
# Without the no-tools clause the model emits a textual pseudo tool-call,
# which lands as untagged text BEFORE any <spoken> block. Untagged degrades
# to `answer`, and an answer arriving before the latch is set is spoken by
# design — so the artifact pollutes the audio and the transcript with the
# harness's own limitation rather than anything about the contract.


@dataclass
class _Carry:
    stream_status_buffer: str = ""
    stream_tag_state: str = "outside"
    stream_untagged_warned: bool = False


class _Sess:
    """Minimal session the parser + TTS gate accept."""

    session_id = "live-audio-loop"
    voice_mode = "speak"
    current_turn_task = object()
    chat_session = None
    tts_buffer = ""
    tts_buffer_kind = "answer"
    tts_spoken_seen = False
    tts_sequence = 0
    tts_synth_task = None
    tts_failure_notified = False

    def __init__(self) -> None:
        self.turn_states_by_chat: dict = {}


def _build_engines():
    """Real STT + TTS via the production builder, so this exercises the same
    wiring the backend uses rather than a hand-rolled config."""
    from tesseract.mirror.server.app import _build_voice_runtime

    # `_warmup_tasks` is seeded by the real app factory; the builder schedules
    # model warm-ups into it.
    app: dict = {"cost_ledger": None, "_warmup_tasks": []}
    _build_voice_runtime(app)  # type: ignore[arg-type]
    return app


def _concat_wavs(chunks: list[bytes]) -> tuple[bytes, float]:
    """Join per-sentence WAVs into one, returning (wav_bytes, seconds).

    The lanes emit a complete WAV per synth call; naive concatenation would
    embed headers mid-stream, so the PCM is unwrapped and rewrapped once.
    """
    frames: list[bytes] = []
    params = None
    for raw in chunks:
        if not raw:
            continue
        with wave.open(io.BytesIO(raw), "rb") as w:
            params = params or w.getparams()
            frames.append(w.readframes(w.getnframes()))
    if params is None:
        return b"", 0.0
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(b"".join(frames))
    pcm = b"".join(frames)
    seconds = len(pcm) / (params.framerate * params.nchannels * params.sampwidth)
    return out.getvalue(), seconds


async def main() -> int:
    cfg = load_chat_brain_config()
    app = _build_engines()
    engine = app.get("tts_engine")
    stt = app.get("stt_engine")
    if engine is None:
        print("!! no TTS engine — voice block missing from roles.yaml")
        return 2
    print(f"chat_brain : {cfg.tier}.{cfg.provider} {cfg.model}")
    print(f"tts        : {getattr(engine, 'provider_key', '?')}")
    print(f"stt        : {'present' if stt else 'MISSING'}")

    # --- 1. a real turn from the real role, through the real parser -------
    system_prompt = assemble_system_prompt()
    if "<spoken>" not in system_prompt:
        print("!! assembled prompt carries no <spoken> contract")
        return 2

    adapter = build_adapter(cfg.ref)
    opts = AdapterOptions(
        model=cfg.model, provider=cfg.provider, role="chat_brain", tier=cfg.tier,
        temperature=cfg.temperature, max_output_tokens=cfg.max_output_tokens,
        context_window=cfg.context_window, reasoning_effort=cfg.reasoning_effort,
        knowledge_cutoff=cfg.knowledge_cutoff, use_responses_api=cfg.use_responses_api,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": QUESTION},
    ]

    sess, carry = _Sess(), _Carry()
    by_kind: dict[str, list[str]] = {}
    pieces_in_order: list[tuple[str, str]] = []
    async for chunk in adapter.stream(messages, options=opts):
        if chunk.type is ChunkType.ERROR:
            print(f"!! adapter error: {chunk.error}")
            return 2
        if chunk.type is not ChunkType.TEXT or not chunk.text:
            continue
        for kind, text in _split_text_for_surfaces(sess, carry, chunk.text):
            by_kind.setdefault(kind, []).append(text)
            pieces_in_order.append((kind, text))

    joined = {k: "".join(v).strip() for k, v in by_kind.items()}
    if not joined.get("spoken"):
        print("!! model emitted no <spoken> block — cannot test the mute path")
        print(f"   surfaces: {sorted(joined)}")
        return 1
    print(f"\nsurfaces   : {sorted(joined)}")
    print(f"intent     : {len(joined.get('intent',''))} chars")
    print(f"spoken     : {len(joined['spoken'])} chars")
    print(f"answer     : {len(joined.get('answer',''))} chars")

    # --- 2. replay through the REAL TTS gate with the REAL engine ---------
    from tesseract.mirror.server import tts as tts_mod

    captured: list[bytes] = []
    original_emit = tts_mod._emit_tts_chunk

    async def _capture(session, audio_bytes, provider, *, is_final):  # noqa: ARG001
        if audio_bytes:
            captured.append(audio_bytes)

    async def _sink(session, envelope):  # noqa: ARG001
        """The terminator emits its is_final envelope straight through
        send_envelope, which wants a real session with an event log."""

    tts_mod._emit_tts_chunk = _capture  # type: ignore[assignment]
    tts_mod.send_envelope = _sink  # type: ignore[assignment]
    tts_mod.tts_suppressed = lambda _s: False  # type: ignore[assignment]

    # `_spawn_tracked` normally schedules synthesis; run it inline so the
    # ordering is deterministic and every chunk is captured before we stop.
    from tesseract.mirror.server import ws as ws_mod
    pending: list = []
    ws_mod._spawn_tracked = lambda app_, coro, name="": pending.append(  # type: ignore[assignment]
        asyncio.ensure_future(coro)
    ) or pending[-1]

    app_for_tts = {"tts_engine": engine, "cost_ledger": None}
    for kind, text in pieces_in_order:
        await tts_mod._maybe_emit_tts_sentences(app_for_tts, sess, text, kind=kind)
    while pending:
        batch = list(pending)
        pending.clear()
        await asyncio.gather(*batch, return_exceptions=True)
    await tts_mod._flush_tts_terminator(app_for_tts, sess, succeeded=True)
    tts_mod._emit_tts_chunk = original_emit  # type: ignore[assignment]

    wav_bytes, seconds = _concat_wavs(captured)
    if not wav_bytes:
        print("!! no audio produced — the TTS lane may be unavailable")
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "spoken_contract_live.wav"
    out_path.write_bytes(wav_bytes)
    print(f"\naudio      : {len(captured)} chunk(s), {seconds:.1f}s -> {out_path.name}")

    # A rough spoken-rate sanity bound: the answer alone would run minutes.
    est_answer_s = len(joined.get("answer", "")) / 15.0
    print(f"             (answer alone would be ~{est_answer_s:.0f}s at ~15 chars/s)")

    # --- 3. read the audio back with local Whisper ------------------------
    if stt is None:
        print("!! no STT engine — cannot close the loop")
        return 1
    transcript_parts: list[str] = []
    async for text, is_final in stt.transcribe_stream(wav_bytes):
        if is_final and text:
            transcript_parts.append(text)
    transcript = " ".join(transcript_parts).strip()
    print(f"\ntranscript : {transcript[:400]}")

    # --- 4. verdict --------------------------------------------------------
    def _words(s: str) -> set[str]:
        return {w.strip(".,;:!?()[]\"'").lower() for w in s.split() if len(w) > 6}

    spoken_w, answer_w, heard_w = (
        _words(joined["spoken"]), _words(joined.get("answer", "")), _words(transcript),
    )
    answer_only = answer_w - spoken_w
    spoken_hit = len(spoken_w & heard_w) / max(len(spoken_w), 1)
    leak = (answer_only & heard_w)
    leak_ratio = len(leak) / max(len(answer_only), 1)

    print(f"\n{'=' * 66}\nVERDICT")
    print(f"  spoken words heard      : {spoken_hit:.0%}")
    print(f"  answer-only words heard : {leak_ratio:.1%} ({len(leak)} of {len(answer_only)})")
    if leak:
        print(f"  leaked sample           : {sorted(leak)[:8]}")

    ok = spoken_hit >= 0.5 and leak_ratio <= 0.10
    if ok:
        print("\n  PASS — the spoken line was heard; the answer was not read aloud")
    else:
        why = []
        if spoken_hit < 0.5:
            why.append("the spoken line was not heard")
        if leak_ratio > 0.10:
            why.append("the answer leaked into audio")
        print(f"\n  FAIL — {' and '.join(why)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
