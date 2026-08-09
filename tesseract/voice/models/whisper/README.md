# Local Whisper STT models

One subdirectory per CTranslate2 checkpoint, named after the checkpoint:

```
whisper/
  large-v3-turbo/   config.json  model.bin  preprocessor_config.json
                    tokenizer.json  vocabulary.json
```

`python -m tesseract.scripts.fetch_whisper_model` downloads whichever
checkpoint `providers.yaml::local.whisper.local_whisper.model` names, from
the revision-pinned URL in that entry's `downloads:` map, verifying each
file's sha256 before installing it. Checkpoints coexist, so switching the
config back and forth never re-downloads.

Without a snapshot here, `faster-whisper` resolves the bare checkpoint name
by downloading it from HuggingFace at *first transcription* — unpinned, and
paid for as a multi-minute stall the first time the operator speaks. That
fallback still works; this directory is what makes it unnecessary.

Adding a checkpoint: add a key to that entry's `downloads:` map with a
`base_url` pinned to an upstream revision and a `files:` map of
filename → sha256. No code change — the fetch script reads the catalog.
