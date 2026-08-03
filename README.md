# TESSERACT

TESSERACT is a personal assistant that runs on your own Windows machine. It remembers what you tell it, keeps a research library it can search, and you can talk to it — all without sending anything to the cloud unless you choose to turn a cloud feature on.

## What it is

Most AI assistants forget you between sessions and can't safely do anything. TESSERACT is the layer that fixes both.

It's a local-first AI runtime. It gives an assistant:

- **Long-term memory that consolidates rather than hoards** — it decays and merges what it knows instead of accumulating everything forever.
- **An append-only research vault** it can search and synthesize from, kept separate from memory so research never contaminates recall.
- **A tool registry with a real permission model** — every file write, shell command, and outbound call is gated by policy, and the default posture asks you first.
- **An orchestration kernel** that runs background and scheduled work with durable state and crash recovery, so a job survives a restart instead of vanishing with the process.

It's **model-agnostic by design**. API providers, subscription CLIs, and local models all plug into the same role wiring, so swapping the brain is a config change, not a rewrite — see [You aren't tied to Anthropic](#you-arent-tied-to-anthropic) below.

The interface is a desktop app: voice-first, with a visual HUD.

Everything it remembers is plain files on your disk — Markdown you can open, read, and edit. Nothing is locked in a database.

## Install

1. Go to the [Releases](../../releases) page and download the latest installer (`TESSERACT_x.y.z_x64-setup.exe`).
2. Run it. Windows will show a blue screen titled **"Windows protected your PC"**, and the only button on it says **"Don't run"**.

   This is expected. The app isn't code-signed yet, so Windows has no history with it and treats it as unknown by default — the same thing happens to any new small app until enough people have run it. To get past it: click **"More info"**, then click **"Run anyway"**.

   You only have to do this once per machine, not every time you open TESSERACT.

3. Follow the installer. It installs just for your Windows user account, so it won't ask for admin permission.

### Opening it

TESSERACT appears in your Start Menu — just search for it. Opening it a second time while it's already running brings the existing window back rather than starting a second copy.

### A note while the project is private

Right now the TESSERACT source repository is private, and the app downloads its own source code from that repository the first time it runs. That means, until it's made public, you also need a GitHub account with access to the repository and a personal access token, which the person who gave you the installer will need to give you. If the first-run setup screen can't download the source because the repository is private, it will ask you to paste that token right there — no need to create any folders or files yourself. Paste it, and setup continues on its own. If you don't have a token, ask whoever gave you the installer; this section will stop applying once the repository goes public.

## First run

The first time you open TESSERACT, it sets itself up: it downloads a Python runtime, installs its dependencies, downloads a browser engine it uses for web tasks, and downloads the local voice it speaks with. You'll see a progress screen with messages like "Downloading Python…" and "Downloading dependencies…".

This takes several minutes — roughly five to ten, depending on your internet connection. That's normal. Don't force-quit it; just let it finish. After this first-run setup, every later launch is fast.

## One thing to add before you can talk to it

Nothing is _required_ to install and start TESSERACT — it will open, set itself up, and run. But to actually hold a conversation it needs a language model, and out of the box it's set up to use OpenAI. So add one key:

1. Get an API key from [platform.openai.com](https://platform.openai.com/signup).
2. Open the `.env` file in your data folder (see below) — it's already there, with every key listed and commented.
3. Paste your key after `OPENAI_API_KEY=`, save, and **restart TESSERACT**. It only reads that file at startup.

Until you do, TESSERACT still opens and works — but when you send a message it will tell you it has no chat provider yet, rather than sitting silent.

### One more key, free, worth adding

TESSERACT does a lot of work you never see: an observer that watches the conversation, sub-agents, background reasoning, image generation. Out of the box all of that is pointed at **NVIDIA's build tier, which is free and needs no payment method** — so it doesn't run up a bill on your OpenAI key.

Get a key at [build.nvidia.com](https://build.nvidia.com/) and paste it after `BUILD_NVIDIA_KEY=`. If you skip it, everything still works; that background work just falls back to OpenAI and you pay for it.

### You aren't tied to OpenAI

OpenAI is only the shipped default. TESSERACT can use any of these, and can mix them — one model for conversation, a different one for background work, a local one for anything you'd rather keep on your machine:

|                            |                                                                                          |
| -------------------------- | ---------------------------------------------------------------------------------------- |
| **Paid APIs**              | OpenAI (GPT) · Anthropic (Claude) · Google (Gemini) · xAI (Grok) · NVIDIA NIM (free tier) |
| **Subscription CLIs**      | Claude Code · Codex — use a subscription you already pay for, no separate API bill       |
| **Local, on your machine** | Ollama for chat and embeddings · Whisper for hearing you · Piper and Kokoro for speaking |

Each needs its own key in `.env` (except the local ones, which need nothing). Which model handles which job is set in `config/roles.yaml`, or under **Settings → Model roles**, and you can change it while TESSERACT is running — it picks up the change without a restart. [SETUP.md](SETUP.md) walks through swapping a provider and adding one that isn't listed.

Beyond a chat model, everything else is genuinely optional: web search, image generation, Telegram. Each is unlocked by adding its own key, and if you ask for something that needs a key you haven't added, TESSERACT tells you at that moment instead of refusing to start. You can see what's on and what's off under **Settings → Capabilities**.

## What works without any key

- **Talking to it** — speech recognition runs locally on your machine.
- **Spoken replies** — the voice is downloaded during first-run setup. If that download can't complete, TESSERACT replies in text and quietly retries on every later launch, so a spotty connection delays spoken replies rather than losing them.
- **Memory** — it remembers what you tell it, in plain files you can read.
- **Its research library** — documents you add are indexed and searchable.

These all need a language model to be _useful_ in conversation, but they run on your machine and cost nothing.

## What happens when a key is missing

Nothing fails at startup. TESSERACT starts with whatever you've given it and tells you at the moment you ask for something it can't do. The full table is in [SETUP.md](SETUP.md); the short version:

| Missing               | What you lose                                     | What you still have                                    |
| --------------------- | ------------------------------------------------- | ------------------------------------------------------ |
| Chat key              | Conversation                                       | Everything else — it just can't reply                   |
| `BUILD_NVIDIA_KEY`    | Free background work                               | The same work, billed to your chat key instead          |
| `BRAVE_SEARCH_API_KEY`| The `web_search` tool                              | Memory, vault, and the model's own knowledge            |
| `TAVILY_API_KEY`      | `tavily_search` / pulling a web page into the vault| The same — these two are separate from Brave, not a fallback for it |
| Ollama                | Meaning-based search of memory and the vault       | Keyword search over both, automatically                 |
| Telegram token        | The Telegram bridge                                | Everything else; the bridge just stays off              |

Web search is the one people trip over: **Brave and Tavily are two different tools, not alternatives.** Brave backs `web_search`; Tavily backs `tavily_search` and `tavily_extract`. Neither key covers the other's tools, so if you want the web fully available, add both. Both have free tiers.

## Where your data lives

Everything TESSERACT remembers lives in one folder:

```
%LOCALAPPDATA%\com.tesseract.mirror
```

(Paste that path into File Explorer's address bar to open it.) Nothing else on your machine is touched.

Inside it, the parts worth knowing about:

| Folder           | What's in it                                                                      |
| ---------------- | --------------------------------------------------------------------------------- |
| `.env`           | Your API keys. Commented, explaining what each one unlocks.                       |
| `memory-store/`  | What TESSERACT remembers — plain Markdown you can read and edit.                  |
| `vault/`         | Your research library. Drop documents in and they become searchable.              |
| `workspace/`     | How it understands itself and you — including notes it keeps on your preferences. |
| `config/`        | Settings. Editable, but the defaults are sensible.                                |
| `tars-workshop/` | Scratch space for longer pieces of work.                                          |

Each of these arrives with a short explainer file inside it. They're ordinary files — nothing is hidden in a database.

### Changing settings while it's running

Most of `config/` reloads the moment you save it, with no restart — including `providers.yaml` and `roles.yaml`, so you can swap which model does what mid-conversation. If a change is malformed, TESSERACT logs the problem and keeps running on the previous settings rather than falling over.

The exception is `.env`: keys are read once at startup, so adding one always needs a restart.

## Updates

TESSERACT checks for updates on its own and shows a small notification when one is available. Nothing installs automatically — it only updates when you click to apply it. TESSERACT restarts itself as part of applying the update.

## Uninstall

Uninstall TESSERACT the normal way: Windows Settings → Apps, or Control Panel → Programs and Features.

The uninstaller will ask whether to also delete your saved data (memory, research library, settings, `.env`). The default answer is **no** — your data is kept in case you reinstall later. If you want a completely clean removal, either answer yes when asked, or delete the folder yourself afterward:

```
%LOCALAPPDATA%\com.tesseract.mirror
```
