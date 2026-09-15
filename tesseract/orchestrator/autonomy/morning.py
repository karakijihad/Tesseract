"""What the day may work, what it may spend, and the conversation it works in.

Both rows of the day read this file: `morning` decides what is worth doing and
`workday` works one step per wake. They share it rather than each answering
these questions for itself, because a second answer to "may this project be
touched" or "is there room left today" is a second ceiling, and two ceilings
drift until one of them is wrong.

The rule about place comes first, because it is the one that cannot be bought.

Operator ruling, 2026-09-09: the kernel is not the morning's to work. Unattended
morning work reaches `workshop/projects/*` and nothing else, and kernel work
stays directed from the chair.

**Keyed on where a project LIVES, never on its id or its name.** `proj-tesseract`
and `proj-workshop` are the two rows that have to be excluded today, and naming
them would be a rule a rename defeats: an id is minted from a name
(`mint_project_id`) and a name is the operator's to change. A project is the
morning's when its root is a direct child of `<home>/workshop/projects/`, which
excludes those two by construction and keeps excluding them under any name.

It also excludes a project registered somewhere else on the disk entirely, and
that is the point rather than a side effect. The operator can point the
assistant at their own repository, and a directory they linked so they could
work in it together is not a directory the runtime may go into by itself at
eight in the morning.

**Roots are compared resolved**, because the registry stores them resolved
(`models.normalize_root`) and `Path.resolve()` follows a junction to its target.
A link dropped inside `workshop/projects/` that points at the repo therefore
resolves to the repo and fails the check, which is the same reasoning
`seal_guard::safe_cwd` applies after its `mkdir`.

**A budget is the second half and it is a different question.** Being the
morning's is about place; having room today is about money, and this module
answers only the first. A project priced at zero IS the morning's and simply
has no room, which is the operator saying "not this one" in a way they can see
and undo. A project with no budget at all was never priced, so there is nothing
to spend against and no step may be proposed for it.

What this does NOT check is whether the root still exists on disk. A registered
directory that has been deleted is a registry problem, `project_open` already
reports it in those words, and a stat per project here would be this module
predicting a failure the step itself reports better.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.orchestrator.projects.models import Project
from tesseract.permissions.decide import NO_APPROVER_REASON

log = logging.getLogger(__name__)

#: The `deny_reason` `permissions/decide.py::evaluate` writes when a tool
#: asked for the operator's approval and no `ask_fn` was wired: a write
#: refused for want of an approver, as against every other way a call can
#: fail.
_NO_APPROVER = NO_APPROVER_REASON

#: The stop rule's `kind` for this condition. Not a row in
#: `healing/remedies.py`: there is no remedy to run, the same reason a
#: governor pause carries no remedy row, only a decision the operator makes.
_BOUNDARY_KIND = "autonomy_write_refused"

#: What a morning conversation is called on disk, and the reason there is one
#: per day rather than one per wake. The phase's item 4 works a step per wake
#: and asks that "the record reads as one day", so the wakes after the first
#: rejoin the conversation the morning opened rather than starting again with
#: no idea what was already decided.
_MORNING_CHANNEL = "morning"


def morning_setting(key: str) -> int:
    """One number out of `agenda.yaml::morning`, or a raised `KeyError`.

    **Raises rather than defaulting.** Every limit on unattended work is read
    through here, and a ceiling that quietly falls back to a number written in
    Python is a ceiling nobody set. That is the rule `config` carries for every
    infrastructure value in this tree.

    One reader because there were two: `morning_prompt.max_steps` and
    `project_propose.every_days` built the same path, parsed the same file,
    checked the same block and raised the same shape of error, so a change to
    where that block lives had two places to reach.

    **`config_dir()`, which is the config the operator edits.** It was
    `TESSERACT_DIR/config`, inherited from `max_steps` and carried forward into
    a second reader before an audit caught it. On this checkout the two are the
    same directory, which is why it was invisible; in a packaged install
    `TESSERACT_DIR` is inside the sealed `app/` tree and `config_dir()` is the
    operator's own, so every limit here was read off the shipped template and
    no edit of theirs ever reached it. The kernel already reads its own caps
    through `CONFIG_DIR` (`app.py`), so the two halves of one file were being
    read from two trees.
    """
    import yaml

    from tesseract.paths import config_dir

    path = config_dir() / "agenda.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = raw.get("morning")
    if not isinstance(block, dict) or key not in block:
        raise KeyError(f"{path}: morning.{key} is missing")
    return int(block[key])


def workshop_projects_dir() -> Path:
    """``<TESSERACT_HOME>/workshop/projects``, resolved at call time.

    Call time for the reason `projects/paths.py` gives: a module-level constant
    freezes whichever home was live when the first importer touched the file,
    and that is the shape that leaks test writes into the operator's tree.
    """
    from tesseract.paths import home_dir

    return (home_dir() / "workshop" / "projects").resolve()


def is_the_mornings(project: Project) -> bool:
    """Whether unattended morning work may reach this project at all.

    Place only. Money is `has_room_today`'s question, and the two are kept
    apart so a project that is out of budget is never confused with one the
    morning was never allowed to touch.
    """
    try:
        root = Path(project.root).resolve()
    except (OSError, ValueError):
        # A root that will not resolve is not one the morning goes into. It
        # fails closed rather than raising, because this runs over every
        # registered project and one bad row must not take the whole morning
        # down with it.
        return False
    return root.parent == workshop_projects_dir()


def workable_projects(projects: list[Project]) -> list[Project]:
    """The projects a morning may propose steps for, in registry order.

    Both halves: it is the morning's by place, and it has been priced. A
    project with no budget was never priced, so there is no ceiling to spend
    against and no step may be proposed for it. That is the whole of "nothing
    is spent on work the operator has not priced".
    """
    return [
        project
        for project in projects
        if is_the_mornings(project) and project.budget_usd is not None
    ]


def why_not(project: Project) -> str:
    """Why this project gets no morning step, in the operator's words, or "".

    Written for a surface rather than a log: a project that never gets worked
    is a thing the operator has to be able to ask about, and "it is not
    eligible" answers nothing they can act on. Empty means it IS workable.
    """
    if not is_the_mornings(project):
        return (
            "it lives outside the workshop, so it is only worked when you ask"
        )
    if project.budget_usd is None:
        return "it has no daily budget, so nothing may be spent on it"
    return ""


def morning_chat_id(day: date) -> str:
    """The one chat record a given morning writes to, stable across restarts.

    `durable_chat_id` is the channels' helper and is reused rather than
    reimplemented: it is a uuid5 over a namespace and a pair of strings, so
    "the morning of the ninth" resolves to the same 32 hex characters however
    many times the app is restarted that day. A random id would open a fresh
    record on every wake and the day would read as four conversations that
    never met.
    """
    from tesseract.integrations._channel_session import durable_chat_id

    return durable_chat_id(_MORNING_CHANNEL, day.isoformat())


def open_morning_session(
    app: Any, *, day: date | None = None, row: str = "morning"
) -> Any:
    """The day's own conversation, opened through the one seam.

    **One conversation, two rows.** `row` is the scheduler row this wake came
    from and it decides the funnel door the turn records, nothing else: the
    chat id is the day's either way, so the morning and every wake after it
    rejoin one record. A wake that recorded `schedule:morning` would say the
    decision and the work arrived through the same door, and the panel reading
    those doors would be reading a row that did not fire.

    Built through `session_factory::_build_chat_session` because that function
    is where a session is given its funnel door, and its own comment says a
    ChatSession built anywhere else records no turns. A morning that recorded
    no turns would be the one kind of work on this machine that leaves no
    trace, which is the opposite of the phase's point.

    **No ask callback, and that is the documented headless behaviour rather
    than an omission.** `permissions/decide.py::evaluate` auto-allows read-only
    tools and denies everything else when there is no `ask_fn`, on the rule
    that absence of an approver is refusal. On this install the question does
    not arise: `free` resolves a posture to auto before an ask is reached, so
    nothing the morning does stops here. On a shipped `max` install it does
    arise, and the morning's writes are refused rather than acted on, which is
    the safe end. Passing a denier here would look like an answer to that
    while being the same refusal with a callback in front; the actual answer
    is `send_one_turn` reading the refusal off the turn and filing an advice
    card so the phone is told instead of nobody.

    Raises `ChatInfraNotReady` if the backend is still booting, like every
    other caller of the factory.
    """
    import uuid

    from tesseract.integrations._channel_session import restore_history, restore_meta
    from tesseract.mirror.server.session_factory import (
        NullWebSocket,
        deny_overage,
        noop_cli_sink,
        noop_status_emit,
    )
    from tesseract.lib.clock import today
    from tesseract.mirror.server.event_log import EventLog
    from tesseract.mirror.server.session import ServerSession, _build_chat_session

    when = day or today()
    session_id = f"{row}_{when.isoformat()}_{uuid.uuid4().hex[:8]}"
    chat_session = _build_chat_session(
        app,
        session_id,
        None,
        noop_cli_sink,
        deny_overage,
        noop_status_emit,
        kind="autonomy",
        turn_entry=f"schedule:{row}",
    )
    session = ServerSession(
        session_id=session_id,
        ws=NullWebSocket(),
        chat_session=chat_session,
        event_log=EventLog(),
        pending_asks={},
        pending_overage_asks={},
        kind="autonomy",
        active_chat_id=morning_chat_id(when),
    )
    restored: list[Any] = []
    restore_history(chat_session, session.active_chat_id, out=restored)
    # After the ServerSession exists, for `_channel_session`'s own reason: the
    # meta it repairs is the one `__post_init__` just minted with a wake-time
    # clock, so a morning returned to at noon would report itself as created
    # then and the day would lose its own start.
    restore_meta(session, session.active_chat_id, restored[0] if restored else None)
    return session


#: How much of the model's own words reach a row's reason. The reason is a line
#: on a panel, not a transcript, and the turn itself is in the chat record under
#: the day's own chat id.
_REASON_CHARS = 240


def _boundary_card_since(day: date) -> datetime:
    """The `since` a day's repeated wakes share, so one boundary mints one card.

    `stop_rule.card_id` hashes `since` into the item's id (truncated to the
    minute through `mint_agenda_id`), the same way `governor.
    _ask_if_it_keeps_coming_back` pins `since` to the pause chain's start
    rather than the tick that happens to be filing. There is no chain to read
    here, so the day itself is the anchor: every wake between the morning and
    the day's last hourly step reads the SAME `since` and therefore mints the
    SAME id, which is what makes a second wake hitting the same tool update
    the standing card instead of opening a second one. A boundary still
    standing tomorrow gets tomorrow's card, on the same reasoning the day's
    budget and its ledger both reset at midnight rather than carrying a debt
    forward silently.
    """
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _file_boundary_card(raw: dict[str, Any], *, origin: str, since: datetime) -> None:
    """The operator's advice card for a write the day could not ask about.

    Reached from `send_one_turn`'s own loop, on a `TOOL_RESULT` chunk whose
    `deny_reason` is exactly `_NO_APPROVER`: an ASK-postured tool with nobody
    to ask, which is the one refusal that otherwise tells the operator
    nothing. Every other refusal a tool can return, a policy DENY, a
    bash-security block, a path-validator refusal, is an ordinary tool error
    the turn already reports in its own words and mints no card here.

    Invariants held at once, because this runs inside a wake that must
    degrade rather than stop:

    - **One card per boundary per subject, deduped across repeated wakes.**
      `subject` is the tool's name, `since` is pinned to the calendar day
      (`_boundary_card_since`) rather than this call's own clock, so the
      morning's wake and every workday wake that hits the same tool on the
      same day resolve to one `card_id` and `stop_rule.file_card` updates the
      standing item instead of opening a second one.
    - **No card for a refusal that is not this boundary.** The exact-string
      compare at the call site is what keeps a `free`-mode bash-security
      refusal, or any DENY the policy already decided, out of this path: on
      `free` the six forced-ASK bash checks write a different `deny_reason`
      and every ordinary tool resolves AUTO before an ask is ever reached
      (`permissions.yaml` is the authority), so `_NO_APPROVER` is not written
      for them.
    - **The turn still degrades as today.** This is a side effect read off a
      chunk the loop already receives; it neither stops the turn nor changes
      what `send_one_turn` returns, so a boundary costs a card and nothing
      else, exactly as `open_morning_session`'s own docstring already says
      the refusal is the safe end.
    - **Same funnel.** `stop_rule.file_card` mints the identical `AgendaItem`
      shape the recovery pass and the governor file, so the card is an
      ordinary advice card: answerable from the phone, on the panel that
      already reads that shape.
    - **No posture decided here.** This reads `deny_reason`, which is
      `decide.py`'s own record of a decision `permissions.yaml` already made;
      nothing here asks, grants, or overrides a posture.

    Never raises: a card that could not be filed costs a card, not the wake,
    the same contract `stop_rule.file_card` and `governor.
    _ask_if_it_keeps_coming_back` both hold for the same reason.
    """
    if raw.get("deny_reason") != _NO_APPROVER:
        # The one gate, held here rather than at the call site, so a caller
        # cannot accidentally widen it by skipping the compare. Every OTHER
        # `denied_hard` refusal is an ordinary tool error the turn already
        # reports in its own words.
        return
    tool_name = str(raw.get("tool_name") or "")
    if not tool_name:
        return
    try:
        from tesseract.orchestrator.healing import stop_rule

        stop = stop_rule.advice_owed(
            kind=_BOUNDARY_KIND,
            subject=tool_name,
            said=(
                f"the {origin} tried to use {tool_name} and nobody was there "
                f"to approve it. This machine asks before that tool runs, and "
                f"an unattended turn has no one to ask, so the step stopped "
                f"there rather than running it. What would fix this is "
                f"deciding whether {tool_name} may run unattended at all, or "
                f"staying at the desk for the steps that need it"
            ),
        )
        stop_rule.file_card(stop, since=since, now=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("morning: could not file the boundary card for %s", tool_name)


async def send_one_turn(
    app: Any, prompt: str, *, origin: str, counting: str
) -> tuple[str, int]:
    """One unattended turn in the day's conversation: what it said, and how
    many times it called `counting`.

    Both rows of the day send through here rather than each opening a session
    of its own. A row that built its own would be a second answer to where
    unattended work is recorded, and the two would drift apart on the one thing
    that has to hold: the morning and every wake after it are one conversation,
    so the wake at noon reads what the morning decided instead of starting
    again with no idea.

    **What came of the turn is COUNTED, not parsed.** A morning that proposed
    nothing is told apart from one that proposed three by the `task_propose`
    calls on the turn, and a wake that closed its step from one that ran out of
    road by the `task_close` calls, because that is the record. The model is
    asked to say so in words as well, and the words reach the row's reason for
    the operator to read, but nothing keys off the string.

    The session is opened per wake rather than held between them, because a row
    that kept one alive would be a second lifetime for a conversation the chat
    record already owns.

    `save_now` at the end for the reason the cockpit runs an autosave pump: a
    turn that is not written is a turn the next wake cannot read, and these
    rows have no websocket to close and flush behind them. Threaded, because
    its own docstring says it is called from a worker thread by that pump and
    it was measured past the 50ms the loop may block for.

    **A write refused for want of an approver files the operator's card.**
    `open_morning_session`'s own docstring names the shape: on a shipped
    `max` install there is no `ask_fn`, so an ASK-postured tool is refused
    rather than acted on. That refusal used to reach nobody. `_file_boundary_card`
    is the seam, reached here because this is the one place both rows of the
    day read every `TOOL_RESULT` chunk; see its docstring for the invariants.
    """
    from tesseract.brain.chat import ChunkType
    from tesseract.lib.clock import today
    from tesseract.mirror.server.session_autosave import save_now

    since = _boundary_card_since(today())
    session = open_morning_session(app, row=origin)
    text: list[str] = []
    counted = 0
    try:
        async for chunk in session.chat_session.send(prompt, runtime_origin=origin):
            if chunk.type == ChunkType.TEXT and chunk.text:
                text.append(chunk.text)
            elif chunk.type == ChunkType.TOOL_CALL_START:
                call = chunk.tool_call
                if call is not None and call.name == counting:
                    counted += 1
            elif chunk.type == ChunkType.TOOL_RESULT and chunk.raw.get("denied_hard"):
                # `_file_boundary_card` is the one place that decides whether
                # THIS denial is the no-approver boundary; every other
                # `denied_hard` refusal reaches it and is turned away there.
                await asyncio.to_thread(
                    _file_boundary_card, chunk.raw, origin=origin, since=since
                )
    finally:
        # In a `finally`, because a turn that fell over mid-stream was billed
        # for what it managed and `ChatSession` has already appended it.
        # Skipping the write there would lose the one conversation on this
        # machine that nobody was watching, and the next wake would rejoin a
        # record that says the day never happened.
        try:
            await asyncio.to_thread(save_now, app, session)
        except Exception:  # noqa: BLE001 - the turn happened either way
            log.warning("the day's chat record was not written", exc_info=True)
    return ("".join(text).strip()[:_REASON_CHARS], counted)


def day_is_capped(app: Any) -> str:
    """Why no unattended row may run right now, or `""` when one may.

    `agenda.yaml::daily_caps.usd` is a ceiling on EVERYTHING the machine spent
    today, the operator's own conversations included, and it binds both rows of
    the day for the same reason it binds the kernel's dispatch: a cap that
    stopped half the unattended spend would be half a ceiling. The project
    budgets sit inside it and are the tighter bound on any ordinary day.

    **Second reader, not a second rule.** `KernelConfig.from_yaml_dict` is the
    one loader of the cap and `CostLedger.snapshot()` the one reader of today's
    total, which is exactly the pair `kernel.py::_check_daily_caps` compares.

    `exceed_behavior` does not branch here. It is `pause`, `operator_gate_all`
    or `stop`, and with nobody watching all three mean the same thing: the row
    does not begin. Gating on an operator who is not there would be an ask that
    times out, which is a refusal that also spent the turn.

    **Fails CLOSED, and this said the opposite for one commit.** The claim then
    was that it followed `_read_day_spend`'s rule; it does not, and the
    difference is what an audit caught. The kernel gives up only the USD LEG of
    its ceiling when the accessor throws, and its token and second caps still
    bind, so it is never left with no bound at all. Here there is one number,
    so giving it up gives up the whole ceiling, and `app["cost_ledger"]` being
    absent is a real boot state rather than a hypothetical. That is the shape
    `spent_today_by_project` already refuses: a ceiling nobody can see is not a
    ceiling, and the rows say so rather than proceeding.

    **A cap of zero is the operator disabling the ceiling; a cap that is not
    there is a file nobody finished.** `KernelConfig.from_yaml_dict` maps an
    absent `daily_caps.usd` to 0.0, so reading only its answer made deleting
    the key indistinguishable from setting it to nothing, and a function that
    claims to fail closed would have opened on a missing line. The key's
    presence is checked here and the value still comes from that one loader.
    """
    import yaml

    from tesseract.orchestrator.autonomy.kernel import KernelConfig
    from tesseract.paths import config_dir

    try:
        raw = yaml.safe_load(
            (config_dir() / "agenda.yaml").read_text(encoding="utf-8")
        ) or {}
        declared = (raw.get("daily_caps") or {})
        if "usd" not in declared:
            raise KeyError("daily_caps.usd is missing")
        cap = KernelConfig.from_yaml_dict(raw).daily_usd_cap
    except Exception as exc:  # noqa: BLE001 - a ceiling nobody can read is not one
        log.warning("the day's ceiling could not be read: %s", exc)
        return (
            "what this machine may spend in a day could not be read, so there "
            "is no ceiling to work under. Nothing ran and nothing was spent"
        )
    if cap <= 0:
        return ""

    ledger = app.get("cost_ledger") if hasattr(app, "get") else None
    if ledger is None:
        return (
            f"what this machine has already spent today could not be read, so "
            f"the ${cap:.2f} ceiling cannot be checked. Nothing ran and "
            f"nothing was spent"
        )
    try:
        spent = float(ledger.snapshot()["global"]["spent_usd"])
    except Exception:  # noqa: BLE001 - as above
        log.warning("today's total could not be read", exc_info=True)
        return (
            f"what this machine has already spent today could not be read, so "
            f"the ${cap:.2f} ceiling cannot be checked. Nothing ran and "
            f"nothing was spent"
        )
    if spent >= cap:
        return (
            f"everything on this machine has spent ${spent:.2f} today against "
            f"a ${cap:.2f} ceiling, so nothing runs on its own until tomorrow"
        )
    return ""


def spent_today_by_project(day: date | None = None) -> dict[str, float] | None:
    """What each project has been billed for today, or `None` if unread.

    **`None` and `{}` are different answers and both reach a decision.** An
    empty mapping is a ledger that was read and holds nothing for today, which
    prices every project at zero and is right. `None` is a ledger nobody could
    read, and a caller that treated the two alike would hand the morning a
    budget that looks untouched on the one day it cannot see. That is
    `playbook_reuse.cost_by_turn`'s contract and it is kept for its reason.

    **The parse is not here.** `routes/autonomy_history.py` is the one reader
    of this ledger and says so; `runtime_tuning` already asks it rather than
    opening the file, and a morning that parsed rows itself would be the third
    place a change to the row format has to reach. This resolves where the
    ledger is, hands the question over, and turns a store that will not answer
    into `None`.

    **Blocking, on purpose, and the caller threads it.** The read behind it
    parses a file that only grows. `asyncio.to_thread` at the call site keeps
    that off the loop that health, heartbeats and every other conversation
    share.

    One limit is inherited rather than chosen: the shared reader tails the last
    `autonomy_history.MAX_LOG_BYTES` of the file, so a day whose rows fall
    outside that window is understated. Every consumer of that reader has the
    same property, and for the panel it costs a slightly short chart. Here it
    would cost room the morning does not have, which is the one place this
    matters, and the ledger would have to be several times its present size
    before the window could cut a single day.
    """
    from pathlib import Path as _Path

    from tesseract.brain.cost.ledger import configured_log_path
    from tesseract.lib.clock import today
    from tesseract.mirror.server.routes.autonomy_history import project_spend

    when = day or today()
    path = configured_log_path()
    if path is None or not _Path(path).is_file():
        return None
    try:
        return project_spend(_Path(path), on=when, project_of=_project_of_task)
    except Exception:  # noqa: BLE001 - a store that will not answer is not a zero
        log.warning("morning: could not read today's spend by project", exc_info=True)
        return None


def _project_of_task(task_id: str) -> str:
    """The project a task belongs to, or `""` for one that names none.

    A task the reaper has already taken is not an error and answers `""`: the
    money was spent, the record of what on is gone, and no project's budget
    can honestly be charged for it.
    """
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore

    item = AgendaStore().get(task_id)
    return getattr(item, "project_id", "") if item is not None else ""


def room_left(project: Project, spent_today: dict[str, float] | None) -> float | None:
    """What this project may still spend today, or `None` if it cannot be said.

    `None` when the project was never priced or when the ledger could not be
    read, and those are the two cases where proposing a step would be spending
    against a ceiling nobody can see. A project priced at zero answers 0.0,
    which is a number and not an absence: the operator set it, and the morning
    honours it by proposing nothing.

    Never negative. A project already over its budget has no room rather than
    negative room, because the only thing a caller does with this is compare it
    against a step's estimate.
    """
    if project.budget_usd is None or spent_today is None:
        return None
    return max(0.0, project.budget_usd - spent_today.get(project.id, 0.0))


__all__ = [
    "CHECK_WORDS",
    "checks_named",
    "committed_today",
    "day_is_capped",
    "declared_checks",
    "is_the_mornings",
    "morning_chat_id",
    "morning_setting",
    "open_morning_session",
    "room_left",
    "send_one_turn",
    "spent_today_by_project",
    "why_a_step_is_refused",
    "why_not",
    "workable_projects",
    "workshop_projects_dir",
]


#: The four checks a project may declare, by the word a person would use for
#: each in a sentence. Read against `VerifyCommands`, whose field names these
#: are, so a fifth check added there has to be given its words here and the
#: test over `VerifyCommands.model_fields` fails until it is.
CHECK_WORDS: dict[str, tuple[str, ...]] = {
    "test": ("test", "tests", "suite"),
    "typecheck": ("typecheck", "typechecks", "types"),
    "lint": ("lint", "lints", "linter"),
    "live": ("live", "published", "deployed", "url"),
}


def checks_named(criteria: str) -> set[str]:
    """Which declared-check kinds this sentence claims will decide it.

    Whole words, case-folded, so "the latest run" does not read as `test` and
    "it lives under scripts/" does not read as `live`. It is deliberately a
    small closed vocabulary rather than an attempt to understand the sentence:
    the question is only whether the criteria leans on a check, and a criteria
    that leans on one nobody declared cannot be closed by anything but a
    sentence.
    """
    import re

    words = set(re.findall(r"[a-z]+", criteria.casefold()))
    return {
        kind for kind, spellings in CHECK_WORDS.items() if words & set(spellings)
    }


def committed_today(project_id: str, day: date | None = None) -> float:
    """What today's un-run steps on this project have already claimed, in USD.

    **The ledger cannot answer this and that is the whole reason it exists.**
    A step is billed when it is WORKED, so between the morning proposing three
    and the day working the first, the ledger records nothing for two of them.
    Each was checked against the same room and each fitted, which is how a
    project's daily ceiling was passed by proposals that individually honoured
    it.

    Counted only for steps PROPOSED TODAY that have not run. Two bounds, and
    each is needed:

      * a step that has run is in the ledger, so counting its estimate as well
        would charge the project twice for one piece of work;
      * a step proposed last week and never taken up is not a claim on today's
        money, and counting it would let an abandoned task hold a budget shut
        for as long as nobody cancelled it.

    A store that will not answer counts zero. That is the fail-open direction,
    and it is the right one here only because it cannot stand alone: the
    ledger check beside it fails CLOSED, so an unreadable agenda costs
    tightness rather than the ceiling itself.
    """
    from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
    from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus
    from tesseract.lib.clock import today as _today

    if not project_id:
        return 0.0
    when = day or _today()
    total = 0.0
    try:
        for item in AgendaStore().iter_active():
            if item.source is not AgendaSource.TASK:
                continue
            if item.project_id != project_id:
                continue
            if item.status is not AgendaStatus.PROPOSED or item.attempts:
                continue
            if _local_day(item.created_at) != when:
                continue
            total += float(item.estimated_cost_usd or 0.0)
    except Exception:  # noqa: BLE001 - see the docstring
        log.warning("could not read what today has committed", exc_info=True)
        return 0.0
    return total


def _local_day(when: "datetime") -> date:
    """The operator's calendar day for an instant, treating a naive one as UTC.

    `astimezone()` on a naive datetime reads it as LOCAL, which is a different
    instant from the UTC every writer here stores, and would put a promise in
    the wrong day. Nothing in the load path refuses a naive `created_at`:
    `_load_by_version` checks the schema version and the contract fields and
    nothing else, and `agenda_store.find_fuzzy_dedupe` already carries this
    exact guard, which is the evidence that such a record can reach a reader.
    """
    from datetime import timezone

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().date()


def why_a_step_is_refused(
    project: Project | None,
    criteria: str,
    estimate_usd: float | None,
    spent_today: dict[str, float] | None,
    committed_usd: float = 0.0,
) -> str:
    """Why an UNATTENDED proposal may not be made, or `""` when it may.

    **Only for a session nobody is watching.** In the operator's own chat every
    one of these is a conversation: they can price a project, say they will
    check it by hand, or decide the budget does not apply today. Unattended
    there is nobody to say any of that, so the same three questions have to be
    answered by the record or the step is not proposed.

    Three refusals, in the order they cost. Place first, because a project
    outside the workshop is not the morning's however cheap the step. Then the
    money, because a step with no ceiling has no bound at all. Then the
    contract, because a criteria naming a check the project does not declare
    closes on a sentence, and a sentence written by the same model that
    proposed it is not evidence.

    Written as sentences rather than codes: this reaches the model as the
    refusal it acts on, and the operator as the reason a morning did nothing.
    """
    if project is None:
        return (
            "a step worked with nobody watching has to name a project, so its "
            "budget and its checks can decide it"
        )
    if not is_the_mornings(project):
        return (
            f"{project.name} lives outside the workshop, so it is only worked "
            "when you ask. Nothing unattended goes in there"
        )
    room = room_left(project, spent_today)
    if room is None:
        return (
            f"{project.name} has no daily budget, or today's spend could not "
            "be read, so there is no ceiling for this step to fit inside"
        )
    # What today already promised comes off before anything is compared. The
    # ledger holds what was SPENT and these are steps nobody has worked yet.
    room = max(0.0, room - max(0.0, committed_usd))
    if room <= 0:
        return (
            f"{project.name} has spent or already promised its budget for today"
        )
    if estimate_usd is None:
        return (
            f"a step on {project.name} has to estimate what it will cost, "
            f"because ${room:.2f} is what is left today"
        )
    if estimate_usd > room:
        return (
            f"that step is estimated at ${estimate_usd:.2f} and {project.name} "
            f"has ${room:.2f} left today"
        )
    if not criteria.strip():
        return (
            f"a step on {project.name} has to say what evidence would show it "
            "is done, and its success criteria is blank"
        )
    undeclared = checks_named(criteria) - set(declared_checks(project))
    if undeclared:
        return (
            f"the criteria says a {', '.join(sorted(undeclared))} check decides "
            f"it and {project.name} declares none, so nothing would run and it "
            "would close on your own word"
        )
    return ""


def declared_checks(project: Project) -> set[str]:
    """The check kinds this project actually declares, by name."""
    verify = project.verify
    return {
        kind
        for kind in CHECK_WORDS
        if str(getattr(verify, kind, "") or "").strip()
    }
