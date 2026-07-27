"""TARS voice subsystem — cloud STT (Gemini Flash audio) + cloud TTS
(Gemini Flash TTS).

Mirror's WS handler calls `STTEngine.transcribe_stream` for mic audio
and `TTSEngine.synthesize` for response audio. Both are cloud-only
Gemini on the same `GOOGLE_API_KEY`. Hitting the daily cap raises
`BudgetExhausted`; the WS handler surfaces the cause via
`voice_instruction`.

Public symbols:
    STTEngine          — Gemini Flash audio transcription
    TTSEngine          — Gemini Flash TTS synthesis
    VoiceParams        — voice_id + tone_prompt (TARS-controlled)
"""

from tesseract.voice.stt import STTEngine
from tesseract.voice.tts import TTSEngine, VoiceParams

__all__ = [
    "STTEngine",
    "TTSEngine",
    "VoiceParams",
]
