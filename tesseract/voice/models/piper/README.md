# Piper voice models

Drop the ONNX file + its sibling `.onnx.json` config in this directory.

Default wired voice (see `tesseract/config/providers.yaml`):

- `en_GB-northern_english_male-medium.onnx`
- `en_GB-northern_english_male-medium.onnx.json`

Source: <https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_GB/northern_english_male/medium>

After download, restart the Mirror — `_build_voice_runtime` lazy-loads
the voice on first synth.

Other voices: edit `tesseract/config/providers.yaml::local.piper.<id>.model`
to point at a different ONNX filename and add the corresponding files
here.
