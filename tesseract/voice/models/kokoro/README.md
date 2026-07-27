# Kokoro voice models

Kokoro-82M is the candidate replacement for Piper as TARS's local-first
TTS engine. Single ONNX model, voice = small style embedding, supports
linear blending, runs faster than real-time on CPU.

## Files

| File | Size | Source |
|------|------|--------|
| `kokoro-v1.0.onnx` | 311 MB | [github.com/thewh1teagle/kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx) |
| `voices-v1.0.bin` | 27 MB | same release: [voices-v1.0.bin](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin) |

54 voices bundled in the single `.bin`. Output is **24 kHz mono float32**.

## Usage

```python
from kokoro_onnx import Kokoro

kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")

# (a) named voice
samples, sr = kokoro.create(
    "Mission status: nominal.",
    voice="bm_george",   # voice ID
    speed=1.0,           # 0.5 – 2.0 typical
    lang="en-gb",        # en-us / en-gb / ja / zh / es / fr / hi / it / pt
)

# (b) blended voice — linear combination of style embeddings
import numpy as np
g = kokoro.get_voice_style("bm_george")
l = kokoro.get_voice_style("bm_lewis")
charon = 0.6 * g + 0.4 * l
samples, sr = kokoro.create("...", voice=charon, speed=1.0, lang="en-gb")
```

`create()` returns `(np.ndarray[float32], 24000)`. `kokoro.get_voices()`
returns the full ID list. `create_stream()` is the async generator
variant for streaming chunks.

## Voice IDs (relevant subset)

**British male — TARS candidates:** `bm_daniel`, `bm_fable`, `bm_george`,
`bm_lewis`.
**British female:** `bf_alice`, `bf_emma`, `bf_isabella`, `bf_lily`.
**American male:** `am_adam`, `am_echo`, `am_eric`, `am_fenrir`, `am_liam`,
`am_michael`, `am_onyx`, `am_puck`, `am_santa`.

Other languages: Japanese `j*_*`, Mandarin `z*_*`, Spanish `e*_*`, French
`ff_siwis`, Hindi `h*_*`, Italian `i*_*`, Brazilian Portuguese `p*_*`.

## Kokoro vs Piper

| | **Kokoro** | **Piper** |
|---|---|---|
| Model count | one 311 MB ONNX, 54 voices | one ONNX per voice (~64 MB each) |
| Voice = | style embedding (~500 KB, blendable) | baked-in model |
| Output | 24 kHz | 16 / 22 kHz (per voice) |
| Naturalness | meaningfully better — clearer prosody, less "robotic" | adequate, distinctly synthetic on long sentences |
| CPU realtime factor | **~0.8 – 1.0×** (on RTX 2070 box, CPU-only) | ~0.1 – 0.3× (CPU, ONNX) |
| Voice blending | yes — linear combination of style vectors | no |
| Multilingual | yes (10 languages, one model) | no — separate models per language |
| Dependency footprint | ~400 MB install (onnxruntime + phonemizer-fork) | ~50 MB install (piper-tts) |
| Already wired in TARS? | not yet — probe under `tesseract/scratch/kokoro_probe/` | yes — `tesseract/voice/providers/piper_tts.py` |

Both are local, free, offline, no API key. The cost of upgrading is
~350 MB more on disk + the phonemizer dependency.

## What we CAN control

| Lever | How | Notes |
|---|---|---|
| **Voice** | `voice="bm_george"` | 54 stock voices, swap by ID |
| **Voice design** | `voice = w1*style_a + w2*style_b + …` | linear blend of style embeddings; weights need not sum to 1, but staying near 1 keeps amplitude sane |
| **Speed** | `speed=0.85` (slow) … `1.15` (fast) | inference-time, doesn't re-synthesize — repitches/repaces |
| **Language / accent** | `lang="en-gb"` vs `"en-us"` | affects phonemization |
| **Prosody via punctuation** | `.` `,` `—` `!` `?` | meaningful effect — exclamations get energy, dashes get pauses, questions get rising intonation |
| **Phoneme override** | `is_phonemes=True` + IPA input | bypasses text-to-phoneme; useful for pronunciation lock-ins |
| **Streaming** | `create_stream(...)` async generator | chunked synthesis for live playback |

## What we CANNOT control

| Missing | Workaround |
|---|---|
| Natural-language emotion (`"say this surprised"`) | not supported. Use punctuation phrasing for prosody, or route emotion-needing lines to a different engine (ElevenLabs, OpenAI gpt-4o-mini-tts) |
| Per-utterance mood (calm → urgent mid-sentence) | not supported. Voice + speed are session-level knobs |
| Reference-audio voice cloning | not supported. Lock the timbre via blending, not by feeding a clip |
| SSML tags | not supported. Use punctuation only |
| Pitch shift independent of speed | not exposed. Post-process the WAV if needed |
| Whisper / shout / specific affect | not supported. Stay-in-tone delivery only |

## TARS Charon-male-British seed

`bm_george` is the closest single-voice match (grounded male British
baritone). `0.6 * bm_george + 0.4 * bm_lewis` adds warmth without losing
the authority. Lock the chosen blend as a `np.float32` file under
`tesseract/voice/models/kokoro/voices/charon.npy` once we settle.

## Probe

Listen to `tesseract/scratch/kokoro_probe/out/` — 9 WAVs covering the
four British male voices, two design blends, two speed variants, and a
prosody (exclamation) variant. Probe code: `tesseract/scratch/kokoro_probe/probe.py`.

Wiring into the runtime (replacing or sitting alongside Piper) is a
follow-up — touches `tesseract/voice/providers/`, `roles.yaml`, and
`providers.yaml`. Not done yet.
