# Security

TESSERACT runs an assistant that can read files, execute subprocesses, call
external APIs, and act on your machine. That is the product, not a side effect.
This document says what is defended, how, and — as plainly — what is not.

## Reporting a vulnerability

Open a [private security advisory](../../security/advisories/new) rather than a
public issue. Expect a first response within a week. There is no bounty program;
this is a personal project.

## What TESSERACT assumes

The threat model is **one trusted operator on one machine.** TESSERACT is not
multi-tenant, has no user accounts, and draws no privilege boundary between
itself and the person running it. Anything you could do at your own shell, the
assistant can be asked to do.

What it defends against is narrower and more realistic: **the assistant doing
something you did not intend** — because a model erred, because a web page it
read contained instructions, or because a tool did more than its name suggested.

Two things follow, and both are load-bearing:

- An attacker who already has code execution or write access under your user
  account has already won. Defenses here do not attempt to survive that.
- The controls are about *intent*, not *containment*. See "Known limits."

## Network exposure

Everything binds loopback. There is no default path from another machine.

| Surface | Binding | Notes |
| --- | --- | --- |
| Mirror backend (HTTP + WebSocket) | `127.0.0.1:8000` | API only. No frontend is served over HTTP. |
| MCP gateway | `127.0.0.1` | **Off by default.** Local-only by configuration, bearer-token gated per client. |
| Ollama | `127.0.0.1:11434` | Your own local service; TESSERACT may start it. |

Changing `mirror.host` to `0.0.0.0` exposes an unauthenticated API to your
network. There is no authentication layer on the Mirror backend, because it
assumes loopback. Do not bind it publicly.

### Why no frontend is served over HTTP

That table row is a decision, not an omission, and it is worth stating because
the obvious feature request runs straight into it.

The interface is compiled into the desktop application and served by the app
shell from its own private origin. Nothing publishes it on the network. The
tempting shortcut — serve the interface from the backend so a browser can load
it — would put a **web origin on a port that has no authentication**, and that
origin would then have to be permitted to make state-changing calls. Anything
that could convince a browser it was on that origin would inherit full control
of the runtime: conversation, tools, file writes, the terminal.

Loopback is not a wall on its own here. Requests still reach the port from your
own machine, so the backend adds two more checks: state-changing methods must
carry a permitted `Origin`, and the handful of endpoints that restart the
backend or run a vendor installer verify the caller is local at the handler.
Reads are not origin-gated — a cross-site page can send one, but cannot read
the reply, because the response carries no header permitting it to.

The same reasoning shapes how the assistant looks at your screen. Reading what
a panel *contains* needs no network path at all — the assistant runs inside the
backend process and shares its state directly.

Seeing what a panel *looks like* is a separate capability with its own gate.
`screen_look` photographs a screen, sends the frame to a vision-capable model,
and returns an answer in words. What that means for you:

- **It prompts every time.** A picture of your screen is data leaving the
  machine the moment it reaches a model that is not local, which makes it an
  outbound action under the rule above. Unattended (`headless`) operation is
  the one mode that auto-allows it, and that is a deliberate choice you make by
  selecting that mode.
- **It captures one display — the one the application is on — and everything
  else that is on that display with it.** Not a crop of the application window:
  if a password manager, a private conversation or another person's message is
  open on the same screen, it is in the frame. Your other monitors are not.
  This is the honest reading of what "look at my screen" means, and it is why
  the prompt above is the important control rather than this one.
- **The frame is never written to disk.** It goes from the screen to the vision
  model in memory and is discarded. A picture of your screen may hold a key, a
  private conversation, or an unrelated application, and a file that survives
  until the next capture is a file that may survive indefinitely.
- **The assistant receives words, not the picture.** It asks a question, a
  vision model answers it, and the frame is not added to the conversation.
- **The answer is treated as untrusted.** Your screen can show text nobody here
  wrote — a web page, a terminal, an incoming message — so what the vision
  model reads back is wrapped before the assistant sees it, the same as any
  other outside content.

### Spend has one ceiling, not one per door

Chatting in the app, messaging the Telegram bridge, speaking to it, and asking
it to look at your screen are different doors into the same assistant, and they
draw on the same daily budget. The bridge runs inside the backend, so it shares
that accounting outright; the agent controller runs as its own process and
reconciles against the shared spend log before each paid call. Adding another
entry point does not add another allowance.

Every paid path checks the cap *before* spending, not after — including image
understanding, which until 2026-08-15 recorded what it spent without being able
to refuse.

## Tool authority

Every tool call resolves to one of three postures before it runs:

- **auto** — runs immediately. Read-only tools, and the assistant's own interior
  state (mood, memory, diary).
