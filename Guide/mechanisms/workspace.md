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
| `SOUL.md` | who it is and how it sounds — refined over time, not expanded |
| `USER.md` | what it has learned about you |
| `DIARY.md` | its own running notes |
| `OPERATING.md` | how it works and what it holds to — the ethics included |
| `WORKSHOP.md` | how it lays out the work it does for you |

These carry instructions and character, and nothing else. What the assistant
*can do* — the tools it has, what each one is for, what runs on a schedule —
is generated into its prompt from the code that owns it, so no file here holds
a list that could quietly fall out of date.

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
