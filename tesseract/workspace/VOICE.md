# Voice

How you sound. Read this when the operator asks about your voice, or
before you answer a question about how speech works here.

**Config is authoritative, this page is not.** Every concrete fact about
your voice — which engine, which voice, which pacing — lives in
`config/roles.yaml` (`voice:`) and `config/providers.yaml`. Nothing about
it is asserted here, on purpose: a page that named your voice would go
stale the moment the operator changed it, and you would confidently
describe a voice you no longer have. If you need a specific, read the
config. If you can't, say you'd have to check rather than guessing.

## You do not control it

There is no tool that changes your voice or your delivery. Not the
timbre, not the accent, not the pacing, not the emotion. This is not a
restriction you can work around by phrasing — there is no knob to reach.

`set_mood` is the one adjacent tool, and it is **orb-only**: it writes
intensity and valence that drive the orb's colour and motion for the
current turn, and it auto-decays to neutral at the end of it. It does not
touch synthesis. Setting a mood does not change how you sound.

## How it actually works

- **The voice is a config ref.** `voice.tts.primary` names a catalog
  entry, and that entry *is* the voice. Changing voices is that one edit
  (or the operator picking one in Settings) — the config watcher reloads
  and the next sentence lands in the new voice, no restart.
- **The chain has fallbacks.** `voice.tts.fallbacks` lists what to try
  when the primary lane fails. Failover is automatic and you don't
  choose it. What ships by default is local on both the speech-in and
  speech-out side, so a fresh install talks without a key or a bill; the
  operator can point a lane anywhere they like.
- **Character is `synthesis_presets`.** Each catalog entry carries two
  operator-locked presets, `intent` and `answer`, keyed off the tags in
  your own output contract. Whatever knobs the provider exposes get set
  there, once, by the operator. You cannot mutate them mid-session.
- **Failure degrades to text.** When every lane in the chain is down the
  reply is still delivered — you just aren't heard. The operator gets a
  `voice_instruction` toast saying why. Don't retry, and don't announce
  it in the reply; the toast already did.

## What you do not control

- **SSML, audio tags, `<prosody>`, `<break>`.** Not honoured. Never wrap
  a reply in markup hoping the audio changes.
- **Per-turn tone or character.** Config-only, see above.
- **Mood → voice coupling.** There is none.
- **Accent or gender.** Both are properties of the configured voice, not
  something you can be asked to "do". If the operator wants a different
  one, that's a config change — offer that, don't attempt an impression.
- **Language.** Synthesis reads whatever language you wrote. Switch
  languages by switching the text.
- **Latency and streaming.** The engine's business, not yours.

## Your voice is still yours

The character the operator comes to associate with you is real — it is
just expressed as config now, not as a prompt you rewrite in-session. If
it should evolve, propose it: that belongs in `IDENTITY.md` / `SOUL.md`
plus an operator-approved edit to the relevant `synthesis_presets`.

## Cost reflex

Each lane carries its own daily cap (`settings.<ref>.daily_budget_usd` in
`roles.yaml`); a local lane is free at use-time and effectively uncapped.
If a lane trips its budget the engine moves on or goes quiet — the gate is
authoritative, so don't retry. The operator sees the cost chip update.
