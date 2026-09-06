"""Everything that runs on its own, in one place.

Read it as the answer to "what does this machine do when nobody is asking it
to". A row is a `schedule.yaml` job; a service runs for as long as the app
does; a trigger waits on a named event; an on-demand entry is armed by the
operator. Four kinds of entry, one set.

Adding something that fires on its own means adding it here — a malformed entry
raises at import, and `checks.py` refuses a shipped row that appears in neither
place. That is the whole enforcement: the declaration cannot be skipped and
cannot quietly disagree with the schedule.

An embedding pass is not counted as this file's cost. Every memory writer
embeds, best-effort, whether or not the thing that called it wanted to — so
`kind` describes the call an entry chooses to make, and a row that writes a
memory is deterministic here.
"""

from __future__ import annotations

from tesseract.scheduler.manifest.entry import (
    DISPATCHED,
    Entry,
    Kind,
    Owner,
    Runs,
)

# ── Rows: `schedule.yaml`. Cadence is the operator's and lives there. ──

ROWS: tuple[Entry, ...] = (
    Entry(
        name="capture",
        runs=Runs.ROW,
        summary=(
            "Seals what the last few minutes produced into memory leaves, and "
            "recaps any conversation that has gone quiet."
        ),
        why=(
            "Turns and terminal work would wait for the nightly pass, anything "
            "the machine lost before it would never be captured at all, and a "
            "conversation held anywhere would leave nothing behind, so the next "
            "one would start from nothing."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.HOME,
    ),
    Entry(
        name="consolidate",
        runs=Runs.ROW,
        summary=(
            "Settles the day into the library: digest, distil, lint, scrub, "
            "re-index. It sweeps the agenda and the providers on the way."
        ),
        why=(
            "The library would grow by accretion: duplicates never merged, broken "
            "links never repaired, indexes drifting from the files they describe."
        ),
        kind=Kind.REMOTE_MODEL,
        # `chain_1` is what the two feedback stages ride. `DISPATCHED` arrived
        # with `provider_probe`: it calls each active role's PRIMARY REF
        # directly rather than riding a chain, so no literal list could name
        # what it spends on without going stale the next time a role moves.
        chains=("chain_1", DISPATCHED),
        owner=Owner.HOME,
    ),
    Entry(
        name="watchman",
        runs=Runs.ROW,
        summary="Reads what the runtime actually did and reports what broke.",
        why=(
            "Errors, restarts and stalled work would sit in log files nobody opens "
            "until something visible failed."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.RUNTIME,
    ),
    Entry(
        name="janitor_sweep",
        runs=Runs.ROW,
        summary="Reaps orphaned processes, scratch directories and dead controller sessions.",
        why=(
            "Killed runs leave behind processes holding ports and directories "
            "holding locks, and both outlive the thing that made them."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
    ),
)

# ── Services: loops that run for as long as the app does. ──
#
# Declared here rather than only in the code that starts them: an interval
# chosen in a constant and started in a `create_task` is invisible to every
# surface an operator can reach, which is how six of these came to exist
# without appearing in any registry.

SERVICES: tuple[Entry, ...] = (
    Entry(
        name="scheduler_tick",
        runs=Runs.SERVICE,
        summary="Wakes once a minute and fires whichever rows are due.",
        why="Nothing on a schedule would ever run. Every row above depends on it.",
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/scheduler/engine.py:_tick_loop",
        substrate="scheduler",
    ),
    Entry(
        name="alarm_tick",
        runs=Runs.SERVICE,
        summary="Checks the alarm registry every ten seconds and fires what is due.",
        why=(
            "An alarm you set would arrive up to a minute late, which for the one "
            "thing you asked to be reminded of is the whole value of it."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.DELIVERY,
        site="tesseract/scheduler/engine.py:_alarm_tick_loop",
        substrate="scheduler",
    ),
    Entry(
        name="autonomy_kernel",
        runs=Runs.SERVICE,
        summary=(
            "Drains the event bus, scores what it finds, dispatches workers, and "
            "calls out one whose heartbeat has gone quiet."
        ),
        why=(
            "Nothing the assistant decides to do for itself would ever be picked "
            "up, recovery after a crash would wait for a person, and a worker that "
            "died mid-task would stay listed as running."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=(DISPATCHED,),
        owner=Owner.HOME,
        site="tesseract/orchestrator/autonomy/kernel.py:_run_loop",
        substrate="autonomy_kernel",
    ),
    Entry(
        name="governor_detector",
        runs=Runs.SERVICE,
        summary="Watches the kernel's own behaviour and pauses a source that misbehaves.",
        why=(
            "A loop that produced the same work repeatedly would keep producing it, "
            "and the first sign would be the bill."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/orchestrator/autonomy/governor.py:_run_loop",
        substrate="autonomy_kernel",
    ),
    Entry(
        name="brief_delivery",
        runs=Runs.SERVICE,
        summary="Puts the morning brief in front of you at the hour you set.",
        why=(
            "The brief would be written every night and sit in a folder until "
            "you thought to go and look for it."
        ),
        # Free: the brief was already written and paid for as a stage of the
        # nightly row. This only carries it to the inbox and Telegram.
        kind=Kind.DETERMINISTIC,
        owner=Owner.DELIVERY,
        site="tesseract/mirror/server/brief_delivery.py:delivery_loop",
        substrate="brief_delivery",
    ),
    Entry(
        name="spawn_heartbeat",
        runs=Runs.SERVICE,
        summary=(
            "Looks at the background work that is still running, and starts a "
            "turn to say what has been going a long time with nothing back."
        ),
        why=(
            "Everything else about a background task speaks when it finishes. "
            "Work that simply keeps going would be noticed only when you "
            "thought to ask, which is how an eighteen minute run went unchecked."
        ),
        # It starts an ordinary turn on the chat that owns the work, so what it
        # spends is whatever that conversation was already spending.
        kind=Kind.REMOTE_MODEL,
        chains=(DISPATCHED,),
        owner=Owner.DELIVERY,
        site="tesseract/mirror/server/spawn_heartbeat.py:heartbeat_loop",
        substrate="spawn_heartbeat",
    ),
    Entry(
        name="workspace_reply_retry",
        runs=Runs.SERVICE,
        summary=(
            "Looks for a question you asked in a thread that has no answer, "
            "and asks the assistant again."
        ),
        why=(
            "A comment got one attempt when you posted it. If that attempt "
            "failed you were left with a question, no answer, and nothing "
            "anywhere saying so."
        ),
        # It dispatches the same controller session the first attempt did, so
        # a retry costs what the reply would have cost.
        kind=Kind.REMOTE_MODEL,
        chains=(DISPATCHED,),
        owner=Owner.DELIVERY,
        site="tesseract/mirror/server/workspace_reply_retry.py:retry_loop",
        substrate="workspace_reply_retry",
    ),
    Entry(
        name="liveness_feed",
        runs=Runs.SERVICE,
        summary=(
            "Tells an open Autonomy panel when something it is drawing has "
            "changed state, instead of the panel asking again."
        ),
        why=(
            "Every room on that panel polled, so a step taking its turn, or "
            "going quiet for longer than it declared, showed up whenever the "
            "room next asked. It also left the panel unable to tell nothing "
            "happening from not being told."
        ),
        # It reads files the routes already read and publishes what changed.
        # Nothing it does calls a model.
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/mirror/server/liveness_feed.py:feed_loop",
        substrate="liveness_feed",
    ),
    Entry(
        name="loop_lag_monitor",
        runs=Runs.SERVICE,
        summary="Samples the event loop and reports what blocked it when it stalls.",
        why=(
            "A blocked loop stops health checks, heartbeats and inbound turns at "
            "once, and without this the only symptom is an app that feels slow."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/mirror/server/app.py:_monitor_loop_lag",
    ),
    Entry(
        name="session_autosave",
        runs=Runs.SERVICE,
        summary="Writes an open session to disk on your autosave interval.",
        why=(
            "A crash or a power cut would take every turn since the session opened, "
            "and those turns are what the library is built from."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.HOME,
        site="tesseract/mirror/server/session_autosave.py:autosave_pump",
    ),
    Entry(
        name="mcp_session_sweep",
        runs=Runs.SERVICE,
        summary="Closes MCP sessions whose client vanished without saying so.",
        why=(
            "Every abandoned session would hold its stream and its state open until "
            "the backend restarted."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/mirror/server/mcp/server.py:_sweep_loop",
    ),
    Entry(
        name="mcp_client_supervisor",
        runs=Runs.SERVICE,
        summary="Keeps each configured outbound MCP server connected, reconnecting with backoff.",
        why=(
            "A server that dropped once would stay gone until the next restart, and "
            "its tools would simply be missing."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/mcp_client/manager.py:_serve",
        substrate="mcp_clients",
    ),
    Entry(
        name="entity_signals_pump",
        runs=Runs.SERVICE,
        summary="Pushes entity signals to an open session on a fixed interval.",
        why="The panels that watch live state would only update when you acted.",
        kind=Kind.DETERMINISTIC,
        owner=Owner.DELIVERY,
        site="tesseract/mirror/server/ws_connection.py:_entity_signals_pump",
    ),
    Entry(
        name="controller_heartbeat",
        runs=Runs.SERVICE,
        summary="Touches the agent controller's heartbeat file while the daemon lives.",
        why=(
            "Nothing could tell a controller that died from one that is merely idle, "
            "so a dead one would keep its lanes and never be replaced."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/orchestrator/agent_controller/lifecycle.py:_heartbeat_loop",
    ),
    Entry(
        name="pty_feed",
        runs=Runs.SERVICE,
        summary="Drains a terminal pane's output and, while observing, offers its lines to the observer.",
        why=(
            "Terminal work would reach neither the screen nor the assistant, and "
            "what you do in a shell is half of what there is to learn."
        ),
        # The drain itself is free; the branch that forwards a line to the
        # observer is a paid call, and an operator reading this list should see
        # that a terminal pane CAN spend. It only fires while observing and only
        # for a consented pane.
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.HOME,
        site="tesseract/mirror/server/pty_manager.py:_reader_loop",
    ),
    # The four below wait on IO rather than on a clock, so the loop scan
    # cannot see them. Each names the `boot.yaml` substrate that starts it,
    # which is what `check_substrates` holds them to instead.
    Entry(
        name="telegram_poll",
        runs=Runs.SERVICE,
        summary="Holds a long poll open to Telegram and hands each message to the session.",
        why=(
            "Messages sent to the assistant from a phone would arrive only when "
            "something else happened to ask."
        ),
        # The poll costs nothing. A message it delivers becomes an ordinary turn
        # on the chat path and bills there — the operator asked for that one.
        kind=Kind.DETERMINISTIC,
        owner=Owner.DELIVERY,
        site="tesseract/integrations/telegram/bridge.py:_poll_loop",
        substrate="telegram_bridge",
    ),
    Entry(
        name="observer_subscriber",
        runs=Runs.SERVICE,
        summary="Watches the turns of an attached session and proposes what to remember.",
        why=(
            "What is worth keeping would be decided only in the moment, by an "
            "assistant busy answering, and most of it would be lost."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.HOME,
        site="tesseract/brain/observer_subscriber.py:_run",
        substrate="observer",
    ),
    Entry(
        name="config_watcher",
        runs=Runs.SERVICE,
        summary="Watches the config directory and re-applies a file the moment it is saved.",
        why=(
            "Every settings change would need a restart, and a restart mid-work is "
            "how a setting stops being changed at all."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.RUNTIME,
        site="tesseract/mirror/server/config_watcher.py:start",
        substrate="config_watcher",
    ),
    Entry(
        name="workspace_watch",
        runs=Runs.SERVICE,
        summary="Pushes an inbox row to the open app the moment it is written or decided.",
        why=(
            "Half the things that write to the inbox never announced it and "
            "nothing outside the backend could, so a row appeared only when you "
            "pressed Refresh, and a decision taken on your phone left the "
            "window in front of you showing a question already answered."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.DELIVERY,
        site="tesseract/mirror/server/workspace_watch.py:sync",
        substrate="workspace_watch",
    ),
    Entry(
        name="activity_subscriber",
        runs=Runs.SERVICE,
        summary="Keeps one connection to the agent controller and mirrors its activity here.",
        why=(
            "Work running in the controller's own process would be invisible to "
            "every surface in the app."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.DELIVERY,
        site="tesseract/mirror/server/activity_subscriber.py:_run",
        substrate="activity_subscriber",
    ),
)

# A loop the scan finds that is not a service, and why. Every one of these
# belongs to one call, one connection or one boot — it ends when that ends.
# A loop with no entry and no line here fails
# `test_no_undeclared_loop.py`, which is what "nothing runs undeclared" means
# in practice.
EXEMPT_LOOPS: dict[str, str] = {
    "tesseract/integrations/telegram/bridge.py:_typing_keepalive":
        "holds the typing indicator for one outbound message",
    "tesseract/kernel/adapters/cli.py:_pump_codex_json":
        "drains one CLI subprocess for one call",
    "tesseract/kernel/tools/grep_tool.py:_watch_cancel":
        "watches for cancellation during one grep",
    "tesseract/kernel/tools/grep_tool.py:run":
        "one grep, polling its own subprocess",
    "tesseract/mcp_client/manager.py:_health_hold":
        "the keepalive inside one mcp_client_supervisor connection",
    "tesseract/memory/ollama_boot.py:_wait_for_ollama":
        "a bounded wait for the local model server at boot",
    "tesseract/mirror/server/mcp/stream.py:serve_activity_stream":
        "one SSE connection, for as long as the client holds it",
    "tesseract/mirror/server/pty_manager.py:wait_idle_for_pane":
        "a bounded wait for one pane to go quiet",
    "tesseract/orchestrator/agent_controller/dispatcher.py:ensure_daemon_running":
        "a bounded wait for the controller to come up",
    "tesseract/orchestrator/agent_controller/lanes/ipc_proxy.py:await_turn":
        "one turn on one lane",
    "tesseract/orchestrator/agent_controller/lanes/manager.py:await_turn":
        "one turn on one lane",
    "tesseract/orchestrator/agent_controller/reload_bridge.py:notify_controller_reload":
        "a bounded wait for the controller to acknowledge a config reload",
    "tesseract/orchestrator/agent_controller/transcript.py:tail":
        "follows one transcript for one reader",
    "tesseract/orchestrator/autonomy/kernel_worker_runner.py:_beat_until_done":
        "one worker's heartbeat, for as long as that worker runs",
    "tesseract/scheduler/pipeline/lock.py:hold":
        "a bounded wait for whatever is running the pipeline to finish",
    "tesseract/scripts/agent_cli.py:_shutdown_running_daemon":
        "one CLI command waiting for the daemon to exit",
}

# ── On demand: armed by the operator or the assistant. ──

ON_DEMAND: tuple[Entry, ...] = (
    Entry(
        name="scheduled_task",
        runs=Runs.ON_DEMAND,
        summary="Runs a recurring task you described, on the cadence you gave it.",
        why=(
            "Asking for something to happen every week would mean writing a job "
            "module for it, which puts it out of reach from a conversation."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.DELIVERY,
    ),
    Entry(
        name="provider_watch",
        runs=Runs.ROW,
        summary=(
            "Searches for what the model providers have changed lately and "
            "writes you a digest of it."
        ),
        why=(
            "A model retired, repriced or given a bigger context window is "
            "found the day something stops behaving, rather than the day it "
            "was announced."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.HOME,
    ),
    Entry(
        name="tool_call",
        runs=Runs.ON_DEMAND,
        summary="Runs one tool on the cadence you gave it, and sends you what it said.",
        why=(
            "Polling a mailbox or a feed meant writing a job that reimplemented "
            "the tool that already does it, which is a second copy of the work "
            "and a second copy of its permission story."
        ),
        # The job itself is a dispatch and costs nothing. What it costs is
        # whatever tool the row names, and one of the tools it can name is
        # `invoke_agent`, which starts a turn. Same reasoning as `pty_feed`:
        # an operator reading this list has to see that this row CAN spend,
        # and the row is the only place that says which tool it runs.
        kind=Kind.REMOTE_MODEL,
        chains=(DISPATCHED,),
        owner=Owner.DELIVERY,
    ),
)

# ── Triggers: a row whose firing rule is an event, not an hour. ──
#
# These are `schedule.yaml` rows like any other — the difference is that they
# declare `when:` where the others declare `cadence:`, and the condition named
# there decides. Both of the first two are the same argument: the work is worth
# a model call once enough has happened since the last one, and no hour can
# know that.

TRIGGERS: tuple[Entry, ...] = (
    Entry(
        name="playbook_extract",
        runs=Runs.TRIGGER,
        summary=(
            "Reads a task you accepted as done and writes the way it was done "
            "down as a playbook for next time, or adds the task to a playbook "
            "that already says how."
        ),
        why=(
            "A problem solved once is solved again from scratch. Without this, "
            "the record of how a task was done is read by nobody."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.HOME,
    ),
    Entry(
        name="skill_refinement",
        runs=Runs.TRIGGER,
        summary=(
            "Reads how your skills have been performing and offers a rewrite of "
            "one that keeps ending in errors or corrections. A playbook revision "
            "that did worse than the one before it is retired, and the earlier "
            "one is kept to return to."
        ),
        why=(
            "A skill that quietly misleads the assistant goes on misleading it. "
            "Without this, the usage log records that and nobody reads it, and a "
            "revision that made a playbook worse stays the one it reaches for."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.HOME,
    ),
    Entry(
        name="working_set_review",
        runs=Runs.TRIGGER,
        summary=(
            "Reads which tools and playbooks you actually use and offers to "
            "change what rides every turn: carry the ones it keeps looking up "
            "first, stop carrying the ones nothing has touched."
        ),
        why=(
            "What a turn carries is roughly half of what it costs before you "
            "have typed anything, and the two gauges that could say whether "
            "the list is right have never moved anything. It never edits the "
            "list. It shows the change and the use it was computed from, and "
            "you decide."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.HOME,
    ),
    Entry(
        name="runtime_tuning",
        runs=Runs.TRIGGER,
        summary=(
            "Reads what each part of the app actually spends and offers to "
            "change a daily spending limit that is stopping work, or one set "
            "so high it is not a limit."
        ),
        why=(
            "A limit is set once, on a guess, and then nothing ever looks at "
            "it again. Without this, work is turned away all week and the "
            "only sign is that something did not happen. It never changes a "
            "limit. It shows the change and the spending it was read from, "
            "and you decide."
        ),
        kind=Kind.DETERMINISTIC,
        owner=Owner.HOME,
    ),
    Entry(
        name="skill_suggest",
        runs=Runs.TRIGGER,
        summary=(
            "Reads across recent days of work and points out a task you keep "
            "repeating that no skill covers yet."
        ),
        why=(
            "The library only grows when somebody notices a repeated shape. It "
            "never drafts a skill. It says what it saw, and you decide."
        ),
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.HOME,
    ),
    Entry(
        name="provider_probe",
        runs=Runs.TRIGGER,
        summary=(
            "Checks every model you have configured, once real traffic has "
            "already fallen back from one of them."
        ),
        why=(
            "A key that expired at breakfast would go unnoticed until the "
            "nightly check, and every role would spend the day quietly running "
            "on its second choice."
        ),
        kind=Kind.REMOTE_MODEL,
        # It calls each active role's PRIMARY ref directly rather than riding a
        # chain, so no literal list could name what it spends on without going
        # stale the next time a role moves.
        chains=(DISPATCHED,),
        owner=Owner.RUNTIME,
    ),
    Entry(
        name="panel_writer",
        runs=Runs.TRIGGER,
        summary=(
            "Writes the one line each room on the Autonomy panel opens with, "
            "from the numbers that room already counts."
        ),
        why=(
            "A list of room names is a menu. A line each is the overview, and "
            "without it you have to open a room to learn whether anything in "
            "it needs you."
        ),
        fires="when you open the panel and a room's numbers have moved",
        kind=Kind.REMOTE_MODEL,
        chains=("chain_1",),
        owner=Owner.DELIVERY,
    ),
)

# ── Substrates that start nothing continuous. ──
#
# The service half of the manifest is held to a `site`, and four services wait
# on IO where no clock-scan reaches them, so they are held to the `boot.yaml`
# substrate that starts them instead. That check ran in one direction only: an
# entry naming a substrate the boot graph dropped was caught, and a substrate
# that quietly grew a loop was not. Which is the direction that matters, since
# six loops came to run without appearing in any registry and that is the
# defect this whole manifest exists to close.
#
# So every substrate is now accounted for: it carries an entry, or it is named
# here with what it does instead. A one-shot preparation is not a service, and
# saying so once is what makes the silence about the rest meaningful.
NOT_A_SERVICE: dict[str, str] = {
    "tls_trust_store": "loads the CA bundle once and returns",
    "ollama": "probes the local model server at boot; the server is not ours",
    "tool_registry": "builds the registry; what runs is a tool, on a turn",
    "cost_ledger": "opens the ledger; what spends is the work that bills to it",
    "recovery": "replays what the previous process left unfinished, then stops",
    "activity_rebuild": "seeds the activity registry from disk before the subscriber connects",
    "voice_runtime": "builds the lanes; a lane runs when something speaks",
    "chat_infra": "builds the chat stores; the surfaces that use them are entries",
    "command_registry": "registers the commands once",
    "voice_dependent_tools": "registers the tools that need the voice runtime, once",
    "outbound_notifier": "primes the shared sender; what it sends is the work that asked",
    "serial_warmups": "drains the GIL-bound warm-up queue and exits, re-armed on a voice reload",
    "operator_panes": "reopens the panes the previous process left, once",
    "cli_auth": "reads the CLI credentials once",
    "browser_provision": "fetches the browser binary when one is missing",
    "wake_spotter": "preloads the wake decoder into a cache",
}


ENTRIES: tuple[Entry, ...] = ROWS + SERVICES + TRIGGERS + ON_DEMAND


def _by_name() -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for entry in ENTRIES:
        if entry.name in out:
            raise ValueError(
                f"manifest: {entry.name!r} is declared twice — the name is the "
                "ledger's billing key and cannot mean two things"
            )
        out[entry.name] = entry
    return out


BY_NAME: dict[str, Entry] = _by_name()


def entry(name: str) -> Entry | None:
    return BY_NAME.get(name)


def entries_of(runs: Runs) -> tuple[Entry, ...]:
    return tuple(e for e in ENTRIES if e.runs is runs)


__all__ = [
    "BY_NAME",
    "ENTRIES",
    "NOT_A_SERVICE",
    "EXEMPT_LOOPS",
    "ON_DEMAND",
    "ROWS",
    "SERVICES",
    "TRIGGERS",
    "entries_of",
    "entry",
]
