---
title: When a conversation gets long
description: What happens when a conversation reaches the model's ceiling, and what carries over.
---

What happens when a conversation reaches the model's ceiling, and what carries
over.

## Every conversation has a ceiling

A model can only read so much at once. Everything you have said, everything it
has said back, and every file and page and command result it looked at along
the way are re-sent on every single turn, so a long conversation is not just
approaching a limit. It is getting more expensive and slower with every
exchange, all the way there.

The ceiling is a share of the model's window rather than a fixed number, so it
follows the model you are using. It ships at a quarter. You can move it in
Settings under Chat.

## What happens when it is reached

The assistant does not start forgetting the middle, and it does not quietly
compress what you said into something shorter. It stops, writes down where the
work stood, clears the conversation, and carries on from what it wrote.

That is one act, and it is the same act every time:

1. **Reflect.** A separate model turn reads the conversation and writes what it
   taught into memory as ordinary notes you can open.
2. **Write the record.** What the work was, what is done, what is left, what
   happens next, and which files and tasks it touched.
3. **Archive.** The whole transcript is copied and stays searchable.
4. **Clear.** The conversation empties. It keeps its name and its place in your
   list, so nothing moves under you.

Then one of two things happens, and it depends on whether there is work left.

## Continue, or stop

If the work goes on, the cleared conversation opens with the record, written as
a message you can read:

```
This conversation was consolidated and cleared, and the work carries on here.
What was said is archived and searchable with `recall_history`; what it taught
is in memory. This is where the work stood.

Objective: ship the export feature
Phase: writing the tests
Done:
- the endpoint and its schema
- the failure path for a missing account
Remaining:
- the test for a partial export
- the changelog line
Next: write the partial-export test
Open questions:
- should a partial export still send the receipt email?
Artifacts:
- src/export/handler.py
- the partial-export task
```

If the work is finished, nothing is carried over and you are simply told the
conversation ended.

Either way you can read exactly what it thinks it was doing, and correct it if
it is wrong. That is the point of writing it down rather than summarising: a
summary is prose about a conversation, and this is a statement of where the
work stands, in the assistant's own words, which you can argue with.

## Who decides

Two things can start it, and they are recorded differently so you can tell them
apart afterwards.

- **The ceiling.** The conversation got too big. The runtime decides this and
  nothing else.
- **The assistant.** It finished a phase and judged that a clean start would
  serve the work better than carrying everything forward. It can reach this
  at any size.

Whether the work continues or stops is always the assistant's answer, never the
runtime's. The one thing the runtime does refuse is a continue that nothing is
owed for: if the previous boundary reported nothing left to do, or the same
next step has come back several times running with nothing finished in between,
it stops instead and writes down why. A conversation that ended because it was
going in circles and one that ended because the work was done are the two things
you most need to be able to tell apart later.

And it can decline to do any of it. Carrying on is a real answer, not a
failure, and it is what happens on most turns.

## A turn is never cut in half

If the ceiling is crossed while the assistant is mid-answer, in the middle of
a run of tool calls, nothing happens to the conversation at that moment.
Clearing it there would take away the question it is answering and the results
it has already gathered.

Instead the ceiling is lifted for the rest of that turn and the crossing is
remembered. The turn finishes normally. The moment it ends, the boundary runs.
One crossing, one boundary, and the answer you were waiting for arrives intact.

The only thing that can still interrupt a single turn is the hard limit on one
request, which trims the largest tool results to keep the request from being
rejected outright. That is a last resort and it is recorded when it happens.

## When it cannot happen

A boundary needs somewhere to put the transcript before it clears one. If that
write fails, or the app cannot reach the conversation the turn ran in, the
boundary does not happen and the conversation is left exactly as it was.
Nothing is rewritten and nothing is lost. You get an error in the app saying so,
and the next turn tries again.

Nothing else is tried in its place. There used to be a fallback that summarised
the middle of the conversation instead, and the one branch it ran on was the
branch where the copy had just failed to be written.

## What this buys you

**It costs less.** A conversation at the ceiling re-sends everything on every
turn. After a boundary it sends a page. On a long piece of work that is the
difference between the cost climbing all afternoon and it staying flat.

**It stays sharp.** The alternative most tools use is to summarise the middle
and keep going, then summarise the summary, and again. Detail drains out a
little at a time and nobody can see it happening. Here the record is written
once, by an assistant that still has the whole conversation in front of it,
and it is never rewritten.

**It survives a restart.** The record is on disk. Close the app, come back
tomorrow, and the work picks up from the same place.

**Nothing is thrown away.** The transcript is archived and searchable, so
anything the record left out can still be found by asking for it.

## What you can change

The ceiling, in Settings under Chat. Lower it and the assistant consolidates
more often, holding less in front of it at any moment and spending less per
turn. Raise it and it carries more at once and pays more for each exchange.

There is nothing else to set. How much survives a boundary is not a number any
more: what carries over is the record, and the record is as long as the work
needs it to be.

## Where this is going

Two things are being worked on rather than settled.

**The right ceiling is measured, not argued.** A quarter is a reasonable
starting point and nothing more. The same task run at several settings, priced
from the real ledger, is what should decide it, and until that has been run the
number is an opinion.

**The assistant should see a boundary coming.** It is told how full the
conversation is on every turn, so it can choose to stop at a clean point rather
than being interrupted at whatever it happens to be doing when the ceiling
arrives. A boundary the work chose is always better than one it hit.
