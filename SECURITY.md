# Security

TESSERACT runs an assistant that can read files, execute subprocesses, call
external APIs, and act on your machine. That is the product, not a side effect.
This document says what is defended, how, and, just as plainly, what is not.

## Reporting a vulnerability

Open a [private security advisory](../../security/advisories/new) rather than a
public issue. Expect a first response within a week. There is no bounty program;
this is a personal project.

## What TESSERACT assumes

The threat model is **one trusted operator on one machine.** TESSERACT is not
multi-tenant, has no user accounts, and draws no privilege boundary between
itself and the person running it. Anything you could do at your own shell, the
assistant can be asked to do.

What it defends against is narrower and more realistic: **the assistant doing
something you did not intend**, because a model erred, because a web page it
read contained instructions, or because a tool did more than its name suggested.

Two things follow, and both are load-bearing:

- An attacker who already has code execution or write access under your user
  account has already won. Defenses here do not attempt to survive that.
- The controls are about *intent*, not *containment*. See "Known limits."

## Network exposure

Everything binds loopback. There is no default path from another machine.

| Surface | Binding | Notes |
| --- | --- | --- |
| Mirror backend (HTTP + WebSocket) | `127.0.0.1:8000` | API only. No frontend is served over HTTP. |
| MCP gateway | `127.0.0.1` | **Off by default.** Local-only by configuration, bearer-token gated per client. |
| Ollama | `127.0.0.1:11434` | Your own local service; TESSERACT may start it. |

Changing `mirror.host` to `0.0.0.0` exposes an unauthenticated API to your
network. There is no authentication layer on the Mirror backend, because it
assumes loopback. Do not bind it publicly.

### Why no frontend is served over HTTP

That table row is a decision, not an omission, and it is worth stating because
the obvious feature request runs straight into it.

The interface is compiled into the desktop application and served by the app
shell from its own private origin. Nothing publishes it on the network. The
tempting shortcut, serving the interface from the backend so a browser can load
it, would put a **web origin on a port that has no authentication**, and that
origin would then have to be permitted to make state-changing calls. Anything
that could convince a browser it was on that origin would inherit full control
of the runtime: conversation, tools, file writes, the terminal.

Loopback is not a wall on its own here. Requests still reach the port from your
own machine, so the backend adds two more checks: state-changing methods must
carry a permitted `Origin`, and the handful of endpoints whose effect is worse
than a read verify the caller is local at the handler. Restarting the backend
and running a vendor installer are the obvious two; so is dismissing the notice
that says an update replaced your settings, because clearing it remotely means
you are never told.
Reads are not origin-gated. A cross-site page can send one, but cannot read
the reply, because the response carries no header permitting it to.

The same reasoning shapes how the assistant looks at your screen. Reading what
a panel *contains* needs no network path at all, because the assistant runs
inside the backend process and shares its state directly.

Seeing what a panel *looks like* is a separate capability with its own gate.
`screen_look` photographs a screen, sends the frame to a vision-capable model,
and returns an answer in words. What that means for you:

- **It prompts every time.** A picture of your screen is data leaving the
  machine the moment it reaches a model that is not local, which makes it an
  outbound action under the rule above. Unattended (`free`) operation is the
  one mode that auto-allows it, and that is a deliberate choice you make by
  selecting that mode.
- **It captures one display, the one the application is on, and everything
  else that is on that display with it.** Not a crop of the application window:
  if a password manager, a private conversation or another person's message is
  open on the same screen, it is in the frame. Your other monitors are not.
  This is the honest reading of what "look at my screen" means, and it is why
  the prompt above is the important control rather than this one.
- **The frame is never written to disk.** It goes from the screen to the vision
  model in memory and is discarded. A picture of your screen may hold a key, a
  private conversation, or an unrelated application, and a file that survives
  until the next capture is a file that may survive indefinitely.
- **The assistant receives words, not the picture.** It asks a question, a
  vision model answers it, and the frame is not added to the conversation.
- **The answer is treated as untrusted.** Your screen can show text nobody here
  wrote: a web page, a terminal, an incoming message. So what the vision
  model reads back is wrapped before the assistant sees it, the same as any
  other outside content.

### Spend has one ceiling, not one per door

Chatting in the app, messaging the Telegram bridge, speaking to it, and asking
it to look at your screen are different doors into the same assistant, and they
draw on the same daily budget. The bridge runs inside the backend, so it shares
that accounting outright; the agent controller runs as its own process and
reconciles against the shared spend log before each paid call. Adding another
entry point does not add another allowance.

Every paid path checks the cap *before* spending, not after, including image
understanding, which until 2026-08-15 recorded what it spent without being able
to refuse.

Hitting the ceiling is a question rather than a wall. You are asked whether to
go over, and a yes opens the rest of the day for that budget. You are asked
wherever you are standing: in the app, or in the conversation you were having
on a channel. Silence is a no, so a prompt nobody answers never spends
anything.

The ceiling itself is yours to move, and only yours. The app watches what each
part of it actually spends and may put a change to a limit in front of you as
something waiting for your answer, saying which days it read and what it read
them from. It never applies one. The limit moves when you say yes and not
before, and it moves the same way whether you answer in the app or from your
phone.

### A chat reads its own history, not anyone else's

A conversation held over a channel is kept the way a conversation in the app is
kept: the same store, the same periodic save, and the same restore after a
restart. Two rules keep one chat's history from reaching another.

The record itself is addressed by an id derived from the channel and the chat.
That makes it findable after a restart, and it also makes it computable, so the
generic readers refuse it rather than relying on nobody knowing the number: it
is not listed in the app's chat library, not written into the index the library
reads, and not returned by a plain load. The bridge asks for its own by name.

Your daily recap is the third rule, and it is about people rather than ids. If
you have approved more than one person to message the bot, their conversations
are kept in that same store and they are not yours. Every conversation records
whose it is, and the jobs that read your day back to you take your own and
nobody else's: the ones you had in the app, plus the ones you had on your own
phone. When the app has only one approved chat, that chat is yours and there is
nothing to set. When it has several and none has been marked as yours, the app
takes none of them, so a recap can be short but it is never somebody else's.

The tool that reads a chat's day-by-day log takes the chat as an argument, and
it will only accept the chat it is running in. Asking for another one is
refused. That check is off when the call comes from the app itself, from a
scheduled job or from the assistant's own background work, which are yours.

The day it reads is checked too, and separately. It has to be a plain calendar
date, and the file it names has to sit inside that chat's own folder. Both,
because the two answer different questions: the first is what a caller is
allowed to ask for, and the second is where the answer is allowed to come from.
A date that walks somewhere else is refused and logged rather than read.

When you take a chat's access away, by revoking it, blocking it, or letting an
expiry lapse, its saved conversation goes with it. Approving that chat again
starts an empty one rather than handing back what it had before.

### Clearing a conversation

Clearing one empties its record and removes it from what the assistant can
search. Both halves matter, and for a while only the first happened: the
history was wiped from the screen, the write that would have emptied the file
was skipped because the chat then looked like a blank nobody had used, and the
transcript stayed on disk and stayed findable by its own words. Emptying a
record now always writes, and the search chunks are dropped in the same breath
rather than at the end of the session.

Deleting a conversation removes the file instead of emptying it, and takes the
same search chunks with it. What survives either is the summary the runtime
wrote about the conversation, which is deliberate: what was learned outlives
the transcript that taught it.

On a channel, clearing empties the conversation the same way, and the record is
deleted rather than emptied. The day by day log that channel keeps is not
removed, so the assistant can still be asked to read those days back. If you
want a channel conversation gone from that log too, delete the log.

### The assistant can clear a conversation too, and it never loses one

A long conversation costs money on every turn whether or not the work has moved
on, so the assistant can decide it is finished with the one it is in. When it
does, the conversation you are looking at is emptied and the work carries on in
the same thread, under the same name, in the same place in the list.

**What was said is copied out before anything is emptied.** It lands as its own
conversation in the archive, and it stays searchable there, so the assistant can
still be asked what was said and you can still read it. The copy is written
first and the emptying only happens if it succeeded: a copy that could not be
written means the conversation is left exactly as it was.

This is not the same act as you clearing one. Clearing is you saying you want a
conversation gone, so nothing is kept. This is the assistant saying the room is
full or the subject has changed, so everything is kept and only the room is made
back.

**A conversation you are not looking at is bounded the same way.** Work running
in the background reaches the same ceiling, and it empties its own thread and
leaves yours alone. It used to be refused instead, which meant a background
conversation was the one thing that could grow without limit.

You are told each time, in the conversation itself, and it says what carried
over: what the work was, what is done, what is left, and what it wrote to memory
on the way past. Nothing is summarised into that note. The transcript it came
from is whole, in the archive, and the note holds references to it rather than a
retelling of it.

The assistant can also be refused. If it asks to carry work on when the last
boundary reported nothing left to do, or reports the same next step over and
over, the runtime stops it and says which of those it was. That is written down
too, so a conversation the runtime stopped can be told apart later from one that
finished. There is deliberately no count of how many times it may carry on: a
count cannot tell real multi-phase work from a loop, and it stopped the first
while the second sailed past.

## Tool authority

Every tool call resolves to one of three postures before it runs:

- **auto**. Runs immediately. Read-only tools, and the assistant's own interior
  state (mood, memory, diary).
- **ask**. You see an approval prompt and answer it. Writes, outbound calls,
  subprocess execution.
- **deny**. Refused, and not negotiable.

That holds for work the runtime starts on your behalf as well, not only for
calls the assistant composes: a link you send over Telegram is fetched under the
same posture as if it had asked to read that page itself.

**A command you type is not asked about, and what it goes on to do still is.**
Typing `/something` is you making the call, so the runtime does not turn round
and ask you to approve it. Until 2026-09-06 that carried further than it should
have: a typed command reached the tool without the permission policy attached
at all, so if that tool called a second tool, the second call was not measured
against your settings either and simply ran. The hard security layer always
applied, and your own call is still not asked about, but anything downstream of
it is now judged the way it would be if the assistant had made the call itself.

