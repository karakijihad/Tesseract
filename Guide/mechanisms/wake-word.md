---
title: The wake word
description: It hears your phrase and nothing else, on your machine, with no training and no recordings kept.
---

Saying the assistant's name should start a turn. Everything else you say in
the room should not.

That sounds like a small problem until you try to solve it by reading text.

## Why not transcribe first

The obvious approach is to transcribe everything and check whether the words
came out right. It is obvious, and it is what this used to do, and it does not
work.

A speech engine has to guess at a name it has never seen. Ask it to hear
*Tara* and it may write Tara, or Terra, or Tarah, or "ta ra". None of those
are mistakes exactly — they are reasonable spellings of a sound — but a
matcher comparing letters sees three different words. You can loosen the
comparison until the variants pass, and then unrelated speech starts passing
too. Tighten it and your own name stops working. There is no setting that is
right, because the problem is not in the setting.

Worse, transcribing first means everything gets transcribed. Something you
said to another person in the room has already been through a speech engine
before anything decides it was not addressed to the assistant.

## What it does instead

The gate runs a **speech recogniser that can only produce one phrase**. Your
wake words are compiled into the sounds that make them up, and the recogniser
is restricted to that sequence and nothing else. Ordinary speech does not come
out wrong — it does not come out at all.

Nothing is spelled, so nothing can be misspelled. And because the decision is
made from sound, it happens **before** transcription: an utterance that is not
the wake phrase never reaches a speech engine at all.

The model is small — a few megabytes, running on the processor rather than the
graphics card — and it is open source under a permissive licence.

## It tells you while you are still talking

The recogniser reads your speech as it arrives, so the phrase registers the
moment you say it — not when you stop. A green **heard you** marker appears
mid-sentence, and you can keep going knowing the rest is being taken.

This matters more than it sounds. Deciding at the end meant you could say the
phrase, talk for a minute, and only then find out nothing had been listening —
with no way to tell earlier, because nothing had decided yet. If the phrase
does not land, you know within a breath rather than a minute.

## It needs no training

This is the part worth being clear about, because most wake words do not work
this way. Alexa and Siri cannot let you choose a name, because a custom phrase
would mean training a new model on thousands of hours of audio. That is why
their names are fixed.

Here, your phrase is turned into sounds from a vocabulary the model already
knows. It takes milliseconds and it happens again automatically whenever you
change the name or the prefix. **Any two words, no training, no waiting.**

## Checking it hears you

What the guided run in Settings does is not training — it is checking, and
finding the right sensitivity for your voice in your room:

1. Say the phrase five times.
2. Read three short lines that do not contain it.

The first set has to be heard every time. The second set has to be heard
never. The app tries the tightest setting first and works down only as far as
it must, so you get the strictest sensitivity that actually hears you rather
than the loosest one that happens to work.

**If it cannot find one, nothing is saved.** You are told which half failed:
your takes were not heard, or your ordinary speech was. Those are different
problems with different fixes, and a setting stored from a run that failed
either way is a wake word that misbehaves while you have no reason to doubt
it.

You can run it as many times as you like. It is worth redoing if you move
rooms, change microphone, or find it firing when it should not.

## Choose a distinctive name

If your ordinary speech keeps firing the gate, the name is usually the
problem, not the setting.

A name that is also an ordinary English word appears in ordinary sentences. If
the assistant is called *Assistant*, then "the assistant said she would call
back later" contains the phrase — because it partly is the phrase. No
sensitivity separates them, and the honest answer is to change the name rather
than to widen the gate until conversation starts turns.

## Nothing leaves the machine

The recordings you make during the check are decoded in memory and dropped.
They are not written to disk, not uploaded, and not kept. What is stored is
your phrase and two numbers.

There is nothing there to leak, and the whole thing keeps working with no
network at all.

**And it does not listen through a mute.** When the microphone is off, it is
off — there is no low-power always-on path waiting behind it. The design could
support one and deliberately does not: a mute that is not a mute is a promise
you cannot take back.

## Three states, and they are different

|  | what happens |
| --- | --- |
| **Off** | Every utterance is dispatched. |
| **On, not yet checked** | Every utterance is dispatched. |
| **On and checked** | Only the phrase wakes it. |

The middle row is deliberate. Turning the switch on is permission; the check
is readiness. Until you have said the phrase and watched it land, the gate
stays open rather than going deaf — an assistant that hears nothing is far
worse than one that hears everything, and a sensitivity that suits one voice
may not suit yours.

Everything that can go wrong lands in that same open state. A missing model, a
setting file it cannot read, audio too short to decide on, a renamed assistant
whose new phrase was never checked, even a name the model has no sounds for —
all of them dispatch the utterance and say so. Only a confident miss discards.

That includes the moments just after the app starts, and just after you finish
the check. Loading the recogniser takes several seconds, and until it is ready
nothing has heard your speech at all — so those utterances go through rather
than being refused by something that was not listening yet. Nothing waits on
it either; the load happens in the background while the app stays responsive.

Renaming the assistant clears the check, because what was confirmed is that
two particular words are heard reliably, and the new name is not among them.

## Where it lives

`Settings → Voice` runs the check, and shows which of the three states you are
in. **Forget** clears it and returns the gate to hearing everything — that is
the whole undo, and it needs no config edit.

Two settings live in `identity.wake_word` in `mirror.yaml`:

- `min_threshold` — a floor you can raise above what the check found, if you
  want the gate stricter than your recordings implied. It cannot lower the bar
  below what was confirmed.
- `boost` — how strongly the recogniser favours your phrase over what it would
  otherwise hear. The shipped value is neutral — it favours nothing. Raising
  it makes a mumbled phrase more likely to land, and false wakes more likely
  with it.

Neither is a speaker check. Anyone who says the phrase wakes it.
