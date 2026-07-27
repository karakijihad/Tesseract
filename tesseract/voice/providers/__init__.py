"""Voice providers — concrete cloud + local STT/TTS implementations.

Each module exposes one async callable that returns audio bytes (TTS) or
yields `(text, is_final)` pairs (STT). The engines in `tesseract/voice/`
own provider selection + fallback.
"""