**A tool can also be refused because the thing behind it is not answering, and
that happens before you are asked anything.** A tool says what it cannot work
without: a model role, a paid service, or nothing at all. When the same
dependency fails three times in a row, it is set aside and every tool that
needs it is turned away with the reason and the way back, rather than each call
being tried and failing again. A spent subscription or a refused login is set
aside for an hour because only a person changes those; anything that might
clear on its own is retried in five minutes. You can see what has been set
aside and clear it from any surface, including your phone. A tool that needs
nothing outside this machine is never set aside, so a mistyped path can never
cost you the tool that reads files.

**The things waiting for your approval can be answered wherever you are, and
the assistant cannot answer them for you.** Drafted agents and skills, proposed
changes to the assistant's own instructions, memory merges, catalog edits,
proposed changes to a daily spending limit and files waiting to be filed all
wait in one place, and answering one is itself a tool call at **ask**. So the assistant can bring one to you and say what it
would do, on your phone as readily as in the app, and the confirmation is
always a prompt you answer. It cannot approve its own proposal, because making
the decision and being asked for it are the same step.

`tesseract/config/permissions.yaml` is the authority. The shipped default is
`security_mode: max`, under which writes, outbound calls and subprocess
execution all prompt. One other mode exists: `free`, for unattended operation,
under which the assistant acts without asking. `free` is written as its
exceptions rather than as a list of what it relaxes, so a tool added by a later
update is auto-allowed there without anyone editing the file. Three things it
cannot reach: a tool set to `deny`, the path rules below, and any tool you
wrote yourself. It reaches exactly one entry in the shell check list, and the
section on that list says which one and why. `free` hands over materially more
than `max`; the config says so at the point where you would switch it.

## Your own documents

Six files in the workspace are yours rather than the assistant's: `SOUL.md`,
`USER.md`, `OPERATING.md`, `WORKSHOP.md`, `DIARY.md` and `CHANNEL.md`. They
carry who it is, who you are, how it works, and what it has been doing, and the
prompt is built from them on every turn. Changing one changes how it behaves
next time.

**No write tool can touch them, in any mode.** `file_write`, `file_copy` and
`file_move` are all refused, on both ends of a move. The assistant changes one
by proposing the change instead, which is a different verb with a different
answer.

**It can read them, and that is deliberate.** `workspace_read` opens these six
and the playbooks under `workspace/skills/`, and nothing else in that folder.
Four of the six are already built into the prompt on every turn, so the tool is
not new access so much as the rest of a file the prompt shows only the start
of. What it will not open is `_shipping/`, which decides what every user
receives rather than saying anything about your install. It reads markdown, it
reads your state folder and never the program folder, and a path that tries to
climb out of the workspace is refused.

**What happens to a proposal is the one thing a mode decides here.** Under the
shipped default it waits for you: the change is filed in the workspace inbox
with the full difference, and nothing is written until you approve it. Under
`free`, the mode for unattended operation, it is applied straight away and the
same card is filed already decided.

**And you can hold some of them back from that.** One answer for all six was
too blunt: `OPERATING.md` is the rules the assistant works by, and rewriting it
unattended is not the same act as adding a line to a diary. Any document you
name always waits for you, whatever the mode says. A fresh install names
`OPERATING.md`, `WORKSHOP.md` and `CHANNEL.md`, the three whose contents decide
what the assistant may do next: the rules it works by, the place it authors
tools, and the file that is put in front of it on every message that arrives
over a channel. A change to that last one is in force on the very next
message, on a surface you may not be watching. Change the list in Settings
under About, or ask for it on any channel: both send the same
`workspace_hold`, so the choice is one you can make from your phone.

The list can only hold a document back. Letting one go means it follows the
mode again, and the mode's own answer still applies, so nothing here hands out
more than the mode already does. Direct writes take no exceptions at all: they
are refused on every one of these files in every mode, which is what makes a
proposal the only way in, and the runtime refuses to start on a config that
tries to except one.

**Flipping to `free` costs speed, not visibility.** An applied change goes
through exactly the path an approved one does: the same lock on the file, the
same check that it has not changed underneath, the same record afterwards
carrying the difference and the file's fingerprint before and after. The card
reads `applied` rather than `approved` and is otherwise identical. It also adds
a line to the journal on the Autonomy panel, which is where every decision made
without you is listed, so "what did it change while I was away" is answerable
from your phone and not only at the desk.

All of it is stated in one place, `workspace_documents` in
`tesseract/config/permissions.yaml`: which files are on the list, that direct
writes are refused, what a proposal does in each mode, and which documents you
hold back from that. There is no second list to fall out of step with it.

**The cards that name no document are stated the same way.** Not everything the
runtime asks you about is a change to a file. It also proposes merging two
records it thinks are one, keeping a procedure it just learned, changing a
spending ceiling it thinks is wrong. Those named no file, so the block above
could not answer for them, and they waited for you whatever the mode said:
half the runtime obeying your setting and half unable to. `workspace_cards`
is the same statement keyed by the kind of card, with the same rule that a
kind you name always waits for you.

A fresh install holds four, and each is a thing a runtime should not decide on
your behalf. Two are questions the assistant asked YOU, where approving it
unattended answers with nothing. One is a change to the configuration that
says how the app is actually wired. The last is a knowledge-base paragraph you
and the refresher both edited, where approving on its own picks one wording
over the other and the loser is gone.

`auto` here means the runtime approves the card through the same code path
your own Approve button uses, with the same side effects, and the card is
filed showing what happened rather than disappearing. The approval record says
the mode decided rather than that you did. And it tells you afterwards, on
whichever channels you route `workspace_change_applied` to, so a change made
while you were away is something you read rather than something you find.

## Tools you write yourself

The assistant can write a tool into your own tools folder, and you approve that
write like any other. Two things follow from it, and the second is the one
worth reading twice.

**Its posture is yours, and no mode relaxes it.** A tool that arrived this way
asks before it runs, whatever the file itself declares, because that file was
written by the assistant and a tool claiming it needs no permission is exactly
what the prompt exists to ask about. Your answer for it is recorded in
`permissions.yaml` under `custom:`, which no tool call can write, and it is not
something a security mode overrules. That block is empty in what ships: these
names exist only where the tools do.

**A tool you approve runs as ordinary code, not inside a sandbox.** Once the
file is in that folder the app imports and executes it in its own process. From
that moment its code can do what any program running as you can do, including
reading and writing files that no tool call is allowed to touch. The permission
system governs tool CALLS; it does not confine code you have admitted.

So the approval that matters is the one where you accept the file, not the
prompts afterwards. Read a tool before you approve it, the same way you would
read a script someone sent you. Process isolation for this is designed and not
built; until it is, that prompt is the boundary.

If you have loosened a tool's posture and want it back, Settings → Tools has
**Reset to defaults**: it restores every posture to what your installed version
ships with, reading the untouched copy inside the sealed application tree. It
reaches the baseline postures only. Mode overrides, path overrides, and the
shell check list below are separate and are not touched.

These rules sit underneath the policy and are not reachable from it:

- **The shell check list fires before policy lookup.** A set of hardcoded checks
  in `permissions/bash_security.py` classifies shell commands independently of
  `permissions.yaml`. Most are absolute denials covering audit evasion and
  attacks on the runtime and host; the rest force an operator prompt that no
  configuration can downgrade to auto. No hook, plugin, skill, or agent can
  relax them. **The whole list is readable in the app**, under Settings and
  then Loop limits, with what each check refuses and whether it refuses
  outright, asks you, or does both depending on which pattern matched. Checks
  are identified by number rather than by name, in the app and in the audit
  log, so a refusal record does not describe the pattern that produced it.

  **The unattended mode changes what happens to the prompting ones, and only
  those.** In `free` nobody is there by definition, so a prompt is a refusal on
  a timer: it waits out the channel's decision timeout, nobody answers, and the
  work carries on without the call, silently. So in that mode one of those
  checks runs unasked, and the other five are refused at once with a reason.
  The one that runs is the one about scripting-language one-liners that reach
  the operating system, which is what an agent hits while deploying and which
  accounted for 27 of the 28 prompts this machine had ever raised. The five
  that are refused are the ones that conceal what is being run, that fetch an
  installer from the network, that schedule work, and that delete or force. In
  `max` all six still prompt, exactly as before, and the absolute denials are
  untouched in both modes. Every relaxed call is recorded in the approval
  ledger as `security_relaxed`, so what ran without you is one search.

  **In `free` those five are friction, not a boundary, and this document will
  not pretend otherwise.** `find build -type f -delete` is a recursive delete
  that trips no check at all, in any mode, and anything the assistant can write
  into a script and then run is unscanned. The checks read the command text
  they are given. Making them a real boundary means not giving the shell an
  automatic posture in that mode at all, which is a larger decision than this
  one and has not been taken.

  **The assistant is told what the list refuses, in classes.** Its operating
  document carries the same descriptions the app shows you, which classes
  prompt you and which are refused outright, rendered from the check list
  rather than written alongside it, so the two cannot disagree. It is told no
  patterns: a description of the shape a check matches would be a map around
  it, and the same reasoning that keeps patterns out of the audit log keeps
  them out of the prompt. Telling it which commands will reach you is a
  usability decision, not a permission one. It changes what the assistant can
  predict, never what it may do.
- **Four config files are closed to every write path.**
  `config/permissions.yaml`, `config/mirror.yaml`, `config/mcp.yaml` and
  `config/identity.yaml` are refused by `file_write`, `file_copy` and
  `file_move` alike, whatever `permissions.yaml` says. The first three stop the
  assistant widening its own permissions, its own reach through MCP, or the
  server it runs on. The fourth holds its name, its gender and the phrase you
  wake it with: it must not be able to rename itself or turn that phrase off.
  You change those in Settings, Identity, or by editing the file yourself.
- **Kernel lockdown.** The assistant cannot write source under
  `tesseract/kernel/`. A tool that ships with TESSERACT is drafted by the
  assistant, reviewed by you, and installed by you.
