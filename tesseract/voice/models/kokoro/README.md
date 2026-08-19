# Kokoro voice models

Kokoro-82M is the default local TTS engine. One ONNX model holds every
voice; a voice is a small style embedding, so switching voices costs
nothing and voices can be blended. Runs at roughly realtime on CPU and
faster on GPU.

Wired at `tesseract/voice/providers/kokoro_tts.py`; the catalog entries
live under `providers.yaml::local.kokoro`, one per selectable voice.

## Files

Both must be present in this directory or the lane latches disabled and
the chain falls through to the next one.

| File | Size | Source |
|------|------|--------|
| `kokoro-v1.0.onnx` | 311 MB | [github.com/thewh1teagle/kokoro-onnx releases](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx) |
| `voices-v1.0.bin` | 27 MB | same release: [voices-v1.0.bin](https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin) |

`python -m tesseract.scripts.fetch_kokoro_voice` downloads both, from the
release-tag-pinned `download:` block on `providers.yaml::local.kokoro`,
verifying each file's sha256 before installing it. The pin sits on the
connection rather than on each voice because every voice below is a `mix`
over these same two files.

54 voices bundled in the single `.bin`. Output is **24 kHz mono float32**.

## Voice ids

`kokoro.get_voices()` returns the full list. The naming is
`<lang><gender>_<name>` — `a` American English, `b` British English, then
`j`/`z`/`e`/`f`/`h`/`i`/`p` for Japanese, Mandarin, Spanish, French,
Hindi, Italian and Brazilian Portuguese; `f` female, `m` male.

The catalog ships the English voices. Adding one is a new entry under
`local.kokoro.models` merging the shared `defaults` anchor with its own
`mix` — no code change.

## Blending

A voice is a style vector, so a weighted sum of two is a third voice:

```yaml
mix: { bm_george: 0.6, bm_lewis: 0.4 }
```

Weights need not sum to 1, but staying near 1 keeps amplitude sane. The
engine resolves and caches the blend once per mix signature.

## Levers

| Lever | Where | Notes |
|---|---|---|
| Voice | `mix` on the catalog entry | one id, or several to blend |
| Speed | `synthesis_presets.<surface>.speed` | inference-time repacing, 0.85–1.15 useful range |
| Trailing pause | `synthesis_presets.<surface>.sentence_silence` | padded after the sentence |
| Language / accent | `lang` on the catalog entry | affects phonemization |
| Prosody | punctuation in the text itself | `.` `,` `—` `!` `?` all have real effect |
| Device | `device` on the catalog entry | `cuda` falls back to CPU cleanly |

Not available at any price: natural-language emotion ("say this
surprised"), mid-sentence mood changes, reference-audio cloning, SSML,
and pitch shift independent of speed. Lock the timbre with a blend, and
shape delivery with punctuation.

## Versus the cloud lane

The cloud lane is what sits behind this one in the chain, for the case no
local model is on disk at all — an install that declined the download, or
a machine with no room for it. Without it, declining the local voice
means no voice.

The trade runs the other way from the files above: the cloud lane
downloads nothing and speaks well on any machine, but it needs an API
key, it is billed per second of speech, and it cannot speak without a
network. Kokoro is free at use-time and works offline once its two files
are here.

Direction is where they differ in kind. Kokoro takes numbers — `speed`,
`sentence_silence` — because a local voice IS its model file. The cloud
lane takes prose: a `style` and a `pace` written as natural language.
Those knobs are not interchangeable, which is why the preset editor is
per-adapter and refuses a value the lane cannot honour.
