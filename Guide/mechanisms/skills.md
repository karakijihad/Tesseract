---
title: Skills, and how one gets fixed
description: A skill is a procedure the assistant wrote down after doing something once. There is one kind, one way to make one, and one way to fix one. This page is the whole design, the reasoning behind it, and what happens from start to finish.
---

A skill is a procedure written down: a markdown file the assistant reads
before doing a kind of work it has done before. There is one kind of it. What
you might have called a playbook elsewhere in this Guide is the same file:
numbered steps, the tool each one uses, what must be true before it starts,
and what the finished result looks like.

They live in your workspace, under `workspace/skills/`. They are yours: you
can read any of them, and edit or delete one by hand at any time. What you
cannot do is write one with an ordinary file tool. `workspace/skills/` refuses
`file_write`, `file_copy` and `file_move` in every mode, because a file
dropped there directly would skip the checks below. A skill is written only
through the two tools that run those checks, `skill_create` and
`skill_refine`.

## The design in four rules

Everything else on this page follows from these.

1. **One kind.** Every skill owes the same contract, gets the same security
   check, and keeps the same history. There is no lighter kind that skips any
   of it.
2. **One way in.** Every new skill, whoever suggested it, is written by
   `skill_create` through the same checks.
3. **One way to change one.** Every change to an existing skill, whether a
   fix, an undo or a retirement, goes through `skill_refine`, and every one of
   them is a card in your inbox.
4. **The assistant that used a skill says when it went wrong, and the record
   backs it up.** Nothing guesses that a skill was at fault from the logs
   alone, and nothing rewrites a skill on a timer.

## Why it is built this way

The earlier design had two kinds of skill, four separate ways to create one,
two ways to change one, and a nightly job that tried to work out from the logs
which skills were misleading the assistant. Each piece made sense alone.
Together they had three problems, and each rule above answers one.

**The lighter kind escaped the checks.** A skill that was not marked as a
playbook skipped the credential and path scan when it was written, and was
overwritten without keeping the version before it. Every skill on the machine
that ran this design was already the full kind, so the lighter one was a gap
with nothing in it. Removing it closed the gap without changing a single
existing file.

**Four ways in meant nobody checked for duplicates.** Each creator knew
nothing about the others, so the same procedure could arrive three times under
three names. With one door, a duplicate is a question that can be answered in
one place.

**Guessing from the logs never produced a finding.** The nightly job counted a
skill as "corrected" whenever you happened to correct the assistant about
anything in a conversation where that skill had been read. That is a
coincidence, not evidence. It could tell that something went wrong, never
why, and so it asked a model to rewrite a procedure from a tally. The
assistant that actually followed the steps is the one that knows which step
failed, so its report now leads, and the usage record is what corroborates it.

Two further decisions came out of the same review. **A skill is live the
moment it is made**, because the decision about who may make one has already
been taken by the time it exists, and a draft that waits to earn its place by
succeeding twice is a draft that is never used. And **no usage count decides
which skills are described in full on every turn**: a skill is added to that
list when it goes live and removed when it is retired, and nothing else moves
it.

## What a skill declares

A skill's file starts with a block of fields, and the body below it is the
procedure in prose.

| Field | What it says |
| --- | --- |
| `trigger` | the situation that should make the assistant reach for it |
| `use_when` / `not_when` | when it applies, and the near misses where it does not |
| `preconditions` | what must already be true before step one |
| `steps` | numbered steps, each naming the one tool it uses, or none |
| `forbidden-tools` | tools the procedure must never call |
| `expected_result` | what done looks like, so the assistant can check |
| `failure_modes` | the ways it is known to go wrong |
| `evidence` | the work it was written from |
| `version` / `status` | set by the runtime, never by whoever writes the skill |

A file that leaves a field out still loads, and the missing field is reported
as owed rather than hiding the skill. That is deliberate: refusing to load a
file you wrote by hand would make it silently vanish.

Some gaps do block, because a skill with them could not be followed safely: a
step naming a tool the runtime does not have, a step naming a tool the skill
itself forbids, a declared but empty list of steps, anything that reads like a
credential, and a path outside your home tree or one that cannot be shown to
be inside it (a network share, a variable, a tilde, a drive-relative path).

## The whole process, start to finish

### 1. Something suggests a skill

Three things can, and all three end at the same door:

- **The assistant, on its own judgement**, after finishing work it decides was
  new or hard enough to be worth keeping, or when a task you accepted as done
  took enough tool calls to be worth writing down.
- **You, asking for it.**
- **The observer**, the second model that watches the conversation. It cannot
  write a skill itself. On any turn it may notice that the same multi-step
  work keeps being redone, and leave that as a suggestion. The assistant reads
  it and decides whether to act on it.

### 2. The door checks it

Before anything is written, `skill_create` asks, in order:

- Is the name already taken by a live skill, one waiting for approval, or one
  you turned down before? If you turned it down, the refusal repeats your
  reason.
- Does another skill already use exactly the same sequence of tools? Then it
  is the same procedure under a different name, and it is refused with the
  name of the one that exists. The one exception is a task you accepted as
  done: a second success on the same steps is added to the existing skill as
  more evidence rather than refused.
- When nobody is attending the call, are there already too many drafts waiting
  for you? Then it stops rather than flooding your inbox.