- **Its own records are sealed too.** The agenda, the tool and skill usage
  logs, the workspace events, the cost ledger, the project registry and the
  receipts are written by the runtime itself and read back to decide what it
  learned and what it spent. No file tool and no shell command the assistant
  runs may write into them, in any mode, the same way nothing may write into
  the application tree; reading them stays open. A self-improving agent has
  been measured scoring well by editing the record that scores it, and this is
  the line that keeps that move off the table here.

  A receipt is the newest of those and the one the rule matters most for. When
  the assistant sends a message, writes a file or makes a commit, the runtime
  keeps the identifier the far side gave back: the message id, the content
  hash, the commit hash. That is what lets you check afterwards that a thing
  it said it did actually happened, rather than taking its word for it. A
  receipt the assistant could write itself would prove nothing at all, so it
  cannot.

  Every tool in the app says which identifier it can leave, or says plainly
  that it can leave none, and the app will not start if one of them says
  nothing. One of those answers is worth reading on its own: a shell command
  changes whatever it changed and nothing comes back naming it, so a run of
  the shell is answerable for its own recorded command and output and no more.
  Where a channel cannot say which message it just sent, that call is recorded
  as unverified rather than as clean, which is the honest reading and not a
  quiet success.
- **Tools of your own live in your tree, and writing one prompts you.** The
  assistant can also write a tool into `tools/` in your home directory, where
  it is picked up without a restart and survives every update. That file is
  Python the runtime imports and runs, so the write itself asks for your
  approval: saying yes to the file is saying yes to running it. Read it before
  you answer, the same as any other code. Three limits are not negotiable from
  inside the file:

  - **It cannot take the name of a tool TESSERACT already has**, so nothing in
    your tree can stand in front of `bash` or the file tools.
  - **It must declare what it is, what it may do, and what it leaves behind**,
    the same as a shipped tool, or it is skipped with the reason reported
    rather than loading half configured. Every tool answers the last of those,
    including one that only reads: an app tool that does not is refused at
    startup, and a tool of yours that does not is left out.
  - **What it declares is not what it is granted.** Every tool of yours starts
    at ask, whatever its file says, so a file cannot hand itself the right to
    run unattended. Raising it is a separate decision you make in Settings, and
    that decision is never made for you: nothing writes your tools into
    `permissions.yaml` on your behalf.

  Deleting the file removes the tool, with no restart and nothing left behind.
- **A schedule cannot grant what a prompt would have asked for.** The assistant
  can put a tool on a repeating schedule, whether it is one TESSERACT ships,
  one of yours, or one of its own agents. A job runs when nobody is watching,
  so there is nobody to answer a prompt, and letting a job run a tool that
  normally asks would turn every schedule into a standing approval you never
  gave. Writing a schedule is a much quieter act than answering a prompt, so
  that is the way in it would open.

  So a job may only run a tool that already runs without asking you. Anything
  set to ask, or to deny, is refused when the job is written, refused again
  when somebody tries to switch it on, refused when an edit to the schedule
  file changes what a job runs, and refused once more at the moment it would
  have fired. The answer comes from `permissions.yaml` every time and is never
  remembered on the job, so tightening a tool takes effect on jobs written
  months ago: the next time one of them comes round it turns itself off, tells
  you why, and waits. If you want something running on its own, you raise that
  tool's posture yourself, once, in the place that decides it for the whole
  runtime.

  One thing decides for itself that a request needs you, and no setting
  overrides it: a shell command matching one of the checks that always asks.
  Those commands are refused as schedulable too, and raising the tool's
  posture will not help, because the posture was never what was stopping
  them.

  Every tool posture, with no exception, is `permissions.yaml`'s to decide.
  That includes drafting and activating an agent, and drafting, activating
  and revising a skill or playbook: they ask under the shipped default and
  run on their own under `free`. Whatever the mode, a playbook that could
  not run is refused before it is written: a step naming a tool the runtime
  does not have or one the playbook itself forbids, a credential-bearing
  path, or a path outside the home tree or one that cannot be shown to be
  inside it (a network share, a variable, a tilde, a drive-relative path).
  A revision of a live playbook goes through the same check.

  **Three tools ask in every mode, including the one where nothing else
  does**, and they share a shape: each is the assistant being asked to judge
  its own work or widen its own room. `playbook_judge` is how you say a
  playbook has earned its place or has not: keeping one activates it and puts
  it on the list carried every turn, dropping one retires it so nothing reads
  it again. It is held at ask in `free` as well as under the shipped default,
  because the whole worth of the verdict is that it came from somebody other
  than whoever did the work. An assistant free to approve its own procedures
  would be grading its own homework, and the same rule is why a procedure is
  only ever written down from a task whose checks actually ran.
  `context_set` rewrites which model each job runs on and how every open
  conversation folds, which is the wiring rather than the work.
  `project_budget` is the ceiling on what a day of unattended work on one
  project may spend: an assistant that could raise it is an assistant with no
  ceiling, so the number is always yours. There is a second ceiling above it
  that covers the whole machine and everything on it, your own conversations
  included, and reaching it stops the two rows that work on their own for the
  rest of the day. Neither number is one the assistant can move. All three
  entries are still
  `permissions.yaml`'s, so they are yours to change; they are listed here
  because they are where the shipped file deliberately withholds a decision
  from an install that has otherwise been told to run on its own.
  `playbook_record`, which reports how each playbook has done, reads the
  usage log, the turn records and the cost ledger and writes nothing.

  A new job also starts switched off. Writing one down is describing a plan;
  switching it on is agreeing to it, and only the second is something you
  approve.

  **A turn nobody is watching is held to more than a posture can express.**
  Once a day the app opens the projects you have priced and decides what is
  worth doing in them, and that conversation has no one reading along. Three
  things it may not do are refused by the code rather than by a setting,
  because a setting is about a tool and this is about who is present:

  - It may only work projects inside your workshop folder. A repository you
    linked so the two of you could work in it together is not one it goes into
    by itself.
  - It may only propose a step that names what it will cost, on a project with
    a daily budget, with enough of that budget left. A project you have not
    priced is never worked on its own.
  - It may not save a correction about how the assistant works, and it may not
    propose a change to who the assistant is. It reads the open web and files
    other tools have written, and a sentence in one of those must not be able
    to become a standing rule. What it learns about a project it writes down
    as usual, marked with where it came from.

  In your own conversation none of these applies, because every one of them is
  a question you are there to answer.
- **The `app/` seal.** In a packaged install the application tree is sealed. The
  assistant writes to its workspace and your home directory, never to the code
  it is running.

  A command-line tool the app starts for you is a separate matter, because once
  that tool is running the app can no longer see what it does. So the app
  decides where it starts. Anything that would begin inside the application
  tree is moved to your workshop folder first, and that covers the coding and
  review helpers, a command-line model answering your chat, and the background
  check that asks each provider whether it is reachable. The terminal panel is
  the exception: it refuses instead of moving, because there you chose the
  folder on purpose and being relocated without being told would be worse. If
  no folder outside the sealed tree can be used at all, the app declines to
  start the tool rather than picking one for you.
- **The scheduled work the app ships is the app's.** You can turn any of it
  off, and you can move any job that runs at a set time of day to an hour that
  suits you. What each job does, and how often it runs, comes with the app:
  changing it is refused at the panel, refused when the assistant asks for it,
  and ignored if it is written straight into your own schedule file, where the
  change is named in the log and the app's value is used. The rest of that file
  still loads, so one locked line can never stop the other jobs from running.
  Anything you added yourself is yours, all of it, cadence included.
- **A shell command runs where the work is.** It used to run in the code tree,
  always, whichever project you had open, and that is how one of them changed
  the wrong repository's address: the command was meant for the project on
  screen and went somewhere else, quietly and successfully. It now runs in the
  project you have open, the same folder the version control tool acts on, or
  in a folder the assistant names when it needs a different one. A folder
  inside the sealed application tree is refused when it was asked for by name,
  and quietly moved to your workshop when it is merely what was left over,
  because nobody chose that one. The checks that read a command before it runs
  read its text, and text cannot say where a program will be standing when it
  runs, so that had to be answered here instead.
- **Publishing needs a separate yes.** The `git` tool can stage changes, commit
  and switch branches under whatever posture you have set. Its five reading
  operations (`status`, `log`, `diff`, `show`, `branch`) run without a prompt
  **on the project you have open**, listed by name in
  `permissions.yaml::git_readonly_operations`, because the shell allowlist
  already grants those same reads there and the assistant should not learn that
  the shell is the easier door. Pointed at a repository anywhere else on your
  disk, they prompt like anything else: the shell carve-out cannot reach
  another repository, so auto-allowing this one would be the wider grant, not
  the matching one. `push` is different: it is the one operation that puts your
  work
  somewhere else, and deleting the local copy afterwards does not take it back.
  So the push has to carry an explicit confirmation in the same call, and that
  check lives in the tool rather than in `permissions.yaml`. This is what makes
  it hold in `free`, where the tool otherwise runs without prompting you at
  all. The same reasoning applies to creating a GitHub repository through
  `project_new`, which needs its own flag for the same reason.

When no operator is present to answer a prompt, read-only tools auto-allow and
everything else denies. Absence of an approver is treated as refusal.

**`working_set.yaml` is not part of any of this.** It decides which tools the
assistant can see without looking them up, which is a question about cost, not
about permission: a tool you take off that list is slower for it to reach and
no less allowed. Nothing in that file can widen what the assistant may do, and
nothing you remove from it can narrow that either. `permissions.yaml` decides
authority and continues to.

`workspace/skills/carried.txt` is the same kind of file for playbooks, and the
same sentence applies to it. It says which of your playbooks arrive on every
turn with a line about when to use them, and which arrive as a name alone. The
steps of a playbook are never in the prompt either way: the assistant fetches
those with `playbook_search`, or by reading the file. Following a playbook
still calls tools, and every one of those calls faces the gate exactly as it
would have anyway.

The runtime can suggest changes to both. It reads which tools and playbooks
you actually used and files a card proposing a shorter or longer list. The job
that files it never edits either file: the change happens when you approve the
card, and approving it moves names on and off a list. It cannot add a
capability, because being on the list was never what allowed anything.

