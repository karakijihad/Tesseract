"""What the runtime is saying, with no channel's markup in it.

Every kind the runtime sends on its own is composed here into a `Message`: a
title, some prose, the machine facts it needs to name, a list, a footer. How
that reads on a channel is that channel's, and it renders one from the same
value. Telegram happens to make the title bold and the facts monospace;
somewhere else may not be able to, and will still be able to say the same
thing.

It used to build Telegram-HTML directly, in `orchestrator/`, which meant a
second channel would have received `<b>` tags in its text and the only fix
would have been eleven templates copied behind a second adapter. That is the
"10000 adapters" shape: the message is one thing and its reading is many, so
the split is here rather than there.

The parts, and each is here because a template needed it:

* `title` is the headline. Short, and never a sentence.
* `mark` is a severity marker where the kind has one. Only the runtime report
  does, and only red or orange: green never arrives, because a runtime with
  nothing to act on sends nothing at all.
* `body` is the prose, which is what a person actually reads.
* `facts` are `(label, value)` pairs where the value is machine text: an id, a
  provider ref, a file path, a reply token. A channel that can set them apart
  should; one that cannot still labels them.
* `sections` are `(label, prose)` blocks, for a message long enough to have
  parts. The daily brief is the one that does.
* `bullets` are a list where the list IS the content. The runtime report's
  findings are the reason this exists. `bullets_label` heads the list where it
  needs a heading, and goes with it if no entry survives.
* `payload` says the body IS the thing rather than a ping about it. A row the
  operator set up on a timer reports what it came back with, and holding that
  to the length a one-event ping is held to cuts the only part of the message
  that was ever the point. Nothing here cuts a payload; the channel splits it
  into as many messages as it takes.
* `footer` is the pointer that goes last and must survive truncation, as a
  `(label, value)` pair so a channel can still set the value apart. A
  message that drops the link to fit one more finding has lost the only
  part of itself that reaches the ones it could not carry.
* `actions` are the answers a reader can give without typing one. Each is a
  label, the verb it means, and what it acts on, and NOT a button: a channel
  that has buttons draws buttons, a channel that has none is unchanged,
  because every action here is also a reply the message already spells out in
  words. That is the rule they must keep. An action a reader can only reach by
  tapping would be a control that exists on one channel, which is the fork
  this whole funnel exists to prevent.

Escaping and markup are never done here: both are answers to "how does this
read HERE", and both belong to the channel. `render_plain` at the bottom is
the reading a channel gets when it has no opinion, and it lives beside the
model because plain sentences are what the message already is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

Pair = tuple[str, str]


@dataclass(frozen=True)
class Action:
    """One answer a reader can give, in no channel's idiom.

    `verb` and `target` are the runtime's own words for what the answer means
    and what it acts on, so a channel encodes them however its buttons work
    and hands them straight back. Nothing here knows what a callback is.

    **An action must always also be sayable.** The message that carries one
    spells the same answer out in words, so a channel with no buttons loses
    nothing and a button that cannot be drawn is not a control lost.
    """

    label: str
    verb: str
    target: str


@dataclass(frozen=True)
class Message:
    title: str = ""
    mark: str = ""
    body: str = ""
    facts: tuple[Pair, ...] = ()
    sections: tuple[Pair, ...] = ()
    bullets: tuple[str, ...] = ()
    bullets_label: str = ""
    footer: Pair | None = None
    payload: bool = False
    actions: tuple[Action, ...] = ()
    #: The body was written by a model over counted facts and judged before it
    #: got here. `outbound_writer` reads this and leaves such a message alone:
    #: a second pass spends twice to say the same thing worse. Set by the
    #: template that had a narration to lead with, and by the writer itself
    #: once it has written one.
    written: bool = False

    @property
    def is_long(self) -> bool:
        """Is this message more than a ping about one thing?

        A renderer needs to know, because a message whose parts ARE the point
        cannot be held to the length a one-event ping is held to. Three of ten
        findings once reached the operator while seven were unreachable from
        the message, and the brief is nothing but parts.
        """
        return bool(self.bullets or self.sections or self.payload)


def _text(context: dict[str, Any], key: str) -> str:
    return str(context.get(key) or "").strip()


def _item_id(context: dict[str, Any]) -> str:
    return _text(context, "item_id") or _text(context, "agenda_id")


def _lines(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def compose(category: str, context: dict[str, Any]) -> Message:
    """The runtime's own words for one kind, in no channel's dialect."""
    if category == "awaiting_operator":
        item = _item_id(context)
        gates = context.get("gates")
        waiting = (
            ", ".join(str(gate) for gate in gates)
            if isinstance(gates, list) and gates else ""
        )
        facts = _maybe(("Item", item), ("Waiting on", waiting))
        if item:
            facts += (
                ("Reply", f"{item}:approve · {item}:deny · {item}:snooze"),
            )
        return Message(
            title="Awaiting operator",
            body=_lines(_text(context, "goal"), _text(context, "rationale")),
            facts=facts,
            # The same two answers the Reply line spells out. Snooze is not
            # here on purpose: it is a nuance, it stays typed, and three
            # buttons where two decisions exist is how a tap becomes a
            # guess. A channel with no buttons shows the Reply line and is
            # not missing a control.
            actions=(
                (
                    Action(label="Approve", verb="approve", target=item),
                    Action(label="Drop it", verb="deny", target=item),
                )
                if item
                else ()
            ),
        )

    if category == "recovery_summary":
        # The count of conversations is named separately from the count of
        # things needing attention, because it is the one thing here the
        # operator is on the other end of: a question they asked that never
        # got answered. A bare "3 need operator" does not say that.
        return Message(
            title="Recovery",
            body=_lines(_text(context, "text")),
            facts=_maybe(
                ("Boot", _text(context, "boot")),
                ("Got no reply", _text(context, "unanswered")),
                ("Need you", _text(context, "attention")),
            ),
        )

    if category == "governor_pause":
        return Message(
            title="Governor",
            body=_lines(
                "It paused one of its own sources because that source was "
                "misbehaving.",
                _text(context, "reason"),
            ),
            facts=_maybe(
                ("Source", _text(context, "source")),
                ("Noticed by", _text(context, "detector")),
            ),
        )

    if category == "crash_storm_latched":
        return Message(
            title="Crash storm",
            body=_lines(
                "It kept crashing and has stopped trying to start itself again.",
                _text(context, "reason"),
            ),
        )

    if category == "runtime_report":
        return _runtime_report(context)

    if category == "schedule_failed":
        # The consequence decides the sentence. A row that will try again is
        # news; a row that has stopped is a thing the operator has to go and
        # switch back on, and a message that does not say which is which
        # leaves them to open the app to find out.
        stopped = bool(context.get("circuit_broken"))
        return Message(
            title="Schedule",
            body=_lines(
                "Something that runs on a timer failed.",
                (
                    "It has stopped trying and will not run again until you "
                    "switch it back on."
                    if stopped else
                    "It will try again at its next turn."
                ),
            ),
            facts=_maybe(
                ("Row", _text(context, "job_name")),
                ("What went wrong", _text(context, "detail")),
                ("Failed in a row", _text(context, "consecutive_failures")),
            ),
        )

    if category == "scheduled_task_result":
        # The row's own output, and nothing wrapped around it. A title is
        # what the operator called the row, so it heads the message: with
        # twenty rows armed, which one spoke is the first thing to know.
        #
        # It is a payload, not a ping. Held to the ping budget it arrived cut
        # at 512 characters with the rest unrecoverable, where the handler
        # this replaced had sent the whole answer across as many messages as
        # it took.
        return Message(
            title=_text(context, "title"),
            body=_text(context, "body"),
            payload=True,
        )

    if category == "voice_lane_down":
        # The consequence first. "kokoro latched" says nothing to someone
        # holding a phone; "it cannot speak" is the thing that changed.
        return Message(
            title="Voice",
            body=_lines(
                "One of the voices it speaks with has stopped working.",
                _text(context, "reason"),
                "Replies still arrive as text, and it tries that voice again "
                "by itself.",
            ),
            facts=_maybe(("Voice", _text(context, "lane"))),
        )

    return Message(body=_text(context, "text") or category)


def _runtime_report(context: dict[str, Any]) -> Message:
    """The watchman, and only when it found a defect.

    ONE set, and it arrives already chosen. The header counted one thing, the
    narration was built from every finding, and the bullets counted again:
    three sets in one message, which is how "1 thing(s) went wrong" came to sit
    above a paragraph about two things.
    """
    from tesseract.orchestrator.watchman.report import marker

    lines = [str(line) for line in (context.get("lines") or [])]
    count = int(context.get("defects") or len(lines))
    # The plain-sentence reading the `watchman` role wrote, checked against the
    # counted facts before it got here. It leads, because the counted lines are
    # source strings and a person who has not read this repo cannot tell from
    # them whether anything they care about stopped working.
    narration = _text(context, "narration")
    return Message(
        title="Runtime",
        mark=marker(_text(context, "severity")),
        body=narration or f"{count} thing(s) need you",
        written=bool(narration),
        bullets=tuple(lines),
        footer=(
            ("Full report", _text(context, "report_path"))
            if _text(context, "report_path") else None
        ),
    )


def _maybe(*pairs: Pair) -> tuple[Pair, ...]:
    return tuple((label, value) for label, value in pairs if value)


# -- Plain sentences -------------------------------------------------------


#: What a channel with no stated limit will take. Generous, because the only
#: message that runs long is the one whose list is its content, and cutting
#: that is worse than sending it.
DEFAULT_MAX_CHARS = 4000


def cut_plain(text: str, room: int) -> str:
    """As much of `text` as fits in `room`, marked as having been cut.

    The default for a channel whose text is only text. A channel that marks
    its text up passes its OWN cutter instead: how far into a string it is
    safe to stop is a fact about that markup, and this file has never known
    any. Cutting Telegram-HTML here at a character offset split tags and the
    bridge then shipped the whole message with every tag stripped.
    """
    return text[: max(0, room - 1)].rstrip() + "…" if room > 1 else ""


def assemble(
    *,
    head: str,
    body: str = "",
    facts: str = "",
    sections: tuple[str, ...] = (),
    bullets: tuple[str, ...] = (),
    bullets_label: str = "",
    footer: str = "",
    limit: float = DEFAULT_MAX_CHARS,
    bullet: str = "-",
    had_parts: bool = False,
    cut: Callable[[str, int], str] = cut_plain,
) -> str:
    """Lay out already-marked-up pieces, and make them fit.

    **Layout is the message's; markup is the channel's.** Every renderer calls
    this with its own strings, so the order of the parts and what happens when
    they do not fit are answered once.

    Three rules, and each is a defect that was paid for:

    * **The footer is reserved before anything else is laid out.** A message
      that drops its pointer to fit one more finding has lost the only part of
      itself that reaches what it could not carry.
    * **A section is dropped whole, never cut.** A section that loses its body
      loses its label with it, because a heading standing over nothing reads as
      the same fault as a sentence cut in half. The prose body is the one part
      that IS cut, because dropping it whole would leave a title and nothing.
    * **A list says what it left out.** A list that stops early without saying
      so reads as a list that was that short.

    **How far into a string it is safe to stop is the channel's**, so `cut` is
    passed in. The prose here is already marked up by whoever called, and a
    character offset lands wherever it lands: on Telegram it split tags, and
    the bridge's answer to markup it cannot parse is to strip ALL of it.

    `limit` may be `inf`, which is a message nothing here cuts. The channel
    splits it instead, which is what a row's own output needs.

    Returns "" when a message that is NOTHING BUT parts kept none of them: a
    payload-shaped empty ping is worse than no ping. A message with a body of
    its own still has something to say, so it is sent with what survived.
    """
    reserved = len(footer) + 2 if footer else 0
    budget = limit - reserved

    fixed = [head] if head else []
    if body:
        fixed.append(body)
    if facts:
        fixed.append(facts)
    text = "\n\n".join(fixed)
    if body and len(text) > budget:
        room = int(budget - (len(text) - len(body)))
        body = cut(body, room)
        fixed = [part for part in ([head] if head else []) + [body] + ([facts] if facts else []) if part]
        text = "\n\n".join(fixed)

    kept_parts = False
    for block in sections:
        if len(text) + len(block) + 2 > budget:
            break
        text = f"{text}\n\n{block}" if text else block
        kept_parts = True

    if bullets:
        before = text
        text = _fit_bullets(text, bullets, budget, bullet, bullets_label)
        kept_parts = kept_parts or text != before

    if had_parts and not kept_parts:
        return ""
    return "\n\n".join(part for part in (text, footer) if part)


def _fit_bullets(
    head: str,
    bullets: tuple[str, ...],
    budget: float,
    bullet: str,
    label: str = "",
) -> str:
    """As many entries as fit, and a line saying how many did not.

    The heading is charged for up front and dropped with the list when nothing
    fits, for the same reason a section's label goes with its body.
    """
    lead = f"\n\n{label}" if label else ""
    budget -= len(head) + len(lead)
    shown: list[str] = []
    for line in bullets:
        entry = f"\n{bullet} {line}"
        withheld = len(bullets) - len(shown) - 1
        tail = f"\n{bullet} and {withheld} more" if withheld else ""
        if len(entry) + len(tail) > budget:
            break
        budget -= len(entry)
        shown.append(entry)
    if not shown:
        # Not one entry fits. The heading goes with them: a "Vault" over
        # nothing, or a bare "and 4 more" with no list above it, both read as
        # a message that broke rather than one that ran out of room.
        return head
    out = head + lead + "".join(shown)
    if len(shown) < len(bullets):
        out += f"\n{bullet} and {len(bullets) - len(shown)} more"
    return out


def render_plain(message: Message, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Ordinary sentences. Every part of the message, in reading order."""
    footer = ""
    if message.footer:
        label, value = message.footer
        footer = f"{label}: {value}" if label else value
    return assemble(
        head=" ".join(part for part in (message.mark, message.title) if part),
        body=message.body,
        facts="\n".join(f"{label}: {value}" for label, value in message.facts),
        sections=tuple(
            f"{label}\n{prose}" if label else prose
            for label, prose in message.sections
        ),
        bullets=message.bullets,
        bullets_label=message.bullets_label,
        footer=footer,
        limit=max_chars,
        bullet="-",
        had_parts=bool((message.sections or message.bullets) and not message.body),
    )


__all__ = [
    "Action",
    "DEFAULT_MAX_CHARS",
    "Message",
    "assemble",
    "compose",
    "cut_plain",
    "render_plain",
]