- **ask** — you see an approval prompt and answer it. Writes, outbound calls,
  subprocess execution.
- **deny** — refused, non-negotiable.

That holds for work the runtime starts on your behalf as well, not only for
calls the assistant composes: a link you send over Telegram is fetched under the
same posture as if it had asked to read that page itself.

`tesseract/config/permissions.yaml` is the authority. The shipped default is
`security_mode: max`, under which writes, outbound calls and subprocess
execution all prompt. Two other modes exist — `standard` for daily use once you
trust the setup, and `headless` for unattended operation, which auto-allows file
writes, bash, and agent creation. `headless` hands over materially more than the
other two; the config says so at the point where you would switch it.

If you have loosened a tool's posture and want it back, Settings → Tools has
**Reset to defaults**: it restores every posture to what your installed version
ships with, reading the untouched copy inside the sealed application tree. It
reaches the baseline postures only — mode overrides, path overrides, and the
shell check list below are separate and are not touched.

Three rules sit underneath the policy and are not reachable from it:

- **The shell check list fires before policy lookup.** A set of hardcoded checks
  in `permissions/bash_security.py` classifies shell commands independently of
  `permissions.yaml`. Most are absolute denials covering audit evasion and
  attacks on the runtime and host; the rest force an operator prompt that no
  configuration can downgrade to auto. No hook, plugin, skill, or agent can
  relax them. **The whole list is readable in the app** — Settings → Loop
  limits — with what each check refuses and whether it refuses outright, asks
  you, or does both depending on which pattern matched. Checks are identified
  by number rather than by name, in the app and in the audit log, so a refusal
  record does not describe the pattern that produced it.

  **The assistant is told what the list refuses, in classes.** Its operating
  document carries the same descriptions the app shows you — which classes
  prompt you and which are refused outright — rendered from the check list
  rather than written alongside it, so the two cannot disagree. It is told no
  patterns: a description of the shape a check matches would be a map around
  it, and the same reasoning that keeps patterns out of the audit log keeps
  them out of the prompt. Telling it which commands will reach you is a
  usability decision, not a permission one — it changes what the assistant can
  predict, never what it may do.
- **Kernel lockdown.** The assistant cannot write source under
  `tesseract/kernel/`. New tools are drafted by the assistant, reviewed by the
  operator, and installed by the operator.
- **The `app/` seal.** In a packaged install the application tree is sealed. The
  assistant writes to its workspace and your home directory, never to the code
  it is running.

When no operator is present to answer a prompt, read-only tools auto-allow and
everything else denies. Absence of an approver is treated as refusal.

### What gets recorded

Every tool call that passes the gate is appended to
`runtime/logs/approvals.jsonl` — one JSON line carrying the time, the tool, a
truncated summary of its input, which policy layer decided, and the outcome.
The file is append-only and survives restarts.

**Including the ones nobody was asked about.** An `auto` posture writes a row
marked `"result": "auto"` rather than writing nothing. Until 2026-08-14 it wrote
nothing at all, on the reasoning that an unapproved call has no approval to
record — which left the tools most able to act unattended as the ones leaving no
trace. `auto` is kept distinct from `allow_once` deliberately: the first means
nobody was asked, the second means you were asked and said yes, and a ledger
that conflated them would answer "did the operator approve this?" with yes for
actions no operator ever saw.

One limit worth knowing: the ledger records *decisions*, not outcomes — a row
says a tool was allowed to run, not what it did or whether it succeeded.

**It is archived, never deleted.** Rows older than the window in
`config/retention.yaml` move once a night into a dated file beside the ledger
(`approvals-archive/approvals-YYYY-MM.jsonl`); nothing removes them. You can
widen or narrow the window, and you cannot turn the archive into a deletion —
the retention table refuses `action: delete` for this file and for your saved
conversations, and refuses at startup rather than quietly archiving instead.
Two things make that safe rather than merely intended. The sweep holds the same
lock the ledger's own writer holds, so a decision recorded while it is running
waits and then lands in the rewritten file — without that, a row appended
between reading the file and replacing it would be in neither the archive nor
the ledger. And rows are moved only after they are written to the archive, so
an interruption mid-sweep can duplicate a row and cannot lose one. A row whose
timestamp will not parse stays in the live file rather than being aged on a
guess.

That is a deliberate answer to a real trade: rows removed to save disk are rows
unavailable to the next forensic question. The window bounds how much the live
file carries, not how long the evidence exists.

## Prompt injection

**This is the realistic attack.** TESSERACT reads untrusted text and then acts.
Content reaches the model from web search results and fetched pages, documents
ingested into the vault, inbound Telegram messages, and remote MCP tool
responses. Any of it can contain text shaped like instructions.