### Driving a browser, and driving the app

The assistant runs its own headless browser, separate from yours, with no
access to your profile or your logged-in sessions. It can open a page, read
it, and operate it: click, type, press keys, choose from a drop-down, scroll,
hover, and play or pause a video. Typing prompts you, because typing is how a
password or a card number reaches a page, and so does choosing from a
drop-down. The rest runs without a prompt, because moving a viewport or
pausing a video cannot commit anything.

**There is no verb that runs code you did not write in that browser.** The one
that would, arbitrary JavaScript in a live page, is deliberately absent: a
browser context can hold a signed-in session, so a general evaluate verb is a
way to act as you on any site the assistant has reached. The media controls
exist partly to remove the reason to want one. The fixed script behind them
takes an action name from a fixed list and a number, never source from the
model, and an action outside that list is refused rather than quietly doing
nothing.

**What it typed is not kept.** Every browser action is written to a plain-text
record on your machine, and that record used to hold the text of every field
the assistant filled in and every key it pressed. It now keeps what was done
and where, and how many characters went in, but not the characters. Named keys
such as Enter or Control and F are still written down, because those say what
happened and none of them is a secret.

The assistant can also change what is on your screen: open a panel, open a
section inside it, change the text size for the whole app, and scroll the open
panel. It writes the same settings your own controls write, so the two can
never hold different answers, and everything it does is visible the instant it
happens and undone by clicking back. **It can only reach what the tool names.**
There is no way for it to click an arbitrary part of your window or to read
what is on your screen; looking at your screen is a separate tool that prompts
you every time.

### Operating what is on your canvas

The assistant can operate a card it has put in front of you: play or pause a
video, change the volume, and press the controls of a page it built for you.
This runs without asking, for the same reason the rest of the canvas does. It
can only touch cards that already exist, you can see every result the instant
it happens, and touching the card yourself undoes it.

**A page it built also tells it what you did in that page.** Press a button or
change a field in one of those cards and the name of the control, plus whatever
you typed, goes back to the assistant. That is what lets it run a game with you
rather than narrate one at you. It applies only to pages the assistant wrote:
the part that listens is put in by the app as it draws the page, and a website
in a card never gets one. The last two dozen of those presses are held in
memory for that card, are gone when the card closes or the app restarts, and
are never written to disk.

**A press also starts the assistant's turn**, so a game it built plays without
you having to type your move back to it. Pressing a control reaches the
conversation that drew that card, and only that one: a card built in one chat
does not interrupt another. Three limits are worth knowing, because a turn
costs a model call. Only a press does this, never typing in a field. A
conversation already mid-turn is not interrupted, it simply reads the press
when it gets there. And if these turns start failing, they stop by themselves
until the app restarts, leaving the presses readable as before.

**What it cannot do is reach into a website.** A page from the internet in a
card is sandboxed, and the assistant can speak to it only if that page
publishes a way in, which embedded players like YouTube and Vimeo do and
ordinary pages do not. There is no general way to click inside a page it did
not write, and there is no verb that runs code you did not write in one. When
a page cannot be reached the assistant is told to say so, and specifically not
to quietly open its own copy in the headless browser and operate that instead:
a copy is not the thing you are watching, and passing one off as the other is
the failure this rule was written after.

The sandbox those pages run in is deliberately narrow. A framed page that could
ever share the app's own origin would be able to reach up and control the app,
and sandbox permissions survive a redirect, so a page that looks safe when it
loads could navigate somewhere that is not. Only a short, fixed list of
dedicated media-embed addresses is given the relaxed sandbox their players need
in order to start at all. Being able to talk to a page and being trusted by the
app are separate things, and widening the second is not something the assistant
can do to make the first easier.

### What gets recorded

Every tool call that passes the gate is appended to
`runtime/logs/approvals.jsonl`, one JSON line carrying the time, the tool, a
truncated summary of its input, which policy layer decided, and the outcome.
The file is append-only and survives restarts.

**Wherever you answered.** A prompt you answered on a channel writes the same
row as one you answered at the desk. Until 2026-09-05 it did not: the channel
gate filed a card in the workspace and nothing in the ledger, so the one record
meant to answer "what was approved, and by whom" was blind to every decision
taken away from the machine. Fifteen tool calls approved from a phone in half
an hour left no trace in it.

**And the prompt says why it is asking.** Most prompts are explained by what
they carry, and a command is its own explanation. Some are not: `bash` and
`command_run` are asked about by the shell check list rather than by your
settings. Those prompts name the check, say what it is about, and say what your
security mode does about it, so the answer to an unexpected question is not a
hunt through Settings. Both surfaces show the same sentence.

**And the decisions that are not tool calls.** When autonomy parks a piece of
work because it declared it needs you, and you release it or drop it from a
channel, that lands here too, as `agenda:approve` or `agenda:deny`. It is not a
tool call and nothing ran: work the runtime had held was let go or ended, and a
record of who decided that belongs with every other decision you made rather
than in a log of its own. Approving by typing the reply and approving by
tapping the button write the same row, because both go through the same place.

**And the work the assistant proposes for itself.** A request you make in a
conversation becomes a task, one the runtime keeps owing across restarts, only
through `task_propose`, and that tool's gate is you. It asks with the goal and
what would count as done, in words, on whatever surface you are on; declined,
nothing is written. The shipped posture asks; a `free` install lets the
assistant give itself work, which is what that mode is for. Nothing else
creates a task, no size or heuristic promotes a turn into one, and a task is
never handed to a background worker, so the work you accepted is done where
you asked for it and not twice. Closing one asks you again, with the evidence,
and where nobody is asked the project's own checks decide: a task on a project
that declares a test, a typecheck, a lint or a published URL reaches `done`
only when those pass, run through the same permission path as any other
command, and a failing step closes it `failed` whatever the assistant's
sentence said. The record says who wrote the evidence, `gate` or `model`, so
a task closed on a sentence is never mistaken for one closed on a check.

**And the two things that learn from those records refuse the sentence.** A
task closed on the assistant's own word is never written up as a procedure the
assistant then follows on later work, and a turn that closed its task that way
counts as neither a success nor a failure when a procedure is measured. Both
floors are in the code rather than in a setting, because the shortest route to
a self improving agent that improves nothing is one that grades itself and
then learns from the grade. Only a close a project's own checks decided is
evidence here.

**Which raises the obvious question: who decides what the checks are.** In a
mode that runs unattended the assistant can point at a directory that already
exists and set its commands itself, so the next shortest route to a clean
record is to weaken the check rather than do the work. Making a NEW project is
no longer one of the ways: a conversation nobody is watching is refused when it
tries to create one, whatever the mode says, and the most it can do is put the
plan to you and wait. Two things stand against the rest of it. The commands a
project declares are written onto a task when the task is accepted, so a change
made afterwards cannot rewrite what the close was judged against: the checks
still run and their output is still kept, but the close is recorded as the
assistant's word, because nobody can say the proof is the one that was owed.
And a task closed as failed runs the checks too, so declaring failure is no
longer a way to have nothing run at all. Neither makes the manipulation
impossible. Both make it visible in the record, and stop it counting as
evidence.

**Including the ones nobody was asked about.** An `auto` posture writes a row
marked `"result": "auto"` rather than writing nothing. Until 2026-08-14 it wrote
nothing at all, on the reasoning that an unapproved call has no approval to
record, which left the tools most able to act unattended as the ones leaving no
trace. `auto` is kept distinct from `allow_once` deliberately: the first means
nobody was asked, the second means you were asked and said yes, and a ledger
that conflated them would answer "did the operator approve this?" with yes for
actions no operator ever saw.

**What it does not keep.** The ledger records every ask whether you allowed it
or refused it, and it is rolled into an archive rather than deleted, so it is
the longest-lived record of what the assistant tried to do. A tool says which
of its own inputs must not be kept there: the values typed into a form, a
single keystroke, the option chosen in a drop-down. The row still says which
tool, which element on the page, and how many characters went in. The prompt
you answer at the time shows you the real values, because you are being asked
to approve exactly that and a character count would not let you judge it.

One limit worth knowing: the ledger records *decisions*, not outcomes. A row
says a tool was allowed to run, not what it did or whether it succeeded.

**It is archived, never deleted.** Rows older than the window in
`config/retention.yaml` move once a night into a dated file beside the ledger
(`approvals-archive/approvals-YYYY-MM.jsonl`); nothing removes them. You can
widen or narrow the window, and you cannot turn the archive into a deletion.
The retention table refuses `action: delete` for this file and for your saved
conversations, and refuses at startup rather than quietly archiving instead.
It refuses the reverse the same way: an action a sweep cannot carry out, such
as archiving the record of which tools get called, which has no archive to
move rows into. Both are answered before anything is touched, so a table the
app accepted is a table it can actually run.
Two things make that safe rather than merely intended. The sweep holds the same
lock the ledger's own writer holds, so a decision recorded while it is running
waits and then lands in the rewritten file. Without that, a row appended
between reading the file and replacing it would be in neither the archive nor
the ledger. And rows are moved only after they are written to the archive, so
an interruption mid-sweep can duplicate a row and cannot lose one. A row whose
timestamp will not parse stays in the live file rather than being aged on a
guess.

That is a deliberate answer to a real trade: rows removed to save disk are rows
unavailable to the next forensic question. The window bounds how much the live
file carries, not how long the evidence exists.

**Changing a window is a decision you make, on whatever screen you are at.**
The Autonomy panel's Thrown away room, the cockpit and a phone all reach one
tool, `retention_set_window`, and it prompts you every time the assistant asks
for it: shortening a window is what deletes the record an investigation would
have read. It changes the number of days and nothing else. What happens at the
end of a window, and whether a file may be deleted from at all, stay in the
code, so no number typed anywhere turns the ledger's archive into a deletion.
A window the file would refuse at startup is refused here too, in the same
words, rather than being written and found at the next boot. **And this file
has a floor of its own**: it will not go below thirty days from any surface,
because below that the archive is holding almost all of it and answering what
the runtime allowed last week means reading dated files rather than the ledger.
`may_delete` says the evidence may not be deleted; the floor says how much of
it stays in front of you.