- Does the rendered file read back cleanly, and does it pass the blocking
  checks in the section above?

A proposal that fails any of these is refused and nothing is left on disk.

### 3. It goes live

A skill that passes is written to `workspace/skills/pending/` and filed as a
card in your inbox. Approving the card is what makes it live: the folder moves
into `workspace/skills/`, it is marked active, and its name is added to
`workspace/skills/carried.txt`, the list of skills described on every turn.

Under the mode built for unattended work, that happens on its own at the
moment the skill is made, and the card is filed already answered so you can
see it happened. Under the shipped default it waits for you. Either way, the
same function does the move, so a skill approved by hand and one approved
automatically end up in exactly the same state.

Turning a card down moves the draft to `workspace/skills/rejected/` with your
reason beside it, which is what the door reads the next time the same name is
proposed.

### 4. The assistant uses it

Before starting work that looks like something it has solved before, the
assistant checks whether a skill matches. Skills on the carried list arrive on
every turn with a line saying when to use them. Any other skill arrives as a
name alone and is looked up with `playbook_search` when it looks relevant.
That split is about how cheaply a skill is found, never about which skills the
assistant is allowed to use.

Every time a skill is read, one line is written to a usage log: which skill,
which revision, and whether the read went through. A use that goes well
writes nothing more.

### 5. It goes wrong, and the assistant says so

If a step fails, turns out to be wrong, or is missing, the assistant that was
following the skill calls `skill_refine` with `report`, naming the skill, the
revision and the step. It never reports a clean use. Under the shipped
default it asks you before the report is made, the same as any other call to
that tool. Two things happen:

- A correction is recorded against that revision in the same usage log, so the
  record of how each skill has done stays in one place.
- A second agent, the skill fixer, is started in the background.

The assistant that hit the trouble does not rewrite the skill itself, and it
does not get to tell the fixer what went wrong. The fixer is handed pointers
only: the skill's folder, the revision and step reported, and where the record
of that turn is on disk. It reads both itself, because the agent that failed
cannot always tell a wrong procedure from one it did not follow, and those two
call for different answers.

### 6. The fixer decides

If the record shows the procedure was not followed, or does not show enough to
decide, the fixer stops and says so. A skill that was not the problem is not
rewritten because something else went wrong.

If the procedure itself was wrong, the fixer writes a corrected file and files
it with `skill_refine` `revise`, together with a fingerprint of the exact file
it read. It writes at most one revision per report.

### 7. The revision is applied, or waits for you

The revision is a card. When it is applied, the runtime checks the live file
still matches the fingerprint. If the skill changed in the meantime, the
revision is refused rather than landed on top of something newer than what it
was written against, and the card says so.

If it still matches, it runs the same blocking checks a new skill does, the
current version is copied into `workspace/skills/<name>/history/<version>/`,
and the new text goes live as the next version number.

Who applies it follows your mode, like every other change you did not write
yourself: it waits for you under the shipped default, and is applied on its own
under the mode built for unattended work, filed as answered so you can read
what changed.

### 8. Undoing a revision

Nothing in this process deletes anything. If a revision turns out worse,
`skill_refine` `revert` files the same kind of card with an earlier kept
version as the proposal. Applied, that earlier text becomes live again as a
new version number, and the version it replaced stays in `history/`. An undo
is a restore, never a withdrawal that leaves the skill unavailable. Reverting
a skill you retired brings it back: it is marked active again and returns to
the carried list.

### 9. Retiring a skill

**Retiring a skill is always your call**, in every mode, with no exception.
The assistant can ask, with `skill_refine` `retire`, whether a skill should
stop being used. That files a card, and the card is held for your answer even
in the mode where nearly everything else applies itself. The whole worth of
that verdict is that it came from somebody other than whoever did the work: an
assistant free to retire its own procedures would be grading its own homework.

Approving it marks the skill retired and takes it off the carried list. The
file stays on disk. The card reaches you the same way any other card does, so
it is answerable from your phone as readily as at your desk.

## Seeing how a skill has done

`playbook_record` reads the usage log alongside the turns each skill was used
in and what those turns cost, and reports how often each skill was read, how
often a use of it was reported as going wrong, and whether its live revision
has done better or worse than the one it replaced, or whether it is too early
to say. It writes nothing. It is where to look before deciding whether a
skill is worth keeping.

## Who can do what

| Act | Who starts it | Shipped default | Unattended mode |
| --- | --- | --- | --- |
| Create a skill | the assistant, you, or the observer's suggestion | asks before drafting, then waits on the card | goes live on its own |
| Report a skill went wrong | the assistant that followed it | asks before reporting | recorded, and the fixer starts |
| Revise a skill | the fixer | asks before filing, then waits on the card | applied on its own |
| Revert a revision | anyone | asks before filing, then waits on the card | applied on its own |
| Retire a skill | anyone may ask | asks before filing, then waits on the card | waits on the card |
| Write a skill file directly | nobody | refused | refused |

In the shipped default a card waits for you whoever filed it. Every row apart
from the last is a line in `permissions.yaml` and so is yours to change. The
last row is a path rule, and [Permissions](/mechanisms/permissions/) explains
why path rules outrank the tool.