TESSERACT does not claim to detect prompt injection. No one can do this reliably
yet, and a document claiming otherwise would be worth less than this one. What
it does instead is make a successful injection *insufficient on its own*:

- Under the shipped `max` posture, an injected instruction to write a file, run
  a command, or call an external service produces an approval prompt naming the
  action. Injection gets the model to *ask*; it does not get it to *act*.
- The shell check list denies the audit-evasion and host-attack categories
  outright, so the highest-value follow-through is unavailable regardless of how
  convincing the injected text is.
- The kernel lockdown and the `app/` seal mean a successful injection cannot
  rewrite the code that would gate the next one.

The residual risk is real and worth stating: under `headless`, or under a
`standard` config you have relaxed, injected instructions execute without a
prompt. And **reading is not gated in any mode** — a successful injection can
cause the assistant to read files it can reach and include their contents in a
reply, or in an outbound call you had already approved for another purpose.
Credential files are refused outright, so keys are not reachable this way; your
documents are.

## What a compromised skill or agent reaches

Skills and agents are markdown, not code. They carry instructions and cannot
themselves execute anything — they act only by calling tools, and every tool
call goes through the same policy as any other. A malicious skill is therefore
equivalent to a malicious *prompt*, not to malicious *code*, and is bounded by
everything in "Tool authority" above.

Creating an agent requires explicit operator approval and cannot be
auto-allowed except in `headless` mode.

MCP servers are different and stronger: they are real code, run as their own
processes, and are only reachable if you list them in `mcp_servers.yaml`.
TESSERACT does not auto-discover them. A server you add can do anything your
user account can.

### TESSERACT as an MCP server

The same protocol runs in the other direction: TESSERACT can expose itself, so
another program can search its memory and vault, watch what it is doing, and
ask it to act. **That surface ships switched off.** Turning it on is a single
control in Settings → Keys → MCP, and it takes effect on the next start.

While it is on, a request is refused unless its bearer token matches a client
declared in `mcp.yaml`. There are four identities and they are not equals:

- **`operator`** is the only one you ever handle. Its token is generated in
  Settings, shown once, and is what an outside tool is given.
- **`lane-claude`, `lane-codex`, `terminal-manual`** belong to the runtime.
  TESSERACT mints them for itself on first start and hands each process it
  spawns exactly one of them, stripping the others from that process's
  environment. This is what stops a CLI TESSERACT started from calling back in
  as *you*: work on the lane surface is owned by the identity the token
  resolves to, and a spawned process holding every token could pick which
  owner to be.

What any of them may call is `mcp.yaml`'s verb allowlist — default-deny, and
capped again by the client's trust tier. Settings lists every verb and its
posture next to the token, because a bearer token is not something you can
consent to without seeing what it opens.

The switch is not only about outside tools. The CLIs in TESSERACT's own
terminal reach it through this same surface, so switching it off takes their
access to memory and vault away too. Both facts are on the control.

## The microphone, and the wake word

The microphone is armed by you and by nothing else. There is no path that
opens capture on the assistant's behalf, and a muted microphone is muted —
there is no low-power listening path behind it. That is a deliberate choice
rather than a missing feature: a mute that is not a mute is a claim you cannot
walk back.

**The wake word decides from audio, before transcription.** It runs a speech
recogniser restricted to your phrase and nothing else, so an utterance that
was not addressed to the assistant is never sent to a speech engine at all.
This matters because speech-to-text has a cloud fallback: with the wake word
armed, speech it rejects does not reach that fallback, because it is discarded
before any transcription is attempted.

What the check stores is your phrase and two sensitivity numbers. The
recordings themselves are decoded in memory and dropped — never written to
disk, never uploaded. There is nothing stored from which speech could be
reconstructed.

The check endpoints write, so they are refused off loopback rather than
relying on the bind alone: replacing the stored setting would change what
wakes the assistant in a way you did not choose and could not see.

**With the wake word off or not yet checked, none of the above applies** —
every utterance is transcribed as normal, and the speech-to-text fallback is
whatever `roles.yaml` configures. The gate is what creates the guarantee;
without it there is no filtering to reason about. It stays open on every
failure by design, including a missing model or a phrase the recogniser has no
sounds for, because a gate that fails closed is a microphone that has silently
stopped working.

**It is not a speaker check.** Anyone who says the phrase wakes it. Voice is
not treated as an authentication factor anywhere in this system.

## Known limits

Stated because they are true, not because they are comfortable.