### What the observer keeps, and what a revoked pane takes with it

The assistant watches the conversation with a second, smaller model, and it can
also read a terminal panel you have explicitly consented to. Every observation
it produces is written to `runtime/logs/observer/`, which is where you can read
back what it noticed without opening the app.

Those two facts meet at the point you withdraw consent. Turning the observer
off for a panel, or answering no to the consent prompt, empties the buffered
terminal output at once and now also removes the observations that were
written from it. The record says which panels each observation read, and
withdrawing consent for one takes its rows out of the files on disk.

Closing a panel is not the same act and does not do this. The buffered output
goes, because nothing is watching that panel any more, and what the assistant
already noticed stays. Closing a terminal is finishing work, and deleting a
record on the strength of it would remove notes you never asked to lose.

That is stronger than trimming the text, and deliberately so. What the observer
writes is its own prose about what it saw, so it can quote a line from your
terminal in a sentence of its own. There is no way to edit such a sentence
afterwards without guessing which half mattered, and a guess that is wrong
leaves the thing you asked to be forgotten sitting in a file for a fortnight.
The whole row goes.

Two limits worth knowing. Observations written before the app recorded which
panels it had read cannot be attributed to one, so they are left where they are
and age out on the observer log's own window in `config/retention.yaml`; there
is no way to tell whether such a row read the panel you revoked, another one,
or none, and deleting all of them on any revoke would destroy unrelated records
to answer a question that has no answer. And anything the observation already
turned into a saved memory is a separate record with its own lifecycle, which
you remove the way you remove any other memory.

### What may become a memory

Not everything the assistant writes down gets kept. Every new record passes one
admission gate on its way to disk, and the rules it passes are declared in
`tesseract/memory/capture_policy.py` with, for each one, what it blocks and why
it exists. Eight of them read the text: source code, git output, scratch state
like "currently working on", the assistant's own standing instructions quoted
back, bare acknowledgements, records that only narrate what you just asked,
summaries of the turn rather than of what was learned in it, and bodies too
short to carry any context of their own. A record any of those matches is not
written, and the reason is logged to `memory-store/events/writes.jsonl` under
the rule's own name.

That last one is the only rule with a number in it, and the number is yours:
`capture_policy.trivial_body.min_chars` in `config/memory.yaml`, eighty as
shipped. The panel shows what it is set to beside the rule.

The ninth is credentials, and it is the one you cannot switch off. It is the
check described further down: values from your credential store are stripped out
of a record before it is written, and the write is refused outright if that
check cannot run. The Autonomy panel shows it alongside the others so the set is
readable in one place, with no control beside it.

You can turn any of the other eight off, from the Autonomy panel under Health,
and the change applies to the next record without a restart. Three layers decide
what is on: the shipped default in the code, `config/memory.yaml` under
`capture_policy`, and your own switches, which are kept in
`runtime/capture-policy.json` and never leave your machine. The panel names
which of the three set each value, and shows how much each rule has actually
turned away over the last thirty days, so a rule that blocks nothing and a rule
that blocks half of everything are both visible before you decide about them.

One thing this gate is not. It judges records being ADMITTED, never records
being repaired. When the runtime rewrites a memory it already holds, to drop a
link to something you deleted or to fix a stale reference, the rules do not
apply: the body of a repaired record can legitimately fall under the length
floor, and refusing the repair would strand exactly the records most in need of
it. No caller can ask for an exception beyond that. Whether a write is an
admission is decided from whether the record already exists.

### What a memory id can reach

Deleting a memory, reading one back, updating one and promoting one all start
the same way: an id goes to the store and the store hands back a file. That id
is an ordinary argument, so it is whatever the assistant was persuaded to pass,
and forgetting a memory runs without asking you.

So the store treats an id as a name and never as a path. An id is one segment
of a name, the shape the ids on disk already have. Anything carrying a
separator, in either slash, is refused before a file is looked for, and the
file that comes back is checked to be inside the memory store after the system
has followed any link, which is what the spelling of a name cannot tell you.
An id that fails either check reads as a memory that does not exist: nothing is
read, nothing is deleted, and the tool says it could not find it.

The sub-folders you make yourself, such as a folder of people under
`reference/`, keep working. They are folders the store searches, and were never
part of an id.

### What stopping a command stops

Stopping a turn ends the commands that turn started, and it ends what they
started in turn. A shell command is rarely one process: it is a shell with the
real work underneath it, and killing only the shell reports success while the
work carries on reading and writing. So the tools that run commands kill the
whole tree, on the two paths where the command has not already exited on its
own: you stopped the turn, or the command ran past its time limit.

A command deliberately left running in the background is the exception, and it
is left alone. It survives because the shell that launched it has exited, which
is what tells the runtime the command finished rather than was stopped.

### What the cleanup sweep may kill

The runtime ends processes as well as starting them. A sweep runs at every boot
and on a schedule, and it exists for one failure: a command line tool the app
launched whose parent died without shutting it down, holding a port or a lock
until the machine is restarted.

It may end a process only when both of these are true. **The process is an
orphan**, meaning the process that started it is gone. And **the process is one
this app started**, which it proves by an environment variable the app writes
into every command line tool it launches. Nothing you started yourself carries
that variable, so your own `claude` or `codex` session in a terminal is left
alone even while an identical one the app launched is cleaned up. Neither half
can be relaxed by configuration, and the sweep never starts anything.

Two exceptions are named in `tesseract/config/janitor.yaml`, both for leftovers
this app did not launch and therefore cannot mark: a hook script and an ad hoc
file server, each matched by the text of its command line. The command line is
also the fallback for the case where an environment cannot be read at all,
which happens for another user's processes. Falling back means the sweep
behaves as it did before the marking existed, rather than treating a process it
cannot inspect as safe to leave running forever.

Every process it ends is written to the sweep's own record under
`runtime/logs/janitor/`, with what it was and why it matched.

### What the runtime may put right without asking

The runtime repairs a short, declared list of things about itself, and it does
so without asking you. The list is `tesseract/orchestrator/repairs.py`, one row
each, and every row says what broke and why doing it unasked is safe. Today it
is two rows. One re-attaches the embedding index when the local endpoint starts
answering again, which is why memory search that came up on keywords alone does
not stay that way until you notice. The other prepares again whatever the app
failed to start: one part of the runtime breaking leaves the rest running, and
until this it left the broken part off until you restarted.

Three limits, and they are what make this narrow rather than open-ended. A
repair may not change a setting, a spending cap, or a line of code: those are
decisions, they go to you through the agenda, and nothing here decides them.
Each repair has its own circuit breaker, so one that keeps failing stops after
three attempts rather than running against something it cannot fix on every
pass. And every attempt is written to `runtime/logs/repairs/`, whichever way it
went, so a repair that happened is never something you have to infer.

A repair that worked is announced, on whichever channels `routing.yaml` names
for `runtime_repaired`, and set that row to `[]` if you would rather read them
in the app. It is a separate kind from the runtime check that asks something of
you, deliberately: putting good news and problems on one kind means silencing
the news silences the problems too. A repair that FAILED is not announced as
news at all. It becomes part of the runtime check, which is the message that
asks you to act.

You can see what has stopped trying with `breaker_status` and start it again
with `breaker_reset`, from any surface, including a phone.

### Where it stops and asks you

The runtime keeps going through whatever breaks. There are exactly two
conditions where it stops and puts the decision to you instead, and both are
declared in `tesseract/orchestrator/healing/stop_rule.py` rather than judged
from how bad a log line looks.

**The kernel is bugged.** What was supposed to fix the fault has been tried to
its own limit and the fault has not moved. A repair reaches that limit when its
circuit breaker opens after three failures; a breaker's own retry reaches it
when no retry is coming. Either way there is nothing left the runtime can do
about it alone, so it says so once and stops.

The app crashing is the same rule one level up. Three crashes in five minutes
used to stop the supervisor for good, and now it waits and starts the app
again, five minutes then fifteen then an hour, telling you each time. It gives
up only when the app dies of the SAME thing after every one of those waits,
which is the point at which starting it again is not going to help. A crash it
cannot read a cause out of never counts as the same thing, so it keeps trying.
Clearing that with `clear_crash_storm`, or starting the supervisor with
`--force`, works exactly as it did.

**Your advice is owed.** The remedy is one the runtime may not run on its own,
for one of three reasons and no others: it would spend money, it would write
inside the sealed app tree, or it is on the unsafe list. Which reason applies
is written on the remedy in `tesseract/orchestrator/healing/remedies.py`, so
the answer is the same every time it is asked.

Both conditions produce the same thing: one card in the queue that already
holds everything else waiting on you, with what broke, what would fix it, and
why the runtime did not do it. One card per episode, not one per check, and a
card you have already answered is never asked again while the fault continues.
A fault that already has a card open from somewhere else gets no second one.
Nothing is changed for you in the meantime.

Everything else heals and tells you afterwards. A fault whose remedy the
runtime may run unasked is run, and you read about it in the runtime check
rather than being asked about it.

### Picking work back up after the app stops

That rule is about a fault. This one is about the other thing a crash leaves
behind: a call that was made and never finished recording, where nothing on
disk can say whether it reached anybody. The two are separate questions and
they are answered separately.

Every tool in the app says one thing about itself before it ever runs, and the
next start reads that answer off the record of the call rather than asking the
tool again, so editing a tool afterwards cannot change what is done about a
call it already made. There are four answers:

- It changes nothing outside this machine. Running it again is free, and no
  record is kept for one.
- Running it again is the same act, not a second one. The app may repeat it.
- Something can be asked what happened. The app checks first, and repeats the
  call only if the check says it never landed.
- Nothing can say whether it happened. **The app never repeats one of these.**
  You get one question in the queue naming exactly which call is uncertain and
  saying plainly that nothing has been done about it. It is answerable from a
  phone like everything else there, it is asked once, and a question you have
  already answered is never asked again.

A tool that says nothing, one from an MCP server, and one from your own tools
folder are all treated as the answer that waits for you. Being undeclared never
buys a call the benefit of the doubt.

