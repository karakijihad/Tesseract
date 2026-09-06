---
title: Was it the app, or the computer?
description: How TESSERACT tells its own faults from the hours your computer was asleep, shut down, or without power.
---

How TESSERACT tells its own faults from the hours your computer was asleep,
shut down, or without power.

Every hour, TESSERACT reads back what it actually did and reports what broke.
That report has to answer a question first, and for a long time it could not:
when nothing happened for eight hours, was the app broken, or was the computer
off?

Both look identical from the inside. A background job that did not run, a piece
of work whose last sign of life is hours old, a silence in every log at once.
Those are exactly what a real fault looks like, and exactly what a night's sleep
looks like.

## What it used to do, and why that was wrong

It measured elapsed time and guessed a cause from it. A piece of work that had
not checked in for ninety seconds was called dead. After a three hour sleep,
that is every piece of work on the machine, so the morning report announced a
pile of failures that never happened. Guess the other way and it goes quiet
about real ones.

## What it does now

Windows already keeps the answer, so TESSERACT reads it instead of guessing. The
system's own record says when the machine went to standby and came back, when a
shutdown was asked for and by which program, and whether the last one was clean.

That gives three plain answers:

- **Asleep.** The machine was suspended and resumed. Hibernating is reported
  the same way, because as far as the app is concerned they are the same thing:
  it was frozen, and then it was not.
- **Shut down.** Somebody or something asked for it. Where the record names the
  program that asked, the report says so, which is what tells "you chose to"
  apart from "Windows Update chose to". It never keeps your account name or the
  machine's.
- **Lost power.** The machine went without being shut down.

## What it does with them

A fault that began and ended while the computer was away is dropped from the
report, not softened. That distinction matters: a fault kept with an excuse
attached still reads as a fault everywhere its summary appears, and you would
still go and look. So it leaves, and the reason it left is recorded where you
go to check what was set aside.

Losing power is the exception, and it is reported rather than set aside. It is
the one of the three you can actually act on, and nothing else on the machine
will mention it.

## What it deliberately will not do

**It will not explain away something that is still wrong.** Only things that
happened and finished inside a window can be accounted for by that window.
Anything true right now stays in the report, however long the machine was away
beforehand.

**It will not invent an absence.** If the machine came back but nothing
recorded it leaving, TESSERACT does not assume it was away for however long the
records happen to reach back. It would rather explain less than explain wrongly,
because an unexplained fault still reaches you, while a wrongly explained one is
gone for good. The same rule decides which restart an unclean shutdown belongs
to: it has to be the restart that actually closed that absence, not merely the
most recent one on file.

**It will not treat "I could not read it" as "nothing happened".** If the
system record cannot be read, TESSERACT says that in as many words and keeps
blaming elapsed time exactly as it did before. Not knowing must never become an
excuse, because the safe direction is to call a dead thing dead.

## Where you see it

In the hourly report, as a line saying the machine was asleep, shut down, or
without power, how many times, and for how long. On a morning after a night
asleep, that line is the reason the rest of the report is short.
