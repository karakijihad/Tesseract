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

## Unreleased

- **The map tells you which records it means.** Where it said how many records
  disagree, or how many links point at something that is not there, it now
  names them and lets you ask about one. A broken link is asked about from the
  record that still names it, because the missing end is the half nothing can
  look up. Records that nothing points at can be shown on the map itself.
- **You can search your memory from the panel that describes it.** The room
  said how much was in there and gave you no way to look inside it.
- **A half finished call can be answered where you find it.** When the app
  restarts and cannot tell whether something it started actually happened, it
  asks you. That question was only findable in the inbox, and now opens from
  the row that reports it, with the same thread and the same answer.
- **Rows that cannot be opened say why, and where to look instead.** A record
  of a conversation that ended badly is not lost; it is in the Conscience
  panel, under Day, and the row now says so rather than implying nothing more
  is known.
- **The Autonomy panel can act, not just report.** Every room told you what was
  wrong and left you nothing to press. A row that reports a fault now offers
  the thing that answers it, and Health is five tabs, so the faults, the
  decisions and the journal are separate pages instead of one wall.
- **Every fault says who has it.** Each row tells you whether the runtime
  repairs it unasked, whether it tried and stopped, or whether nobody has it
  yet. That is the difference between waiting and it being your turn.
- **Leaving a fault alone no longer silences it for good.** The button promised
  the row would come back if the fault returned. For faults that are reported
  continuously it never did, and the row stayed quiet for ever. Accepting one
  now lasts until the fault ends, so a fault that comes back is shown again.
- **Two kinds of fault could not be acknowledged at all.** A fault about the
  runtime as a whole, rather than one named part of it, was listed under a
  label the button could not match, so pressing it did nothing. That was true
  on every surface, the phone included.
- **Stopping keeps what you typed.** Anything you sent while the assistant was
  working used to be thrown away along with the turn you stopped. It waits now,
  and runs as soon as the stopped turn is finished, which is what a queued
  message was always for. Work running in the background is left alone: a stop
  ends what the assistant is doing for you here, not everything it has started.
- **A stopped turn says it was stopped.** In the app the reply dimmed and said
  nothing, and on a channel the "thinking" bubble stayed there for good. Both
  now say the turn was interrupted, what it had already done still stands, and
  that your next message carries the conversation on from there.
- **A command you stop is actually stopped.** Stopping a turn left any shell
  command it had started running, with nothing reading it. So did a command
  that ran past its time limit. Both now end the command and everything it
  started. A command deliberately left running in the background is untouched.
- **A message sent while the assistant is working reaches it.** On a channel
  you are told it will be read at the next step. If the assistant was already
  past its last step, it never was, and the message vanished. It is answered
  now either way.
- **A stop reports one turn as one turn.** From a phone it said it had stopped
  two, every time, because one turn is carried by two pieces of work inside.
- **A message typed the moment the app opens is no longer lost.** The window
  opens before the conversation list has loaded, and for those seconds there
  was nowhere for a message to go. It was accepted, the box cleared, and
  nothing was sent. The box now waits until there is somewhere to put it, and
  anything that does not send stays where you typed it.

## 1.1.2

The assistant gets accounts of its own, a long conversation stops falling over,
and the panel that shows you what it is doing was rebuilt.

- **The assistant can have its own sign ins.** Settings, Credentials. You add
  an account, say who it belongs to and which addresses it may be sent to, and
  the assistant can use it without ever being shown the value. Each value is
  encrypted with your Windows sign in before it touches the disk, so the file
  opens on your account on this machine and nowhere else. An account can hold
  several boxes, because many services want an id beside a secret, and the
  assistant can ask you for one it does not have.
- **A value that got out is caught on the way back.** Every tool result in the
  app passes one point on its way into the conversation, and a stored value
  found there is replaced before it becomes part of the record. Four further
  places are checked: the conversation saved to disk, the request sent to the
  model, anything saved to memory, and the log files. The request to the model
  is the strict one. If your accounts cannot be read, nothing is sent.
- **A sign in carried inside a web address is blanked too.** A repository
  cloned with an access token records that token in its own address, and the
  assistant is told which project you have open at the start of every turn. It
  was going out with it. This one needs no account saved anywhere: the shape
  gives it away.
- **A long conversation no longer goes blank.** Past the size the assistant can
  send in one go, it used to keep the last three messages and throw the rest
  away silently, then be unable to recall work it had just finished. It now
  summarises the older part, keeps going, and tells you. What it drops first is
  tool output it can fetch again, never what you said.
- **Long answers stop being cut off part way.** The limit on a single reply was
  set to a fraction of what the model actually allows.
- **What a conversation costs is now counted properly.** Every kind of token
  the providers report is captured, each is priced from a rate a person
  checked, and summarising a conversation is billed rather than being free.
  Three of the six model prices had been wrong.
- **A conversation on your phone survives a restart.** It is saved the way one
  in the app is and comes back with its history. Work that finished while the
  app was down is handed over on your next message rather than sitting unread.
- **The Autonomy panel was rebuilt.** Every room opens with a sentence about
  what is in it, the trail at the top says where you are, and a new Atlas room
  draws the map of how everything is wired, including what it does not cover
  yet. A list of what needs you no longer quietly forgets an item.
- **The memory map opens on a question rather than on the whole library.** Pick
  where to start, narrow by kind of record or when it arrived, and connected
  records are drawn near each other. Labels no longer pile on top of one
  another.
- **The assistant can write down a procedure that worked.** A playbook says
  what problem it answers and when to use it, keeps its earlier revisions, and
  records how each one did. A correction you give lands on the playbook it was
  following.
- **Cards you put away keep running.** Minimising one used to take it off the
  page, so it stopped updating and reported itself gone.
- **If the app crashes over and over and gives up restarting, it now says so.**
- **Notifications say what they are and why in plain words.** Four kinds that
  nothing ever sent were removed, and what remains reaches every channel it
  names at once.
- **The mode meant to run without you now runs without you.** A handful of
  shell commands asked your permission whatever mode you had chosen, and away
  from the machine an unanswered question is not a pause, it is a refusal on a
  timer: the work carried on without the command and nothing said so. In that
  mode the one about short scripting one-liners now runs, and the other five
  are refused straight away with a reason instead of waiting half an hour for
  an answer that is not coming. The default mode is unchanged, and everything
  the app refuses outright is still refused in both. Every command that ran
  this way is in the approval record, marked as such.
- **A permission prompt for a command names every rule that stopped it**, not
  only the first, and says what your security mode does about each one.

Security fixes in this release: a crafted date could reach another chat's
transcript or a log elsewhere on the machine when looking up a past day; a
command line tool the app starts could inherit the application folder as its
working directory, where an edit would be erased by the next update; and the
workshop falling back to the top of your home folder, which sits directly above
your memories, your vault and your settings.

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