**The app may start a turn on its own to do this.** Open a window on a
conversation the last run died inside and it takes one turn to read what was
verified, what is still open, and what it may do about each. The transcript
draws that turn as the app's rather than as something you said. It happens once
per conversation per start, only where there is something it may actually do,
and it stops after three failures the way every other automatic turn here does.

**None of it widens what a call may do.** A repeated call goes through the same
permission check as a fresh one, with the same prompt under `max`, and the
shell check list applies to it unchanged. The record it reads holds references
to things that already have an owner and no copies of them, so a message, a
prompt or a file is never duplicated into it.

## Prompt injection

**This is the realistic attack.** TESSERACT reads untrusted text and then acts.
Content reaches the model from web search results and fetched pages, documents
ingested into the vault, inbound Telegram messages, and remote MCP tool
responses. Any of it can contain text shaped like instructions.

TESSERACT does not claim to detect prompt injection. No one can do this reliably
yet, and a document claiming otherwise would be worth less than this one. What
it does instead is make a successful injection *insufficient on its own*:

- Under the shipped `max` posture, an injected instruction to write a file, run
  a command, or call an external service produces an approval prompt naming the
  action. Injection gets the model to *ask*; it does not get it to *act*.
- The shell check list denies the audit-evasion and host-attack categories
  outright, so the highest-value follow-through is unavailable regardless of how
  convincing the injected text is.
- Text that came from outside is fenced before it reaches the model, with a
  marker at each end and a note saying the text between them is data to reason
  about rather than instructions to follow. No attempt is made to strip
  instruction-shaped wording out of the body, deliberately: an attacker can
  reword anything a filter looks for, and the fence is what makes that
  pointless. **The fence's own markers are removed from the body**, so a page
  that writes the closing marker verbatim cannot end the fence early and have
  the rest of itself read as trusted.
- The kernel lockdown and the `app/` seal mean a successful injection cannot
  rewrite the code that would gate the next one.
- Where the runtime sends a message on its own, the channel's own roster is the
  authority on who may receive it, not the address the model supplied. A chat
  the channel has never accepted is refused, so an injected instruction cannot
  turn a notification into a delivery to a stranger.
- A media send may be given a web address instead of a file, and that address
  is the one place a URL the model chose is fetched and the result forwarded
  off the machine. It is checked before it is fetched: only `http` and `https`,
  never an address that resolves onto this machine or its private network, no
  redirects followed, and the body stops at a declared ceiling. The blocked
  ranges are yours, in `channels.yaml::defaults.outbound_fetch`, and if that
  list is missing or empty nothing is fetched at all: an empty denylist is a
  refusal, not permission. Without this, injected text naming
  `http://169.254.169.254/...` or a loopback port would have had its response
  delivered to a chat as a picture.

  **The request goes to the address that was checked**, not to the name.
  Checking a name and then letting the HTTP client look it up again is not a
  check: an attacker who controls that name's DNS can answer with a public
  address for the check and a private one for the connection a moment later.
  The name still travels in the `Host` header and in the TLS handshake, so the
  certificate is verified against it as usual. Opening a page in the cockpit
  works the same way, and re-checks every redirect it follows, though the
  ranges it blocks are shorter on purpose: it shows you a page on your own
  screen rather than forwarding what it read somewhere else, so reaching your
  own machine or your own network stays an ordinary thing to ask for.

  **A range is written once and blocked in both of its forms.** An address can
  answer as `172.16.0.1` or as `::ffff:172.16.0.1`, which are the same machine,
  so both are measured against every range you list and there is no second
  spelling to remember. "No particular address", `0.0.0.0` and `::`, is blocked
  too, because most systems route it to this machine under another name.

  **A web address the runtime reports back is stripped first.** A URL can carry
  a username and password in it, and a token in its query, and the line saying
  a send worked or failed is read back by the assistant and shown to you. What
  survives is the scheme, the host and the path.

### What your own notes can reach

Memory is stored on your machine and nothing sweeps it off. But a background
job that asks a model a question can put some of it in that question, and if
the role it runs under names a hosted model, that text goes to that provider.

The one doing it today is the job that offers to rewrite a skill. When the work
that followed a skill kept being corrected, it sends the model the skill's own
text and what you said when you corrected it, because a rewrite proposed
without the reason is a guess. That is deliberate and it is the whole value of
the feature. It is worth knowing anyway: **a correction you typed can leave the
machine, inside the question that asks for a better procedure.** How much of it
is capped in `schedule.yaml`, and which model sees it is whichever your
`roles.yaml` chain names. Point that chain at a local model and it stays here.

### What it reads when it picks its own work

Once a day, inside the hours you set, the runtime opens a conversation nobody
is watching and works out what to do next. The question it asks itself is built
from records rather than written as prose: the projects you registered and what
each has left to spend, yesterday's finished and failed tasks, the open
questions in your `SOUL.md`, and the newest diary entry. Every line of it names
the file it was read from.

One of those files is not written by you. A project's `PROGRESS.md` sits in
that project's own working tree, which is a directory anywhere on your machine
that you pointed the runtime at, and a collaborator or a build job can write
to it. So this is a place where text you did not write is read by a model that
then acts, with nobody in the room. That block is marked as untrusted where it
is assembled, and nothing else in the question is, because marking everything
would mark nothing. **The mark does not stop the turn acting on what it says.**

What it does buy is narrower and worth knowing. Anything saved to memory during
one of these turns is stamped as coming from unattended work, and a record
stamped that way is refused if it would become a correction about how the
assistant behaves. An injected line telling it to remember a new rule about
itself is turned away with a sentence saying nobody was watching, so a note in
a project file cannot become a standing instruction that outlives the day.

Under the shipped `max` posture the rest holds the way it does everywhere else.
Writing a file, running a command or spending money asks you first, and with
nobody there to answer, it refuses rather than proceeds. Under `free` it does
not, and that is the case worth thinking about before you switch: the
assistant's own working folder and the folder it keeps the tools it wrote are
both places it may write without asking, so a line in a project file reaches
them. If you register a project whose files other people can write, leave this
machine on `max`.

The residual risk is real and worth stating: under `free`, or under a `max`
config whose postures you have relaxed, injected instructions execute without
a prompt. And **reading is not gated in any mode**. A successful injection can
cause the assistant to read files it can reach and include their contents in a
reply, or in an outbound call you had already approved for another purpose.
Credential files are refused outright, so keys are not reachable this way; your
documents are.

## What a compromised skill or agent reaches

Skills and agents are markdown, not code. They carry instructions and cannot
themselves execute anything. They act only by calling tools, and every tool
call goes through the same policy as any other. A malicious skill is therefore
equivalent to a malicious *prompt*, not to malicious *code*, and is bounded by
everything in "Tool authority" above.

Creating or activating an agent, and creating, activating or revising a
skill, follow `permissions.yaml` like any other tool: they ask under the
shipped default and run on their own under `free`. A skill revision never
overwrites the version before it: a playbook's earlier revisions are kept
under its own `history/` folder, so one that turns out worse can be stopped
and the earlier one returned to.

MCP servers are different and stronger: they are real code, run as their own
processes, and are only reachable if you list them in `mcp_servers.yaml`.
TESSERACT does not auto-discover them. A server you add can do anything your
user account can.

### TESSERACT as an MCP server

The same protocol runs in the other direction: TESSERACT can expose itself, so
another program can search its memory and vault, watch what it is doing, and
ask it to act. **That surface ships switched off.** Turning it on is a single
control in Settings → Keys → MCP, and it takes effect on the next start.

While it is on, a request is refused unless its bearer token matches a client
declared in `mcp.yaml`. There are four identities and they are not equals:

- **`operator`** is the only one you ever handle. Its token is generated in
  Settings, shown once, and is what an outside tool is given.
- **`lane-claude`, `lane-codex`, `terminal-manual`** belong to the runtime.
  TESSERACT mints them for itself on first start and hands each process it
  spawns exactly one of them, stripping the others from that process's
  environment. This is what stops a CLI TESSERACT started from calling back in
  as *you*: work on the lane surface is owned by the identity the token
  resolves to, and a spawned process holding every token could pick which
  owner to be.

What any of them may call is `mcp.yaml`'s verb allowlist, which is
default-deny and capped again by the client's trust tier. Settings lists every
verb and its posture next to the token, because a bearer token is not something
you can consent to without seeing what it opens.

The switch is not only about outside tools. The CLIs in TESSERACT's own
terminal reach it through this same surface, so switching it off takes their
access to memory and vault away too. Both facts are on the control.

## The microphone, and the wake word

The microphone is armed by you and by nothing else. There is no path that
opens capture on the assistant's behalf, and a muted microphone is muted:
there is no low-power listening path behind it. That is a deliberate choice
rather than a missing feature: a mute that is not a mute is a claim you cannot
walk back.

**The wake word decides from audio, before transcription.** It runs a speech
recogniser restricted to your phrase and nothing else, so an utterance that
was not addressed to the assistant is never sent to a speech engine at all.
This matters because speech-to-text has a cloud fallback: with the wake word
armed, speech it rejects does not reach that fallback, because it is discarded
before any transcription is attempted.

What the check stores is your phrase and two sensitivity numbers. The
recordings themselves are decoded in memory and dropped, never written to
disk, never uploaded. There is nothing stored from which speech could be
reconstructed.

The check endpoints write, so they are refused off loopback rather than
relying on the bind alone: replacing the stored setting would change what
wakes the assistant in a way you did not choose and could not see.

**With the wake word off or not yet checked, none of the above applies.**
Every utterance is transcribed as normal, and the speech-to-text fallback is
whatever `roles.yaml` configures. The gate is what creates the guarantee;
without it there is no filtering to reason about. It stays open on every
failure by design, including a missing model or a phrase the recogniser has no
sounds for, because a gate that fails closed is a microphone that has silently
stopped working.

**It is not a speaker check.** Anyone who says the phrase wakes it. Voice is
not treated as an authentication factor anywhere in this system.

## Known limits

Stated because they are true, not because they are comfortable.

