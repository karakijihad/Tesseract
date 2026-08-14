---
title: Voice
description: Voice is the primary way in, not a bolt-on.
---

Voice is the primary way in, not a bolt-on.

![Voice](../diagrams/L2-voice.svg)

## Hearing

The microphone produces a continuous stream. Voice activity detection separates
speech from silence, and speech is transcribed as it arrives rather than after
you stop talking.

## The gate

Then the interesting part. Not everything said in a room is addressed to the
assistant, so a transcript has to clear a **wake gate** before it becomes a
turn.

The match is deliberately fuzzy rather than exact. Speech-to-text renders the
same name differently from one utterance to the next — sometimes as one token,
sometimes as two — so the gate scores the leading words by edit distance across
a window either side of the expected phrase width, and compares that score to a
threshold.

A transcript that fails is **discarded down a different path**. It does not
become a quiet turn, and it does not become a turn you have to undo.

## Speaking

The reply is spoken sentence by sentence as it is generated, starting with the
first sentence rather than waiting for the last — which is what makes it feel
like an answer rather than a recital.

Speaking while it speaks cancels playback. You never have to wait your turn.
