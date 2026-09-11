---
title: Skills, and how one gets judged
description: A skill is a procedure written down. The runtime measures whether it is working, and offers you a rewrite when it is not.
---

A skill is a procedure written down: a markdown file the assistant reads before
doing a kind of work it has done before. A **playbook** is a skill that declares
its procedure properly, with numbered steps naming the tool each one uses, what
must be true before it starts, and what the finished result looks like.

They live in your workspace, under `workspace/skills/`. They are yours: the
assistant drafts them, you approve them, and you can edit or delete any of them
by hand at any time.

## What the runtime records

Every time the assistant reads a skill, one line is written to a log. That line
says which skill, which revision of it, and how the read went:

- **ok** means the file was read.
- **error** means it could not be read at all. The file is missing, or it no
  longer parses.
- **correction** is added afterwards, when you correct the assistant in the same
  conversation. It records that a correction happened after the skill was
  consulted, and how far through the procedure the work had got.

That last one is the useful signal, and it is worth being clear about what it
is: evidence that consulting the skill was followed by you putting the
assistant right. It is not proof that the skill was at fault. The assistant may
simply have ignored it.

## How one gets flagged

Once a day, the runtime reads that log over a window you configure and asks one
question per skill: **of the times this was read and used, how often did you
have to correct the result afterwards?** If that crosses a threshold, it files
a card in your inbox.

Three things about how it counts are worth knowing, because each of them was
wrong once and produced a card that made no sense.

**It judges the revision that is live now.** A skill you have already improved
twice is not judged on the failures of the version you replaced. Those failures
were the reason you changed it. If the current version has not been used enough
to have a record yet, the runtime says so and waits rather than borrowing its
predecessor's.

**A correction is not counted as a use.** The correction is recorded beside the
use it is about, not instead of it. Counting both would put one event on both
sides of the sum and make a skill look better than it is.

**A file that will not read is a different problem.** It gets its own card,
which proposes nothing, because rewriting the words in a file is not an answer
to the file being missing. That card tells you which file to go and look at.

## What the card gives you

When the runtime has a model available, the card carries a proposed rewrite you
can apply with one press. It also carries **what was measured**: how many uses,
how many corrections, how far through the procedure the work had got each time,
and what was deliberately left out of the count. You are meant to read the
proposal against that, not against the headline.

**It quotes what you said.** When you correct the assistant, the correction is
saved as a memory, and the runtime records which one. That text is the
strongest evidence there is, so it goes on the card and to the model: not "the
work was corrected after step 3", but what you actually told it. Where a
correction has no record of your words, the card says the cause is unknown
rather than inventing one.

The model that writes the proposal is given the same evidence, and is told
plainly that it is an observation rather than a cause. It is allowed to answer
that nothing should change. That is a useful answer: often the procedure was
fine and the problem was elsewhere.

## If you say no, it stays no

Turning a proposal down is an answer about that text. The runtime remembers it
and will not offer a rewrite of the same file again. If you edit the skill by
hand afterwards, it is back in scope, which is the point: what you refused is
no longer what is there.

## Nothing changes without you

Approving the card is what replaces the live file. The version it replaces is
kept, so you can go back to it. The revision is numbered by the runtime, never
by the model, and if the skill has changed since the card was written the
runtime refuses to apply it rather than quietly rewriting the newer file: a
proposal is about the text it was written against.

Separately, if a revision measures worse than the one before it, the runtime
retires it and tells you. It does not swap one back in for you. Returning to an
earlier version is your decision, on the card.

## Did the rewrite work

A skill is rewritten because its work kept being corrected. Whether that helped
is a separate question, and the runtime now answers it: every place that shows
you a playbook's record says how the live revision compares with the one it
replaced. Better, worse, or too early to say.

Expect **too early to say** most of the time, and read it as honest rather than
evasive. A revision written last week has been used a handful of times, and a
handful of uses either side of a change is not a comparison. It becomes an
answer once both versions have a record worth reading.