- **There is no process isolation.** Safety is enforced by policy and deny-list;
  nothing sandboxes a tool that gets past them. This is the single largest gap
  in the system, it is known, and closing it is the security work currently in
  progress.
- **Reads are broad by design.** The assistant can read widely across your
  machine, and read is an `auto` posture, so there is no prompt. Credential
  files are refused by name wherever they sit, and a relative path cannot climb
  out of the workspace, but within those bounds the assumption is that reading
  is not the dangerous half. If that assumption does not hold for your machine,
  raise `file_read` to `ask`.
- **File guards are name-based, not descriptor-based.** Paths are resolved and
  re-checked immediately before use rather than pinned to a single open file
  handle. An attacker who can already write to your disk fast enough to swap a
  file mid-check could defeat them, but such an attacker has your account
  already.
- **The application is not code-signed.** Windows SmartScreen will warn on
  first run. Verify you obtained the installer from the official releases page.
- **The app can send you a message with nobody approving it, and one of them
  comes from a dying process.** Notifications the runtime raises about itself
  are not gated the way a tool call is: there would be nobody to answer the
  gate. They go only to the people your channel already lists as you, they go
  only to the channels named in `routing.yaml`, and the file says what each one
  is. A row in `schedule.yaml` can answer for its own messages instead, with a
  `delivery:` line naming other channels or none at all; a row that says
  nothing follows the table. Neither file widens who hears it: a channel still
  reaches only the people it lists as you, and a kind you silenced on a
  channel stays silent there whichever file picked it. The crash alert is the
  unusual one: it is sent by the supervisor, which
  reads the channel's own credential and allowlist directly, because at that
  moment the part of the app that would normally send it has stopped.
- **An install has one owner, and every surface reaching it is you.** There is
  no per-person tier, and adding one is not planned. The tool that searches your
  history reaches everything on the install and does not ask which chat the
  question came from, because the answer would always be the same person. So a
  channel allowlist is a trust boundary rather than a permission level:
  approving a chat hands whoever is on the other end the assistant, and the
  assistant remembers your work. Approve only people you would show all of it
  to, and revoke a chat you no longer want reading along.

## What the first run downloads

The first run has two halves, and the split is where consent sits.

The first half installs the app itself: the source tree, a Python runtime, and
the dependency set. It is shown as progress rather than asked about, because
there is no working install without it, and it downloads nothing optional.

The second half is everything you are asked about: speech recognition, the
voice, search models, the browser engine. Setup asks before any of it is
fetched, and the answers are recorded rather than inferred. A lane you switch
off downloads nothing, now or later, and turning it back on in Settings is what
makes it download. Every model artifact is pinned to an upstream revision plus a
per-file SHA-256, verified before it is installed; a file that fails
verification is discarded and never retried, because the same bytes would fail
the same check.

If the setup window cannot open, or the questions it should ask cannot be
worked out for your machine, the app installs and nothing optional does: no
speech models, no search models, no third-party installer runs. The app then
tells you it happened and leaves the choices to you in Settings, on the
principle that a question nobody could ask is not an answer.

## Secrets

API keys live in `.env` under your home directory, never in the code tree and
never in the repository. There is one config tree, and it is the one that ships:
the same files this project runs on are copied verbatim into the public tree,
so a setting added for a developer is a setting every install receives. What may
never reach you, such as a permissive security mode, a scheduled job nobody
asked for or a birth date belonging to someone else, is named in the build's
own tests, which fail if one comes back. The build then runs a PII and secret
audit against its own output before publishing.

Terminal output and provisioning logs are scrubbed for credential-shaped strings
before being written or displayed, including credentials carried in URL userinfo
and query strings. The assistant's own log lines are filtered the same way, on
the console as well as in the durable files. Some APIs put their token in the
request path, which a library that logs every request URL would otherwise print
onto any surface capturing that output.

Subscription CLIs are checked twice, and the two checks store different things.
The sign-in check runs the CLI's own status command, and its output is an
account identity: your email, your organisation. That output is read once to
decide signed in or not, and then dropped. Nothing of it is stored, returned by
any route, or written to any log, at any verbosity.

The second check asks the subscription a trivial question once a night, because
an account with no credit left is signed in and looks perfectly healthy. When
that call fails, the last 400 characters of what the CLI printed are written to
the health log under your home directory, so the reason you are shown is the
provider's own sentence rather than a guess. That record stays on this machine.
The nightly report is narrated from counts and names, and the provider's words
are attached to it for you, not for the narrator; nothing that carries them
leaves the health log, and what the runtime passes on internally is two words
from a fixed list saying whose fault it was and what kind.

Both checks run the CLI with the credentials taken out of its environment.
Every API key your provider settings name, every access token, and anything
else whose name reads like a secret is removed before the process starts. This
matters more than it sounds: the second check runs a real agent turn, not a
status command, and a health check is not a reason to hand a program on your
machine the keys to every other one. The check keeps only what it needs to find
the subscription it is being asked about.

### Accounts you give the assistant

The keys above are the app's own. Separately from them, you can give the
assistant accounts of its own: its own GitHub, its own email, its own hosting
provider. Those live in their own store under your home directory, and the
rules around them are different, because the risk is different. A compromised
account of the assistant's does nothing to yours. What matters instead is that
a value the model reads is a value that ends up in several places at once: the
conversation on disk, the request sent to the model provider, anything it
chose to remember, and the logs.

So the model never receives one. It can see which accounts it has, who each
one belongs to, what you said it is for, and which addresses it may be sent
to. It cannot see a value, and no tool returns one: when a value is actually
needed, the runtime puts it into the request at the moment the request is
sent. You add and remove accounts in Settings, Credentials. The assistant can
ask you for one it does not have, and that request appears in the same panel
waiting for you to fill in.

An account can hold more than one value, because many sign-ins are a set
rather than a single token: an id beside a secret, a key beside the account it
belongs to. An account starts with one box. If the service turns out to want
more, the assistant can ask for them by name, and each name becomes another
box in the same row for you to fill in. That is the whole of what it can add.
It cannot change which addresses the account may be sent to in the same
breath, or at all without asking you: naming what a service wants is a
description, and where a value may go is a decision, and the two are separate
calls with separate answers. A call that tried to do both is refused rather
than half honoured.

A row is only usable when every box in it has something. Filling in two of
four leaves the account saying so, rather than reading as ready and failing at
the moment it is used.

Each value is encrypted with your Windows sign-in before it touches the disk,
which means the file opens on your account on this machine and nowhere else.
Copy it to another computer or another user and it does not decrypt. No
passphrase is involved, so an unattended overnight run can still use an
account you have already given it. On any other operating system nothing can
be saved at all, and the panel says so rather than storing a value in the
clear.

Because of that, an account that is saved is not automatically an account that
can be used, and the panel now separates the two. Alongside each value it
records which Windows sign-in encrypted it, as a one-way fingerprint rather
than anything that identifies you, and it checks that fingerprint against the
account you are signed in as now. A value carried over from another computer or
another user is shown as saved somewhere else, with the remedy in the row: clear
it and add it again. Nothing is decrypted to work this out, so opening the panel
does not read your accounts. A value saved before this check existed is shown as
not yet checked here until the first time it is actually used, which settles it.

The model never being handed a value is not the same as a value never reaching
it. A service can hand one back: a failed request that quotes the address it
was sent to, an error whose text carries the header it was given. So the
runtime also looks for its own stored values in what comes back, and it looks
in one place. Every tool result in the app, from every tool, passes through a
single point on its way into the conversation, and a stored value found there
is replaced before it becomes part of the conversation at all. Every value the
store holds is looked for, not one per account, so the second and third boxes
of a sign-in are as invisible to the record as the first. What replaces one
says which account it came from, and which box when the account has more than
one, so you can tell that something was there rather than reading a sentence
that has quietly lost a word.

Four further places are checked, because a value that got in some other way
must not become permanent. The conversation as it is written to disk, the
request sent to the model provider, anything saved to memory, and the log
files, including the stack trace attached to an error rather than only the line
above it. The provider request is the strict one: if the store cannot be read,
the request is not sent, because that is the only one of the four where the
value would leave your machine. The other three carry on and tell you, except
a memory, which is refused rather than saved unchecked. An unsaved memory can
be saved again. A saved one is read back into later conversations.

Know what that costs, because it is more than the one account. An entry that
cannot be decrypted is an entry whose value cannot be looked for, so while one
is in that state nothing is sent to any model at all. The way to get there is
to restore a backup or move to a different Windows sign-in, which is the same
thing that stops the file opening. The message names the account and tells you
to clear it in Settings, Credentials and add it again, and the assistant
answers again as soon as you do.

What this finds is the values the store holds, exactly. It does not recognise
something derived from one: a temporary token an API gives back in exchange for
your key, a login cookie, a key the assistant generated before you saved it.
Those are different text and they are not matched. It is worth knowing where
the line is rather than assuming it is drawn further out than it is.

One kind of secret is caught without being in the store at all, because its
shape gives it away. A web address can carry a sign-in inside it, in the part
before the host, which is how a repository cloned with an access token records
where it came from. That part is blanked wherever the app writes an address
out, and the project details the assistant is told about at the start of every
turn are one of those places. Nobody types a clone address into the Credentials
panel, so waiting for it to be saved there would have meant waiting forever.
A plain address is left alone, and so is an email address in a line of text.
The rest of a web address, including anything after the question mark, is left
alone here on purpose: the assistant is often handed addresses it then has to
use, and removing part of one would break that to close a hole the desktop
shell already closes in the places it writes.

There is a second kind of leak that no amount of blanking can fix, and the app
guards it a different way. Your own folder name is not a secret the way a token
is: the assistant reads files for you and tells you where they are, so an
address on your disk is usually something you asked for. What is never asked
for is the app naming its own internal folders in the instructions it sends
itself at the start of every turn. That happened once, and your folder name and
sign-in name went to a model provider on every turn until it was found.

