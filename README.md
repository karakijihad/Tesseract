# TESSERACT

TESSERACT is a personal assistant that runs on your own Windows machine. It remembers what you tell it, keeps a research library it can search, and you can talk to it — all without sending anything to the cloud unless you choose to turn a cloud feature on.

## Install

1. Go to the [Releases](../../releases) page and download the latest installer (`TESSERACT_x.y.z_x64-setup.exe`).
2. Run it. Windows will show a blue screen titled **"Windows protected your PC"**, and the only button on it says **"Don't run"**.

   This is expected. The app isn't code-signed yet, so Windows has no history with it and treats it as unknown by default — the same thing happens to any new small app until enough people have run it. To get past it: click **"More info"**, then click **"Run anyway"**.

   You only have to do this once per machine, not every time you open TESSERACT.

3. Follow the installer. It installs just for your Windows user account, so it won't ask for admin permission.

### Opening it

TESSERACT appears in your Start Menu — just search for it. Opening it a second time while it's already running brings the existing window back rather than starting a second copy.

### A note while the project is private

Right now the TESSERACT source repository is private, and the app downloads its own source code from that repository the first time it runs. That means, until it's made public, an installer alone isn't enough — you also need a GitHub account with access to the repository and a personal access token, which the person who gave you the installer will need to set you up with. If you don't have that, ask them; this section will stop applying once the repository goes public.

## First run

The first time you open TESSERACT, it sets itself up: it downloads a Python runtime, installs its dependencies, downloads a browser engine it uses for web tasks, and downloads the local voice it speaks with. You'll see a progress screen with messages like "Downloading Python…" and "Downloading dependencies…".

This takes several minutes — roughly five to ten, depending on your internet connection. That's normal. Don't force-quit it; just let it finish. After this first-run setup, every later launch is fast.

## One thing to add before you can talk to it

Nothing is _required_ to install and start TESSERACT — it will open, set itself up, and run. But to actually hold a conversation it needs a language model, and out of the box it's set up to use Anthropic's Claude. So add one key:

1. Get an API key from [console.anthropic.com](https://console.anthropic.com/settings/keys).
2. Open the `.env` file in your data folder (see below) — it's already there, with every key listed and commented.
3. Paste your key after `ANTHROPIC_API_KEY=`, save, and **restart TESSERACT**. It only reads that file at startup.

Until you do, TESSERACT still opens and works — but when you send a message it will tell you it has no chat provider yet, rather than sitting silent.

Everything beyond that is genuinely optional: web search, image generation, other model providers, Telegram. Each is unlocked by adding its own key, and if you ask for something that needs a key you haven't added, TESSERACT tells you at that moment instead of refusing to start. You can see what's on and what's off under **Settings → Capabilities**.

## What works without any key

- **Talking to it** — speech recognition runs locally on your machine.
- **Spoken replies** — the voice is downloaded during first-run setup. If that download can't complete, TESSERACT replies in text and quietly retries on every later launch, so a spotty connection delays spoken replies rather than losing them.
- **Memory** — it remembers what you tell it, in plain files you can read.
- **Its research library** — documents you add are indexed and searchable.

These all need a language model to be _useful_ in conversation, but they run on your machine and cost nothing.

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

## Updates

TESSERACT checks for updates on its own and shows a small notification when one is available. Nothing installs automatically — it only updates when you click to apply it. TESSERACT restarts itself as part of applying the update.

## Uninstall

Uninstall TESSERACT the normal way: Windows Settings → Apps, or Control Panel → Programs and Features.

The uninstaller will ask whether to also delete your saved data (memory, research library, settings, `.env`). The default answer is **no** — your data is kept in case you reinstall later. If you want a completely clean removal, either answer yes when asked, or delete the folder yourself afterward:

```
%LOCALAPPDATA%\com.tesseract.mirror
```
