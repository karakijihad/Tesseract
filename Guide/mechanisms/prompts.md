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

That is why the assembly stage in [the agent loop](/anatomy/agent-loop/)
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
[Workspace](/mechanisms/workspace/).

## Which tools it can see

Roughly half of what a turn costs before you have typed anything is tool
descriptions. Every tool the assistant has is always callable, but only some of
them are described to it on every turn; the rest it finds by searching, which
costs one extra step and nothing else.

Which ones ride is yours to set. **Conscience** shows every tool, how often you
have actually called it, and whether it is being carried anyway — that
intersection is the one worth acting on, because a tool you never call and load
every turn is pure cost. Turning one off there takes effect on the next turn.

The same list is a file, `config/working_set.yaml`, if you would rather edit it
in one pass. The names under `core:` are yours; the groupings and the
description beside each name are written from the tools themselves, so they
cannot drift from what the tools actually do. Everything not on the list is
written underneath it, commented out, so adding one is uncommenting a line.

This changes what the assistant can *see*, never what it is *allowed* to do.
Taking a tool off the list makes it slower to reach and no less permitted;
permissions are a separate question with a separate answer.

One name cannot be removed: the search tool itself. It is how the assistant
reaches everything not on the list, and without it, it can only reach what is.
