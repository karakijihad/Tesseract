---
title: What it is
description: TESSERACT is a runtime. The thing you talk to is the assistant; the runtime.
---

TESSERACT is a runtime. The thing you talk to is *the assistant*; the runtime
is what gives it memory, tools, permissions and a schedule. The distinction
matters because the model is swappable and the runtime is not — you can point
the assistant at a different provider tomorrow and everything around it,
including everything it remembers about you, stays exactly where it was.

![What it does](../diagrams/L0-what-it-does.svg)

## Three things that are unusual

**It asks before it changes anything.** Not as a setting you can forget to
turn on — as the posture it ships in. In that default mode, writing to a
file and running a command both stop and ask. Reading is free, and so is
looking something up. There is a second mode for unattended work that
relaxes this, and you turn it on deliberately. See
[Permissions](../mechanisms/permissions.md) for the rest, including the
things that run without asking because you set them up that way.

**It remembers on purpose, not by hoarding.** Memory decays and consolidates.
The goal is a store that gets sharper over time rather than one that gets
bigger. See [Memory](../anatomy/memory.md).

**It keeps working when you are not there.** On its own schedule, under the
same budget and the same permission gate as when you are watching. See
[Autonomy](../anatomy/autonomy.md).

## What it is not

It is not a wrapper around one model. It is not a chat window with plugins. And
it is not autonomous in the sense of unsupervised — the whole design is built
around a gate that only you can open.
