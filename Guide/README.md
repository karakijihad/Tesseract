---
title: TESSERACT
description: "An assistant that runs on your machine, remembers you across sessions, and asks, by default, before it writes to a file or runs a command."
head:
  # Starlight appends the site title to every page title, which on the one page
  # whose title IS the site title reads "TESSERACT | TESSERACT" in the browser
  # tab and in every search result. Overridden here rather than by changing the
  # heading: the page is called TESSERACT, and it should say so once.
  - tag: title
    content: TESSERACT
---

An assistant that runs on your machine, remembers you across sessions, and
asks, by default, before it writes to a file or runs a command.

![What it does](diagrams/L0-what-it-does.svg)

That drawing is the whole product in one line. The rest of this guide opens
each part of it up.

**[Download it](https://github.com/karakijihad/Tesseract/releases/latest/download/TESSERACT-Installer.exe)** (Windows) · **[Read the source](https://github.com/karakijihad/Tesseract)** (AGPL-3.0)

## Start here

- **[What it is](start/what-it-is.md)** — the idea, in a page.
- **[Install](start/install.md)** — getting it running.
- **[Setting up](about/setup.md)** — keys, models, and where your things live.
- **[Your first hour](start/first-hour.md)** — what to try, and what to expect.

## How it works

The system is drawn at three levels of detail. Read down only as far as you
need — most people stop at the second.

| | |
| --- | --- |
| **L0** | [What it does](diagrams/L0-what-it-does.svg) — no jargon, ten seconds |
| **L1** | [System context](anatomy/system-context.md) — every loop that touches a turn |
| **L2** | [The agent loop](anatomy/agent-loop.md) · [Voice](anatomy/voice.md) · [Memory](anatomy/memory.md) · [Autonomy](anatomy/autonomy.md) · [Delegation](anatomy/delegation.md) |

## Reference

- [Permissions](mechanisms/permissions.md) — what it will and will not do
- [Memory and the vault](mechanisms/memory-and-vault.md) — two stores, two jobs
- [Prompts](mechanisms/prompts.md) — what the model is actually told
- [Workspace](mechanisms/workspace.md) — the files the assistant keeps about itself
- [What it asks before doing](reference/permissions.md) — generated from the gate's own config
- [Tools](reference/tools.md) · [Models and roles](reference/models-and-roles.md) · [Configuration](reference/config.md) · [Costs](reference/costs.md)

## The project

- [Security](about/security.md) — what it assumes, what it refuses, what it cannot promise
- [Changelog](about/changelog.md) — what changed in each release
- [License](about/license.md) — AGPL-3.0, and what that means for you

Everything under `reference/` is generated from the running code, and the
diagrams and pages are checked against the same facts. A number that no
longer matches the code fails the build wherever it sits — a generated
table, a label inside a drawing, or a sentence on a page like this one.

The pages under `about/` are generated too, from the documents at the top of
the repository. Those files stay where GitHub expects to find them, and these
are rendered from them, so the two cannot come to say different things.
