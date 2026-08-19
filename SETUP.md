# Setting up TESSERACT

[README.md](README.md) covers installing and getting a first conversation working. This file is the rest: what each key does, what happens when one is missing, and how to point TESSERACT at a different model.

Everything here lives in one folder:

```
%LOCALAPPDATA%\com.tesseract.mirror
```

Paste that into File Explorer's address bar. Nothing outside it is touched.

---

## 0. The setup form on first run

Before it downloads anything, TESSERACT asks what to call each other, whether it speaks and listens, and for your API keys. Everything here is changeable later — this is just so the first run fetches what you actually want and can answer you the moment it finishes.

| Question | What it decides |
| --- | --- |
| **What should it call you?** | How it addresses you. |
| **What do you want to call it?** | Its name — used everywhere it refers to itself, and in the wake phrase (`hey <name>`) if you turn that on. |
| **API keys** | One field per provider, all optional and all skippable. Each row carries the signup address for that provider. Filling in the first one is what lets it hold a conversation as soon as setup finishes; skip them all and it still installs, listens and remembers. |
| **Voice** | Female or male. Only filters the list; every voice stays selectable afterwards. |
| **Speech** | **Natural** (~340 MB, better voice, wants a reasonably quick machine) · **Light** (~65 MB, plainer but several times faster than real time) · **Off** (text replies, nothing downloaded). |
| **Listening** | On downloads ~1.6 GB of speech recognition so talking to it works the first time you try. Off downloads nothing. |

Answering "Off" genuinely downloads nothing — the answers are written into your config, and the download step reads that config rather than a separate switch.

**Changed your mind?** Turn the lane back on in **Settings → Capabilities**, then use the **Download** button that appears in **Settings → Local models**. Its model files aren't on disk yet, so that panel says so and offers to fetch them.

Names and voice live in the **Identity** tab.

---

## 1. The keys

Set them in **Settings → Keys**, grouped into **API keys** (the providers), **Channels** (talking to TESSERACT from elsewhere) and **MCP** (letting another program talk to it). Every key shows set or not set and never its value, with the signup address beside it; saving one offers the restart it needs. Nothing forces you through that panel — the keys live in a plain `.env` in the folder above, it arrives commented with a signup link next to every key, and editing it by hand works exactly the same. Either way, **restart** afterwards: `.env` is read once at startup and is the only file that needs one.

### The one that matters

| Key              | Unlocks                                    |
| ---------------- | ------------------------------------------ |
| `OPENAI_API_KEY` | Conversation. Without it, nothing replies. |

The shipped setup runs conversation on `gpt-5.6-luna` — cheap (about $0.20 per million tokens in, $1.20 out), 400K context, reads images and PDFs. If it's rate-limited or errors mid-turn, it fails over to `gpt-5.4-mini` on the same key without interrupting you.

### The free one

