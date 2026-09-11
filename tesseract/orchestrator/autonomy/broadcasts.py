"""Everything the runtime says without being asked, declared.

One row per kind: what it is, why it exists, what emits it, and whether the
operator must see it. Same contract the run manifest uses, for the same
reason: someone deciding where a message goes has to be able to read what it
is first, and a surface may not claim what no producer emits.

**A kind nobody emits fails boot.** `check_producers()` runs at startup and
raises, naming the kind and its declared producer. That is not tidiness: nine
kinds were declared here, rate-capped in `channels.yaml`, mutable from the
dashboard and listed in the routing table, and four of them had never been
sent by anything. The operator was offered a choice about messages that did
not exist.

The four that went, and why:

* `agenda_started` and `agenda_blocked`. Autonomy picking a piece of work up
  is what the panel is for, and `awaiting_operator` already covers the case
  where it stopped and needs a decision. A ping for every pickup is the kind
  of noise that gets a channel muted, which costs the messages that matter.
* `upgrade_restarting` and `upgrade_applied`. They named an UpgradeManager,
  and there is no such thing anywhere in the tree. A kind describing a
  component that does not exist cannot be given a producer.

`producer` is a location, not an import path. It is read by a person deciding
whether a kind is worth having, and a string that has to stay importable is a
string that goes stale silently.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Broadcast:
    """One kind the runtime sends on its own."""

    #: What it is, in the operator's words. This is what a surface shows.
    summary: str
    #: Why it exists at all: what goes wrong for them if it never arrives.
    why: str
    #: Where it is emitted. Checked at boot against what the code contains.
    producer: str
    #: Does the operator have to see this even if they muted the kind or the
    #: channel is over its cap? Routing still decides WHERE: a kind they sent
    #: to no channel goes to no channel, this one included.
    must_be_seen: bool = False
    #: Does the channel's per-hour cap apply? The cap exists to stop the
    #: runtime flooding a channel with things it decided to say. A scheduled
    #: row's own output is not that: the operator chose the cadence, and one
    #: shared bucket would drop the twentieth row's result because the first
    #: nineteen had already fired. Muting still works, which is the half a
    #: person actually reaches for. `must_be_seen` bypasses both; this
    #: bypasses only the cap.
    rate_capped: bool = True


BROADCASTS: dict[str, Broadcast] = {
    "workspace_change_applied": Broadcast(
        summary="One of your documents changed itself, and here is what it now says.",
        why=(
            "A document that only asks is a document you approve; one that "
            "applies itself is one you would never hear about. The operator's "
            "own framing: it is auto, and it tells you afterwards. Without "
            "this the only trace is a card in an inbox nobody opened."
        ),
        producer="mirror/server/workspace_watch.py::_tell_them_it_applied",
    ),
    "awaiting_operator": Broadcast(
        summary="Autonomy is waiting on a decision from you before it can continue.",
        why=(
            "Work it started has stopped and nothing will retry it on its own. "
            "Without this it waits until you next open the app."
        ),
        producer="mirror/server/app.py::_on_item_parked + ::_on_worker_timeout",
        must_be_seen=True,
    ),
    "recovery_summary": Broadcast(
        summary="What was still open when the app restarted, and what it did about it.",
        why=(
            "A restart can leave a question you asked without an answer. This "
            "is the only place that says so."
        ),
        producer="mirror/server/app.py::_send_recovery_nudge",
        must_be_seen=True,
    ),
    "governor_pause": Broadcast(
        summary="It paused one of its own sources because that source was misbehaving.",
        why=(
            "Something it normally does has stopped happening, and the reason "
            "is a decision it made rather than a failure you would see."
        ),
        producer="mirror/server/app.py::_make_governor_notify",
    ),
    "crash_storm_latched": Broadcast(
        summary=(
            "It crashed repeatedly and has stopped trying to start itself again."
        ),
        why=(
            "Nothing will restart it and nothing else can tell you: the part "
            "that normally sends a message is the part that has gone."
        ),
        producer="supervisor/daemon.py::_announce_crash_storm",
        must_be_seen=True,
    ),
    "runtime_report": Broadcast(
        summary="The hourly check of the runtime found something that needs you.",
        why=(
            "A quiet runtime sends nothing at all, so this arriving always "
            "means there is something to act on."
        ),
        producer="scheduler/tasks/watchman.py",
    ),
    "schedule_failed": Broadcast(
        summary="Something that runs on a timer failed, and it is one you asked to hear about.",
        why=(
            "A row that fails quietly keeps failing. Only the rows you marked "
            "`on_failure: alert` send this, and a row can say where it goes."
        ),
        producer="scheduler/engine.py",
    ),
    "scheduled_task_result": Broadcast(
        summary="What one of the tasks you set up on a timer came back with.",
        why=(
            "It is the whole reason the row exists. A row that runs and "
            "delivers nowhere has only written a file you would have to go "
            "and find."
        ),
        producer="scheduler/tasks/scheduled_task.py",
        rate_capped=False,
    ),
    "runtime_repaired": Broadcast(
        summary="It put something right by itself, and this is what.",
        why=(
            "Four things broke in one day once, every one of them was seen "
            "and none was acted on. A runtime that heals and never says so is "
            "indistinguishable from one that did not notice, and the operator "
            "goes looking for a fault that is already gone."
        ),
        producer="scheduler/tasks/watchman.py",
        # Good news, so it never bypasses a mute and it never bypasses the
        # cap. The message that needs a person is `runtime_report`, and
        # sending "I fixed it" down that path is how a channel gets muted.
        must_be_seen=False,
    ),
    "voice_lane_down": Broadcast(
        summary=(
            "One of the voices it speaks with stopped working. Replies still "
            "arrive as text."
        ),
        why=(
            "Both voices were down for a day once and the way it was noticed "
            "was the silence."
        ),
        producer="mirror/server/app.py::_make_voice_lane_down_notify",
    ),
}

#: The daily brief is routed like a kind but is not one of these: it is a
#: scheduled job's output rather than something the runtime says about itself,
#: it carries no rate cap or mute, and its producer is the schedule row. Named
#: here so a reader looking for it does not conclude it was forgotten.
BRIEF_KIND = "daily_brief"


def check_producers(root: str | None = None) -> None:
    """Raise if any declared kind has no producer in the tree.

    Checked by reading the file each row names and looking for the kind's own
    string in it. Blunt on purpose: the alternative is importing every
    producer at boot, which would make a declaration able to run code, and the
    failure this guards against is a kind that nothing emits, not a kind whose
    emitter moved one line.
    """
    from pathlib import Path

    base = Path(root) if root else Path(__file__).resolve().parents[2]
    missing: list[str] = []
    for kind, spec in BROADCASTS.items():
        target = base / spec.producer.split("::", 1)[0]
        try:
            body = target.read_text(encoding="utf-8")
        except OSError:
            missing.append(f"{kind}: {spec.producer} cannot be read")
            continue
        if f'"{kind}"' not in body and f"'{kind}'" not in body:
            missing.append(f"{kind}: nothing in {spec.producer} sends it")
    if missing:
        raise RuntimeError(
            "broadcasts: a kind the runtime declares is never sent by "
            "anything. Give it a producer or delete it.\n  "
            + "\n  ".join(missing)
        )


__all__ = ["BRIEF_KIND", "BROADCASTS", "Broadcast", "check_producers"]