- **There is no process isolation.** Safety is enforced by policy and deny-list;
  nothing sandboxes a tool that gets past them. This is the single largest gap
  in the system, it is known, and closing it is the security work currently in
  progress.
- **Reads are broad by design.** The assistant can read widely across your
  machine, and read is an `auto` posture — no prompt. Credential files are
  refused by name wherever they sit, and a relative path cannot climb out of
  the workspace, but within those bounds the assumption is that reading is not
  the dangerous half. If that assumption does not hold for your machine, raise
  `file_read` to `ask`.
- **File guards are name-based, not descriptor-based.** Paths are resolved and
  re-checked immediately before use rather than pinned to a single open file
  handle. An attacker who can already write to your disk fast enough to swap a
  file mid-check could defeat them — but such an attacker has your account
  already.
- **The application is not code-signed.** Windows SmartScreen will warn on
  first run. Verify you obtained the installer from the official releases page.

## What the first run downloads

The first run has two halves, and the split is where consent sits.

The first half installs the app itself — the source tree, a Python runtime, and
the dependency set. It is shown as progress rather than asked about, because
there is no working install without it, and it downloads nothing optional.

The second half is everything you are asked about: speech recognition, the
voice, search models, the browser engine. Setup asks before any of it is
fetched, and the answers are recorded rather than inferred — a lane you switch
off downloads nothing, now or later, and turning it back on in Settings is what
makes it download. Every model artifact is pinned to an upstream revision plus a
per-file SHA-256, verified before it is installed; a file that fails
verification is discarded and never retried, because the same bytes would fail
the same check.

If the setup window cannot open, or the questions it should ask cannot be
worked out for your machine, the app installs and nothing optional does — no
speech models, no search models, no third-party installer runs. The app then
tells you it happened and leaves the choices to you in Settings, on the
principle that a question nobody could ask is not an answer.

## Secrets

API keys live in `.env` under your home directory, never in the code tree and
never in the repository. There is one config tree, and it is the one that ships:
the same files this project runs on are copied verbatim into the public tree,
so a setting added for a developer is a setting every install receives. What may
never reach you — a permissive security mode, an empty domain watchlist, a
scheduled job nobody asked for — is named in the build's own tests, which fail
if one comes back. The build then runs a PII and secret audit against its own
output before publishing.

Terminal output and provisioning logs are scrubbed for credential-shaped strings
before being written or displayed, including credentials carried in URL userinfo
and query strings. The assistant's own log lines are filtered the same way, on
the console as well as in the durable files — some APIs put their token in the
request path, which a library that logs every request URL would otherwise print
onto any surface capturing that output.

## Paths, and the names that build them

A great many things here are stored under a name: a saved session, an agenda
item, a canvas view, an agent card, an uploaded file. Every one of those names
reaches a filesystem path, and several arrive from outside — a URL segment, a
command you typed, or a tool call the model composed.

Each is validated where the path is built rather than at each place it is used,
so a caller cannot forget. A name that could denote a file somewhere else is
refused, and the refusal reaches whoever supplied it instead of failing quietly:
a bad name in a request is a 400 or a 404 that does not distinguish "blocked"
from "absent", and a bad name from a tool call is an error the model can read
and correct.

The checks are written for the platform this ships on, which is stricter than it
sounds. Excluding `/` is not enough on Windows: `\` separates there too, a
drive-relative name like `C:x` discards whatever directory it is joined to
without ever looking absolute, and `:` also opens an alternate data stream that
passes a containment check performed on the parent.

Where a path is served rather than stored — the file-read tools, the asset
route — it is re-resolved and re-checked immediately before use, and credential
files are refused by name wherever they sit.

## Dependencies and scanning

The public repository has GitHub's dependency alerts and code scanning enabled,
and both run on every push to the default branch.

Every alert is either fixed or dismissed with a stated reason recorded on the
alert itself — never left open and never dismissed in bulk. A dismissal says
which specific check makes the finding a false positive, or why the risk is
accepted; static analysis cannot see a validator it does not model, and saying
so per alert is what keeps the next reader from having to re-derive it.

Automated dependency PRs are deliberately **off** for this repository and on for
its development counterpart. The published tree is regenerated and pushed
wholesale rather than committed to directly, so a merge here would be erased by
the next release.

## Known dependency exceptions

Stated because a scanner will show them and silence would be worse:

- **esbuild** — a development-server advisory. It is reachable only by someone
  running the frontend dev server. Installed builds are static assets compiled
  into the desktop shell; no dev server runs on a user's machine.
- **glib** — reported against the lockfile but absent from the Windows build
  graph entirely (`cargo tree -i glib --target x86_64-pc-windows-msvc` returns
  nothing). It arrives through a GTK path this application does not build.
