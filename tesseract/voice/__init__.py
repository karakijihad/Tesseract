"""Voice subsystem — STT + TTS over the chains named in `roles.yaml`.

Mirror's WS handler calls `STTEngine.transcribe_stream` for mic audio
and `TTSEngine.synthesize` for response audio. Both engines walk their
configured chain and know nothing about which vendor fills it; the
adapters live in `voice/providers/`. What ships by default is local on
both sides, so a fresh install listens and speaks with no key. Hitting a
daily cap raises `BudgetExhausted`; the WS handler surfaces the cause
via `voice_instruction`.

Public symbols:
    STTEngine          — transcription chain
    TTSEngine          — synthesis chain
    NoTTSLaneAvailable — chain exhausted; caller degrades to text
"""

from tesseract.voice.stt import STTEngine
from tesseract.voice.tts import NoTTSLaneAvailable, TTSEngine

__all__ = [
    "STTEngine",
    "TTSEngine",
    "NoTTSLaneAvailable",
]
