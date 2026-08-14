---
title: Prompts
description: What the model is actually told, and how it is assembled.
---

What the model is actually told, and how it is assembled.

## Assembled per turn, not stored

There is no single prompt file. Each turn builds its own from parts: who the
assistant is, what it has been told to remember about you, what was recalled
for *this* question, what tools exist, and what has happened since you last
spoke.

That is why the assembly stage in [the agent loop](../anatomy/agent-loop.md)
has three boxes in front of it. By the time the model sees anything, the turn
has already been priced, drained, and given its memory.

## Manifest by default

The prompt lists what is available rather than inlining all of it. The
assistant reads what it needs when it needs it, instead of carrying the whole
library into every exchange. The result is a smaller, faster, cheaper turn that
can still reach everything.

## The workspace is part of it

Several parts of the prompt come from files the assistant maintains about
itself — its identity, its standing instructions, what it has learned about how
you work. Those are ordinary Markdown files you can open and edit. See
[Workspace](workspace.md).
