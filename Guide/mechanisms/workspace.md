---
title: Workspace
description: The files the assistant keeps about itself.
---

The files the assistant keeps about itself.

These are plain Markdown. You can read them, edit them, and version them. They
are the difference between an assistant configured through a settings panel and
one that has something closer to a self.

| File | What it holds |
| --- | --- |
| `IDENTITY.md` | who it is |
| `SOUL.md` | how it wants to behave — refined over time, not expanded |
| `USER.md` | what it has learned about you |
| `HEARTBEAT.md` | what it noticed while you were away |
| `DIARY.md` | its own running notes |
| `TOOLS.md` · `MCP.md` | what it can do, in its own words |
| `VOICE.md` | how it speaks |

## Refined, not accumulated

The rule these follow is the same one memory follows: sharper beats bigger. A
soul file that grows every week is not learning, it is hoarding. The useful
version of these files is short and specific.

## They are yours

A neutral starter set ships with the app — that is what your assistant begins
as. From the first edit onward the files are yours: they live on your
machine, they are never committed, and no copy of them is kept anywhere
else.

One thing to be clear about, because it is easy to assume otherwise: several
of these files are **part of the prompt**. Identity, soul, and what it has
learned about you are sent to whichever model answers your turn, because
that is how the assistant knows any of it. If a role points at a hosted
model, that provider sees them. Point the role at a local model and it does
not. Which model answers is yours to choose — see
[Models and roles](../reference/models-and-roles.md).