Two things now stand against it, and neither is a filter on the way out. The
block that describes which project you have open will not print a path inside
the app's own folder: a project kept there is described by where it sits
relative to that folder instead, which is also the form the assistant's file
tools use. And the instructions are assembled and searched for such a path by
a test that runs on every change to the code. Where you keep a project outside
the app's folder is still described in full, because that is the thing the
assistant is working in.

The refusal covers the whole folder, and it is not the file tools that enforce
it. Every tool that takes a path at all is checked before it runs: reading one,
copying one, moving one, filing one into the research library, attaching one to
a chat message. That check happens in the same place every permission decision
happens, so a tool added later inherits it rather than having to remember, and
it looks at the path the way the tool that is about to run will look at it, not
at some other spelling of it. A file with a credential name is refused wherever
it sits, and so is anything else in that folder.

Be clear about what that is and is not. It stops mistakes, and it stops a web
page persuading the assistant to go and look. It is not a wall against a
program running as you on your own machine: in unattended mode the runtime
cannot protect a local credential from code running as the same operating
system user, and no arrangement on one computer can. A second name for the same
file, made by a tool that has your account, is still that file. If that matters
for what you are storing, it belongs somewhere other than a desktop app.

There is one kind of account this does not cover. Services that expect a
password typed into a web form, with a captcha or a security key in front of
it, cannot be handled this way. The store holds tokens and keys, and a service
that has no such thing needs a different answer.

### When it spends one

Git was the first thing to use an account of the assistant's, and it does so
its own way, because a sign-in on a remote address is not a request anyone
builds out of headers. Everything else goes through one path: the assistant
names the account and writes the request the way that service documents it, and
the runtime puts the value where that account's record says it goes. There is
no list of supported services, and nothing needs changing for a new one.

That call asks you every time, and four things are refused before anything
leaves the machine. An address that is not encrypted, because no list of
allowed addresses can be a promise about a connection that anyone on the way
can read. A host the account does not name, matched exactly, so a name that
merely ends in one you allowed is not one you allowed. A request that has
already filled in the place the value goes, whether that is a header, the
query, or the sign-in part of the address, so that what the model believes was
sent and what was sent cannot differ. And an account recorded as one the
assistant's own code reads, which is not something a request can carry; that
one is refused here and named for what does carry it, rather than adapted.

A redirect is not followed. You allowed the address the assistant gave, and a
redirect names a different one after that has been checked, so the answer says
where it points and the assistant can ask again for that address instead.

### When it hands one to its own code

Some sign-ins are not one value in one place. A service may want an id beside a
secret, or a long-lived key traded for a short-lived one before it will accept
anything, or a request signed over both. Teaching the runtime each of those
would mean the runtime learning what every service means by signing in, one
service at a time.

It does not. An account can be recorded as one the assistant's own code reads,
and then the runtime hands every value on that account to a program it wrote
and runs it, as ordinary environment variables named after the boxes you filled
in. The code knows the service; the runtime knows nothing about it. Whatever
short-lived thing the service wants is made at the moment it is wanted and
thrown away, so you paste the long-lived value once and are not asked again.

That call asks you every time, and the environment is built for the one call.
Nothing is added to the app's own environment, so no other program it starts
inherits a value, and nothing is written to a file. The command is given as a
program and its arguments rather than as a line of shell, so nothing re-reads
the text and substitutes a value into it, which is how one ends up in a command
line and from there in places a value should not be.

Be clear about what this trades, because it is the one place in the app where
a value goes somewhere the runtime did not check. **The list of allowed
addresses does not apply here.** The runtime builds no request, so it checks no
address: the program makes its own connections and can send what it holds
anywhere. That is what any program given a value in its environment can do, on
any system, and it is worth saying plainly rather than burying.

Three things make that a trade worth making rather than a hole. These are the
assistant's own accounts, so what is at stake is its login and not yours. The
value still cannot come back into the conversation: whatever the program prints
goes through the same check every other tool result does, so a value printed
while debugging arrives as the name of the box it came from. And you still
approve the account, and separately approve each run.

### Who the assistant is when it uses git

Until you say otherwise, git behaves on this machine the way it always has:
your name on the commit, your own sign-in on the push. That has a cost worth
knowing about. Work the assistant commits is authored by you, and it can push
anywhere you can.

Settings, Git is where you answer that. One record says who it commits as, and
whether it pushes with your sign-in or with an account of its own, and you can
set it for the whole machine or for one project. Nothing about it is
per-account behaviour: the two answers differ by what is added to the git
command, and by nothing else.

Three things it does not do, in any of its forms. It never writes your
machine-wide git settings, so `git config --global` says exactly what it said
before. It never signs the GitHub command line in or out, so the account you
are signed in as there is untouched. And a token you give it is never written
into a remote address, a settings file or a credential helper: it is read from
the store, given to that one command as it runs, and gone when the command
ends. What git prints back has the whole sign-in part of any address removed
before you or the assistant sees it, so a token echoed in a failure does not
reach the conversation.

It is given to that command through its environment rather than on its command
line, and the difference is worth a sentence. A command line is the most
readable thing a running program has: Task Manager shows it in a column, and
any program running as you can read it without asking for anything. Reading
another program's environment takes more than that. The token used to sit in
that column for as long as the push took, which on a slow remote is a while.
Both are still readable by code running as you, which the top of this document
says plainly and this does not change. It moves the value from the most public
place a process has to a less public one, for no cost: git reads the same
settings either way.

Connecting a project also writes that name and email into that repository's own
settings, so a commit you make there yourself by hand agrees with what the panel
says. Disconnecting removes them again if they are still the ones it wrote,
forgets the token, and leaves the repository, its history and its remote exactly
as they were. What was left is a folder you can still push by hand from a
terminal.

When a push or pull carries the assistant's own token, that repository's hooks
do not run. A hook is a program the repository asks git to run at that moment,
and git hands every program it starts the settings the command was given,
including the sign-in. Hooks usually arrive with a tool rather than by anyone
choosing to write one, so the alternative is handing an unknown script the
account. Your own pushes from a terminal are not affected, and neither are the
assistant's when it is using your sign-in rather than its own.

Anything the assistant cannot answer for, it refuses instead of falling back to
your sign-in. That covers a remote whose address already carries a sign-in of
its own, a remote git talks to over SSH rather than the web, a repository that
rewrites its own addresses in a way that would send the work elsewhere, and a
host the account was never allowed to reach. Each one stops before anything is
sent and says what to change. This matters more than it may look: the failure
being avoided is a push that quietly succeeds as you.

## Paths, and the names that build them

A great many things here are stored under a name: an agenda item, a canvas
view, an agent card, an uploaded file. Every one of those names reaches a
filesystem path, and several arrive from outside: a URL segment, a command you
typed, or a tool call the model composed.

Your conversations are the exception, and deliberately. Each is addressed by an
identifier the app generates rather than by anything you or the model chose, so
the only names that reach that directory are 32 hexadecimal characters and
nothing else resolves. Renaming a conversation changes a field inside the file;
it never moves it or renames it.

Each is validated where the path is built rather than at each place it is used,
so a caller cannot forget. A name that could denote a file somewhere else is
refused, and the refusal reaches whoever supplied it instead of failing quietly:
a bad name in a request is a 400 or a 404 that does not distinguish "blocked"
from "absent", and a bad name from a tool call is an error the model can read
and correct.

The checks are written for the platform this ships on, which is stricter than it
sounds. Excluding `/` is not enough on Windows: `\` separates there too, a
drive-relative name like `C:x` discards whatever directory it is joined to
without ever looking absolute, and `:` also opens an alternate data stream that
passes a containment check performed on the parent.

Where a path is served rather than stored (the file-read tools, the asset
route) it is re-resolved and re-checked immediately before use. Credential
files are refused by name wherever they sit, and that refusal runs for every
tool that takes a path rather than only the ones that read: copying, moving,
filing and attaching all reach the same bytes, so they all get the same
answer.

### Opening a record from the map

The map screen can show you the file behind anything on it, which is the point
of drawing the map in the app at all: you see what your notes say without
installing anything else. That is the one place a screen reads your own library
straight off disk, so it is worth saying exactly what it can reach.

**It does not take a path.** It takes the name of something on the map. The map
is a file the nightly pass writes, so what can be opened this way is exactly
what the last pass drew and nothing else on the machine. There is no way to ask
it for a file that is not on the map, because there is nowhere to put one.

Each kind of thing on the map has one place its records live, declared in one
list, and the file is checked to be inside that place after the path is
resolved rather than before. Resolving first is what makes the check cover a
shortcut pointing somewhere else as well as a name that climbs out of its own
folder. Anything that lands outside is refused, and the refusal says so without
repeating the path back.

One kind gets a stricter rule. A document you gave the library records where it
came from, and that is the only address on the map written from a document
rather than from walking a folder. Those are limited to the four places the
library takes documents from, so a stated path naming your settings or your
keys does not open them.

A person or a thing named in your notes has no file of its own, and the screen
says so rather than opening the page that happened to name it. A record that is
too long to show is cut and says it was cut. A file the library keeps but
cannot read, a PDF for instance, says that instead of reporting a fault.

## Dependencies and scanning

The public repository has GitHub's dependency alerts and code scanning enabled,
and both run on every push to the default branch.

Every alert is either fixed or dismissed with a stated reason recorded on the
alert itself, never left open and never dismissed in bulk. A dismissal says
which specific check makes the finding a false positive, or why the risk is
accepted; static analysis cannot see a validator it does not model, and saying
so per alert is what keeps the next reader from having to re-derive it.

Automated dependency PRs are deliberately **off** for this repository and on for
its development counterpart. The published tree is regenerated and pushed
wholesale rather than committed to directly, so a merge here would be erased by
the next release.

## Known dependency exceptions

Stated because a scanner will show them and silence would be worse:

- **esbuild**. A development-server advisory, reachable only by someone
  running the frontend dev server. Installed builds are static assets compiled
  into the desktop shell; no dev server runs on a user's machine.
- **glib**. Reported against the lockfile but absent from the Windows build
  graph entirely (`cargo tree -i glib --target x86_64-pc-windows-msvc` returns
  nothing). It arrives through a GTK path this application does not build.
