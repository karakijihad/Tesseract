---
title: Security
description: "TESSERACT runs an assistant that can read files, execute subprocesses, call external APIs, and act on your machine. That is the product, not a side…"
---

<!-- Rendered from SECURITY.md at the repository root by
     tesseract/scripts/generate_guide.py. Edit that file, not this
     one; CI regenerates this page and fails if it differs. -->

TESSERACT runs an assistant that can read files, execute subprocesses, call
external APIs, and act on your machine. That is the product, not a side effect.
This document says what is defended, how, and — as plainly — what is not.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/karakijihad/Tesseract/security/advisories/new) rather than a
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
| MCP gateway | `127.0.0.1` | Local-only by configuration, bearer-token gated per client. |
| Ollama | `127.0.0.1:11434` | Your own local service; TESSERACT may start it. |

Changing `mirror.host` to `0.0.0.0` exposes an unauthenticated API to your
network. There is no authentication layer on the Mirror backend, because it
assumes loopback. Do not bind it publicly.

## Tool authority

Every tool call resolves to one of three postures before it runs:

- **auto** — runs immediately. Read-only tools, and the assistant's own interior
  state (mood, memory, diary).
- **ask** — you see an approval prompt and answer it. Writes, outbound calls,
  subprocess execution.
- **deny** — refused, non-negotiable.

`tesseract/config/permissions.yaml` is the authority. The shipped default is
`security_mode: max`, under which writes, outbound calls and subprocess
execution all prompt. Two other modes exist — `standard` for daily use once you
trust the setup, and `headless` for unattended operation, which auto-allows file
writes, bash, and agent creation. `headless` hands over materially more than the
other two; the config says so at the point where you would switch it.

Three rules sit underneath the policy and are not reachable from it:

- **The shell check list fires before policy lookup.** A set of hardcoded checks
  in `permissions/bash_security.py` classifies shell commands independently of
  `permissions.yaml`. Most are absolute denials covering audit evasion and
  attacks on the runtime and host; the rest force an operator prompt that no
  configuration can downgrade to auto. No hook, plugin, skill, or agent can
  relax them.
- **Kernel lockdown.** The assistant cannot write source under
  `tesseract/kernel/`. New tools are drafted by the assistant, reviewed by the
  operator, and installed by the operator.
- **The `app/` seal.** In a packaged install the application tree is sealed. The
  assistant writes to its workspace and your home directory, never to the code
  it is running.

When no operator is present to answer a prompt, read-only tools auto-allow and
everything else denies. Absence of an approver is treated as refusal.

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

## Secrets

API keys live in `.env` under your home directory, never in the code tree and
never in the repository. The build that produces the public tree ships config
that is either a hand-authored template or a file deliberately declared
identical for every install; a config file that is neither fails the build
rather than falling back to the developer's live copy. The build then runs a PII
and secret audit against its own output before publishing.

Terminal output and provisioning logs are scrubbed for credential-shaped strings
before being written or displayed, including credentials carried in URL userinfo
and query strings.

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
