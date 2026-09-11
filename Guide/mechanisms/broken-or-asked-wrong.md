---
title: Was it broken, or asked wrong?
description: Why TESSERACT records a tool that was handed a path that does not exist differently from a tool that is actually unwell.
---

Why TESSERACT records a tool that was handed a path that does not exist
differently from a tool that is actually unwell.

The assistant runs tools on your behalf all day. It reads files, searches the
web, sends messages, makes commits. Some of those calls come back with an
error, and for a long time every one of them was written down with the same
word: failed.

That word was doing two jobs at once, and they are not the same job.

## The two things that word covered

A tool can come back with an error because **something is broken**. The network
is down, the disk is full, the service it talks to is refusing calls. That is
worth knowing about, because it will keep happening until something is fixed.

A tool can also come back with an error because **it was asked for something it
cannot do**. The assistant asked to read a file at a path where no file exists.
It passed a setting in the wrong shape. It tried to start work that was already
finished. Nothing is broken here. The tool did exactly what it should: it looked,
it did not find, and it said so.

Recording both as failed has a specific and unfair consequence. The tools the
assistant uses most are the ones most often handed a path that does not exist,
simply because they are used the most. So the most reliable tool in the app ends
up looking like the least reliable one, and any count you read is measuring how
often the assistant guessed a filename wrong rather than how well the app works.

## What it does now

There are now two words instead of one.

**Failed** means the tool tried and something went wrong with the tool. This is
the one worth chasing.

**Asked wrong** means the tool worked correctly and the request could not be
satisfied. A file that is not there, a folder that is not there, a pattern it
does not support, work that was already done.

Both are still recorded, both are still visible, and neither is hidden. What
changed is that you can now tell them apart, and so can anything that counts
them.

The full list of words a piece of work can end on, and what each one means, is
in [Background work](/reference/pipeline/).

## Who decides which one it was

**The tool does, at the moment it returns.** Nothing guesses afterwards.

That sounds obvious and it is the whole design. There was an easier option
available: every tool already declares what it depends on, and it would have
been cheap to say that a tool with nothing behind it must be the caller's fault
whenever it errors. That would have been wrong in a way that mattered. Sending
a message on a chat channel is a tool with nothing behind it by that measure, so
a real outage at the far end would have been written down as the assistant's
mistake. A rule that turns a genuine failure into a false accusation is worse
than no rule.

So each tool says it for itself, at the point in its own code where it already
knows the difference. Reading a file that is not there says "asked wrong".
Failing to read a file that *is* there, because the disk refused, says "failed".

## Every tool has now been taught the difference

The whole roster was read once, one tool at a time, and the split written into
each tool's own error branches where it already knew which had happened.

It was done that way rather than by a rule applied over all of them at once,
because a guess here does not produce a gap you would notice. It produces a
confident wrong answer, which is harder to find and does more damage than
saying nothing. A tool that still says nothing reads "failed", exactly as it
did before, so the picture was never less accurate than it started.

## A third word, for the case neither of them covers

There is one more answer, and it is the one this whole area was built for.

A tool can come back reporting success and leave nothing behind that anyone can
check. It says it sent the message. Did it? The only account of that is the
assistant's own, and its own account of its own work is not evidence.

So tools that act in the world now hand back a **receipt**: the identifier the
far side gave them. A message id from the chat service. The hash of the file
that was written. The hash of the commit that was made. None of these is
invented here, which is the point of them. You, or the app tomorrow, can go and
look the thing up without trusting the sentence that reported it.

When a tool that should leave a receipt reports success and leaves none, the
record says **unverified**. Not a success, not a failure. Something happened
outside the app and there is no way to confirm what.

That word is counted in every total rather than quietly rolled into the green
ones. A figure that dropped it would be reporting only on the work that
happened to be checkable, which is the opposite of what a check is for.

A tool that had nothing to leave behind this time says so, and that is a plain
success. Asking a repository for its status is not the same act as making a
commit, even though one tool does both.

Every tool in the app declares which of these it is, and the app refuses to
start if one does not. A tool that can never leave anything says so in as many
words, and that includes the ones that only read. The declaration is a sentence
somebody wrote, not something worked out from how the tool happens to be
written, because a rule guessed from the code would have counted a tool nobody
had thought about as one that had been decided.

One case is worth knowing about on its own. Running a shell command changes
whatever the command changed, and nothing comes back naming it, so a run of the
shell is answerable for its own recorded command and output and nothing more.

Everything the app says on a channel now names its message, including the
notifications it sends you unprompted. A message split across several parts
names the first one, which is the one a reply quotes. Where a channel cannot
say which message it just sent, that call reads unverified rather than clean.

## Where to see it

**Conscience, under "What it did".** Pick a day, or turn on a range and pick
two. It opens on the most recent day that has anything in it.

The top of the panel is whatever was not a clean success, in the order it
happened, with the reason the runtime itself recorded. On a good day that band
says every call came back clean and there is nothing else to read.

Under it, one row per tool: how many times it ran, a bar of how those calls
went, and how many of them left a mark you could go and check. Open a row to
see the individual calls. Switch to "By turn" to see the same day as it
happened instead, which is the view you want when several things went wrong
close together and you suspect they were one event.

Records are kept for thirty days.

You can also just ask. "What did you do yesterday", "did anything fail
overnight", "what have you been using" are answered from exactly the same
records, in the cockpit or on a chat channel, so being away from the computer
does not mean being unable to look.

## Why this matters beyond a tidier label

Anything that judges how the app is doing has to count something. If the count
cannot tell a broken tool from a mistyped path, then every measurement built on
it inherits that confusion: health that looks worse than it is, reliability
figures that move when the assistant changes how it phrases a search, and alarms
for things that were never wrong.

Getting the words right is what makes everything counted afterwards mean
something.
