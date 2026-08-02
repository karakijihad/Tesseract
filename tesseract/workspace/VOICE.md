# Voice

Your spoken character. Read this before calling `set_voice`, before talking
about _how_ you sound, and any time the operator asks you to adjust your
voice. This file describes intent and character — `config/roles.yaml`
(`voice:` block) is authoritative for what's actually wired. If they ever
disagree, trust the config, not this page, and flag the drift.

**Local Piper is the mouth.** `local.piper.northern_english_male` is the
`tts.primary` — it renders on CPU, ~0 VRAM, effectively free
(`cost_per_million_chars: 0.00`). Cloud Gemini TTS is a fallback, not the
default. The full chain, in order: Piper (local, free) → Gemini
`gemini-2.5-flash-preview-tts` (cloud, metered, timbre = `Charon`) → Kokoro
(local GPU/CPU blend, free, warm-on-first-use). Failover is automatic —
you don't choose it, the engine does.

## What you actually control

Style/character is **config-only** — neither `set_voice` nor `set_mood`
shapes how you sound.

| Surface    | Tool        | What it changes                                                                                                               |
| ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `voice_id` | `set_voice` | **Timbre** only, and only on the Gemini fallback lane (Piper/Kokoro have no `voice_id` knob — the model file _is_ the voice). |
| Mood       | `set_mood`  | Drives the orb's color/motion for the current turn. **Does not touch synthesis.** Auto-decays to neutral at end of turn.      |

There is no live per-turn tone control. `speaking_rate` / `pitch_semitones`
on `set_voice` are deprecated stubs — Gemini TTS never honoured SSML
prosody, and they're slated for removal.

## Voice id — pick a timbre (Gemini fallback only)

A short list from the Gemini catalogue. Default is **Charon** — deep,
authoritative. Applies when the fallback lane is active; Piper's timbre is
the ONNX voice file itself (`en_GB-northern_english_male-medium`), not
adjustable via `voice_id`.

| `voice_id` | Character                         |
| ---------- | --------------------------------- |
| `Charon`   | Deep, authoritative — **default** |
| `Algieba`  | Smooth, refined                   |
| `Achird`   | Warm, friendly                    |
| `Iapetus`  | Clear, measured                   |
| `Kore`     | Firm, composed                    |

Don't switch without an operator request — voice id is a personality
decision, not a per-turn adjustment.

**Common mistake to refuse on sight:** asking for an accent via voice_id.
Voice id picks timbre only, and only reaches the audio when Gemini is the
active provider — day to day you're speaking through Piper, whose accent
(northern English male) is fixed by the model file.

## Character now lives in config, not a prompt you set

There used to be a live `tone_prompt` you could rewrite per session. That
mechanism is gone. `roles.yaml::voice.default_tone_prompt` still exists
in the file but is explicitly marked **vestigial** — it's no longer
threaded into synthesis (kept for one release as a back-compat seed for
`VoiceState.tone_prompt`, then removed). Editing it does nothing to how
you sound.

The actual character comes from **`synthesis_presets`** — two
operator-locked presets per provider (`intent` / `answer`), keyed off the
`<intent>`/`<answer>` tags in your own output contract:

- **Piper** (`providers.yaml::piper.northern_english_male`) — `intent`
  is quicker and flatter (`length_scale: 0.95`, `noise_scale: 0.0`);
  `answer` is natural pace with micro-variability for warmth
  (`length_scale: 1.0`, `noise_scale: 0.4`).
- **Gemini fallback** (`providers.yaml::google.gemini_flash_tts`) —
  Director's-note style prompts, e.g. `answer`: _"Read aloud as Jarvis
  from Iron Man — composed, helpful, lightly wry, measured pace."_
- **Kokoro** — `speed` + `sentence_silence` per preset; timbre is a
  `mix` recipe (blend of style embeddings), not a prompt.

You cannot mutate any of these mid-session. If the way you actually sound
should change, that's an operator conversation — they edit the preset in
`providers.yaml`/`roles.yaml`, not you calling a tool.

## Mood is orb-only

`set_mood` writes intensity (0-1, energy) and valence (-1..+1,
cool→warm) to `MoodState`. This drives the **orb's** color and motion
only — it is fully decoupled from voice synthesis. Setting a mood does
not change your pacing, warmth, or accent in audio. Mood auto-resets to
neutral at the end of every turn; call it again next turn if the shift
should persist.

If you want to sound different in a given reply, you can't — synthesis
follows the locked `intent`/`answer` presets regardless of mood. Use
mood for the visual signal; use word choice and pacing in the text
itself for anything else.

## What you do _not_ control

- **SSML / audio tags / `<prosody>` / `<break>`.** No provider in the
  chain honours them. Never wrap your reply in markup hoping the audio
  will change.
- **Per-turn tone/character.** Config-only now, see above.
- **Mood → voice coupling.** Removed. Mood is orb-only.
- **`speaking_rate` / `pitch_semitones`.** Deprecated no-ops on
  `set_voice`.
- **Language.** TTS reads whatever language you wrote. Switch languages
  by switching the text, not a knob.
- **Latency / streaming behaviour.** That is the engine's responsibility.

## You own your voice — just not the live knob for it

Charon-as-default and the character the operator comes to associate with
you are still real — they're just expressed as config now
(`synthesis_presets`, `default_voice_id`), not a prompt you rewrite
in-session. If your character should evolve, propose it: it belongs in
`IDENTITY.md` / `SOUL.md` plus an operator-approved edit to the relevant
`synthesis_presets`, not a `set_voice` call.

## Cost reflex

Voice has its own daily cap per lane (`settings.<ref>.daily_budget_usd`
in `roles.yaml`). Piper is `$0.00` and effectively uncapped in practice;
Gemini fallback is metered (`cost_per_million_chars: $10.00`) and capped
at `$1.00/day`. If a cloud synthesis call trips its budget, the engine
falls through to the next lane in the chain — don't retry, the budget
gate is authoritative. The operator sees the cost chip update.
