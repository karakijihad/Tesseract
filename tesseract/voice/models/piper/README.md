# Piper voice models

Drop the ONNX file + its sibling `.onnx.json` config in this directory.
One model file per voice — unlike the Kokoro lane next door, there is no
shared model and no blending.

Catalogued voices (see `tesseract/config/providers.yaml::local.piper`):

- `en_US-hfc_female-medium.onnx` + `.onnx.json`
- `en_US-hfc_male-medium.onnx` + `.onnx.json`

Source: <https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US>

`python -m tesseract.scripts.fetch_piper_voice` downloads every voice
`roles.yaml::voice.tts` names — primary and fallbacks — from the pinned
upstream commit in each entry's `download:` block, verifying each file's
sha256 before installing it. `--voice <id>` fetches one by catalog id.

After a download, restart the Mirror — `_build_voice_runtime` lazy-loads
the voice on first synth.

Adding a voice: add an entry under `local.piper.models` naming its `.onnx`
filename, and give it a `download:` block with a revision-pinned `base_url`
and a `files:` map of filename → sha256. No code change — the fetch script
reads the catalog.
