---
title: Changelog
description: "What changed in each release, and what it means for you. Newest first."
---

<!-- Rendered from CHANGELOG.md at the repository root by
     tesseract/scripts/generate_guide.py. Edit that file, not this
     one; CI regenerates this page and fails if it differs. -->

What changed in each release, and what it means for you. Newest first.

Every release is installable from the same link —
**[TESSERACT-Installer.exe](https://github.com/karakijihad/Tesseract/releases/latest/download/TESSERACT-Installer.exe)**
— which always points at the current version. If TESSERACT is already
installed, you do not need it: the app offers you the update itself.

## 1.1.1

The install stops being a black box, and the source is public.

- **The source code is open.** TESSERACT is AGPL-3.0. You can read every line
  of what runs on your machine, which for something with this much access to
  your computer seems like the least it can offer.
- **The permanent download link works.** Until now the link in the README
  pointed at a file no release actually carried. Every release from here
  publishes the installer under one unchanging name, so a link you bookmark or
  send to someone keeps working.
- **You can see what the first run is doing.** Setup used to go quiet for
  several minutes with no way to tell installing from hung. It now shows the
  step it is on, the file it is fetching, and how far through that file it is —
  live, as it happens, for every stage.
- **Declining a download actually declines it.** The setup list let you switch
  optional components off, and some of them downloaded anyway. Turning
  something off now costs zero bytes, on that run and on every later launch.
  Graphics-card acceleration was the worst of these — unticking it installed
  well over a gigabyte regardless.
- **Setup asks for your API keys.** All of them, in the same flow that asks for
  the name — each one optional, each with the address to get it from, and all
  of them changeable later under **Settings → API keys**. A missing key used to
  surface as an error in the middle of an answer.
- **Speech recognition uses your graphics card when you have one.** The right
  libraries were missing, so it silently ran on the processor instead and every
  spoken turn took the better part of a minute. Transcription is now fast
  enough to feel immediate, and the voice it speaks with is quicker to start.
- **Replies stop vanishing.** A turn could end having said nothing at all — no
  answer, no error, nothing to retry. Several separate causes; all of them
  fixed, and anything that still fails now says so.
- **It tells the truth about what is installed.** A failed check used to read
  as "nothing is there", so the app reported models as missing when they were
  present, and offered to download them again.
- **Every launch checks itself.** What this machine has is compared against
  what this version needs. Anything you already agreed to is repaired without
  asking; anything else is offered, not assumed. If nothing has changed it says
  nothing at all.
- **The setup window is readable.** It was a third too short for its own form,
  which clipped both edges. It is now resizable, with sensible sizes per step.
- **Opening it twice tells you so**, rather than showing a window with nothing
  behind it.

## 1.1.0

You name it now.

- **The assistant arrives unnamed, and first run asks.** It asks what to call
  it and what to call you, then keeps that name everywhere — the header, how it
  talks about itself, and the wake phrase built from it. Change any of it later
  in the **Identity** tab, which also holds the voice and the documents that
  describe it.
- **First run asks before it downloads.**
- **Say its name to talk to it.** Optional wake phrase, off until you turn it
  on.
- **Spoken replies are written to be heard**, composed separately from what
  goes on screen, so answers stop being read-aloud walls of formatting.
- **Two local voices.** Kokoro leads on naturalness with Piper behind it, which
  is several times faster than realtime on a processor — so a slow machine
  still speaks. Both run on your machine.
- **Point any role at a local model.** Name an Ollama model for a role and it
  gets installed for you.
- **It knows where your work lives.** Projects give it somewhere to keep work
  rather than one undifferentiated pile.

## 1.0.9

- Formulas render properly in chat instead of as raw LaTeX.
- New bottom bar: stage controls and view tabs in two compact menus, with the
  mic, model and observer one click away. It adapts to small windows instead of
  overlapping itself, and tucks away entirely if you want it gone.

## 1.0.8

- The observer is back — a hung provider call could freeze it silently forever,
  and a settings reload could quietly disconnect it.
- Voice transcription works on machines with no NVIDIA setup, falling back to
  the processor automatically instead of failing.
- External MCP tools work on fresh installs again.

## 1.0.7

- Quits and updates are recognised as planned rather than as crashes.
- Settings sections that fail to load retry until they succeed.
- Every failure leaves a trace: the app captures its own output, and interface
  errors are written to disk.

## 1.0.6

- Quitting and updating are fast and clean — no more thirty-second hangs.
- Settings recover automatically after a backend restart.
- Downloaded voice models stopped appearing as alarming "local changes".
- TESSERACT ships with a working voice out of the box.

## 1.0.5

**TESSERACT updates itself from here.** When a new version is published the app
shows a chip; one click downloads the installer, verifies it, and restarts into
the new version. This was the last installer anyone had to run by hand.

## 1.0.4

First-run fixes: docked rails keep their inset, failed turns recover on their
own instead of leaving a permanent error, Settings gained an About block that
works even while the backend is down, and a detached Ollama no longer locks the
app folder against its own update.

## 1.0.3

Fixed a first-run crash caused by a missing terminal dependency, and made the
next failure diagnosable: a missing terminal backend now degrades the terminal
panel instead of killing everything, and console output is captured to logs.

## 1.0.2

**Repaired installs from 1.0.0 and 1.0.1, which could not start at all.**

Two directories never reached the release, dropped silently while publishing —
the identity scaffold, and a backend package. The published copy carried an
ignore list written for a repository where those files were already tracked; in
a fresh one the same rules delete real content. Fourteen files went that way.

Separately, a leftover shutdown request could make the app permanently
unstartable: written for a backend that had already died, it was never
consumed, so every later launch obeyed a stop order it had not asked for.

Both fixed, and the publishing step now fails rather than quietly shipping
without something.