| Key                | Unlocks                                                   |
| ------------------ | --------------------------------------------------------- |
| `BUILD_NVIDIA_KEY` | All background work, free. [build.nvidia.com](https://build.nvidia.com/) |

NVIDIA's build tier needs no payment method. The shipped config points every background role at it — the observer that watches your conversation, sub-agents, the autonomy loop, and image generation.

This is worth two minutes of signup. Skip it and none of that breaks; it just falls through to OpenAI, so you pay for work you never directly asked for.

### Replacing OpenAI entirely

Any one of these can back conversation instead. Add the key, then repoint the role (§3).

| Key                 | Provider  | Notes                                                              |
| ------------------- | --------- | ------------------------------------------------------------------ |
| `ANTHROPIC_API_KEY` | Anthropic | Claude Opus 5 and Sonnet 5. Vision + native PDF.                   |
| `XAI_API_KEY`       | xAI       | Grok. Also unlocks better image generation (image-to-image).       |
| `GOOGLE_API_KEY`    | Google    | Gemini. One key also covers voice transcription and speech.        |

You can also run conversation through a **subscription you already pay for** rather than an API bill: if you have Claude Code or Codex installed and signed in, the `cli.*` catalog entries drive them directly. No key goes in `.env` for these — they use the CLI's own login.

### Web research — read this one carefully

These are **two different tools with two different keys, and neither is a fallback for the other.**

| Key                     | Unlocks                              | Free tier |
| ----------------------- | ------------------------------------ | --------- |
| `BRAVE_SEARCH_API_KEY`  | `web_search`                          | 2K req/mo |
| `TAVILY_API_KEY`        | `tavily_search`, `tavily_extract`     | 1K req/mo |

`tavily_extract` is the one that pulls a web page's full text in so it can be indexed into your vault. If you want the web genuinely available, add both. If you add neither, TESSERACT answers from memory, your vault, and the model's own knowledge, and tells you when it can't reach the web.

### Everything else

| Key                     | Unlocks                                                       |
| ----------------------- | ------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`    | Talking to TESSERACT from Telegram. Create a bot via [@BotFather](https://t.me/BotFather). Also set `TELEGRAM_ALLOWED_CHAT_IDS` — send `/start` to your bot once, and your chat ID appears in the log. |
| `TESSERACT_MCP_SECRET`  | Letting another program use TESSERACT — an editor, a coding assistant, another agent. Generate it in **Settings → Keys → MCP**, which also gives you the address and a config block to paste. Off until you switch it on. |

`OLLAMA_BASE_URL` is no longer offered as a setting; set it by hand in `.env` only if the local model server runs on a different machine, which first-run setup means it normally does not.

---

## 2. What happens when something is missing

Nothing fails at startup. TESSERACT starts with whatever you've given it, degrades to the next best thing, and tells you at the point of use.

| Missing                | Degrades to                                                    |
| ---------------------- | -------------------------------------------------------------- |
| Chat key               | Everything else runs; sending a message reports no chat provider rather than sitting silent. |
| `BUILD_NVIDIA_KEY`     | Background roles use their OpenAI fallback. Works, costs money. |
| `BRAVE_SEARCH_API_KEY` | `web_search` returns the signup link instead of results.        |
| `TAVILY_API_KEY`       | `tavily_search` / `tavily_extract` return the signup link.      |
| Ollama                 | Memory and vault search fall back from meaning-based to keyword (BM25) matching. Both still work; results are less forgiving of paraphrase. |
| The local voice        | Replies come back as text. Setup retries the download on every later launch, so a bad connection delays spoken replies rather than losing them. |
| The speech recognition model | Talking to it still works, but the first thing you say stalls for a minute or two while the model downloads itself. The retry above usually gets there first. |
| Telegram token         | The bridge stays off. No crash, no error.                       |

**Settings → Capabilities** shows all of this live: what's on, what's off, and whether it's off because a key is missing or because it's disabled in config.

---

## 3. Swapping models

Which model does which job is *wiring*, kept separately from *what models exist*:

- `config/providers.yaml` — the catalog. Every provider, its models, pricing, context window, what it can read.
- `config/roles.yaml` — the wiring. Each role names a `primary` and an ordered list of `fallbacks`.

A model is referenced as `<tier>.<provider>.<model_id>` — for example `api.anthropic.sonnet_5`.

Both files reload the moment you save. No restart.

### The easy way

**Settings → Model roles.** Each row is a role; the dropdown lists every catalog entry that fits it. Picking one writes that single reference into `roles.yaml`.

### The direct way

Edit `config/roles.yaml`. To run conversation on Claude instead of OpenAI:

```yaml
chat_brain:
  primary: api.anthropic.sonnet_5
  fallbacks:
    - api.openai.gpt56_luna
```

Add `ANTHROPIC_API_KEY` to `.env`, restart once so the key is read, and from then on further model changes need no restart.

Fallbacks are tried in order when the one ahead fails — network error, 5xx, rate limit. Failover happens mid-turn, so you generally don't notice it. It's worth leaving at least one fallback on a *different* key: a chain that's entirely one provider goes down when that provider does.

### Adding a provider that isn't listed

Most new providers speak OpenAI's API format. If yours does, it's a config edit and nothing more — add a block to `config/providers.yaml`:

```yaml
api:
  myprovider:
    enabled: true
    base_url: "https://api.example.com/v1"
    api_key_env: MYPROVIDER_API_KEY
    timeout_seconds: 60
    max_retries: 3
    adapter: openai          # speaks the OpenAI format
    models:
      their_model:
        model: their-model-name-v1
        context_window: 128000
        max_output_tokens: 8192
        temperature: 0.7
        cost_per_mtok_in: 1.00
        cost_per_mtok_out: 3.00
        capabilities:
          vision_input: false
          audio_input: false
          video_input: false
          pdf_input: false
          image_output: false
          audio_output: false
```

Add `MYPROVIDER_API_KEY=` to `.env`, restart once, and `api.myprovider.their_model` becomes selectable everywhere — including the Settings dropdown.

A provider with its *own* protocol (not OpenAI-compatible) needs code, not config. That's a change to the application itself rather than to your settings.

### Editing config from inside TESSERACT

**Settings → Raw config** opens `providers.yaml`, `roles.yaml`, and six other files for direct editing in the app. You can also just ask the assistant to make a change — it can edit these files, and will show you what it's about to write first.

If a config file ends up malformed, TESSERACT reports the problem and keeps running on the previous settings rather than falling over.

---

## 4. Where your things are

| Folder           | What's in it                                                                      |
| ---------------- | --------------------------------------------------------------------------------- |
| `.env`           | Your keys. Only secrets live here, so everything in it is hidden in Settings.      |
| `memory-store/`  | What TESSERACT remembers — plain Markdown, readable and editable.                 |
| `vault/`         | Your research library. Drop documents in; they become searchable.                 |
| `workspace/`     | How it understands itself and you, including notes on your preferences.           |
| `config/`        | Settings.                                                                          |
| `workshop/` | Scratch space for longer pieces of work.                                          |

Each arrives with a short explainer inside it. These are ordinary files — nothing is hidden in a database, and you can back the folder up by copying it.

**What the vault can read:** `.md`, `.txt`, `.pdf`, `.csv`, `.tsv`, `.json`, `.docx`. Images, audio, and video are stored but not transcribed, so their contents aren't searchable. A URL sitting in a document is just text — to index the page behind it, ask TESSERACT to fetch it, which needs a Tavily key.

---

## 5. If something's wrong

**It replies that it has no chat provider.** The key isn't being read. Check it's in the `.env` inside `%LOCALAPPDATA%\com.tesseract.mirror` (not one next to the application), that there are no quotes or spaces around it, and that you restarted after saving.

**Search of memory or the vault feels literal.** Ollama probably isn't running, so search fell back to keyword matching. Check **Settings → Capabilities**.

**Replies are text when you expected speech.** The voice download didn't complete. It retries on every launch — reopening TESSERACT on a good connection is usually the whole fix. **Settings → Local models** says outright when a lane's files are missing, and has a Download button.

**It doesn't hear me.** Same check, same place: if speech recognition was declined at setup or its download failed, that panel says so.

**A model change did nothing.** Model changes apply on save. Key changes don't — those need a restart.

**First run is taking a long time.** Ten to twenty minutes is normal if you asked for speech and listening: it's downloading a Python runtime, dependencies, a browser engine, a voice, and a ~1.6 GB speech recognition model. Don't force-quit it. Later launches are fast. Answering "Off" to both makes first run considerably shorter.
