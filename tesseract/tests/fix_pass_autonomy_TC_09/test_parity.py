"""TC-9 — parity test suite.

Six end-to-end scenarios cited by ``phase-TC-9-migration-retire.md §7``
and ``README.md §Minimum acceptance criteria``. Each test is an
integration assertion that the TC-1..TC-8 substrate, wired together,
delivers the operator-visible behavior the retirement decision depends
on. If any scenario fails, TC-9 is BLOCKED and deletion of any backend
PTY lifecycle code is forbidden.

Scenarios:

2. Headless session → ``tars`` client attaches later → transcript replay
   delivers every event that landed before the attach.
3. Mirror (observer) + CLI (interactive) see the same typed transcript
   for one session.
4. Scheduler/autonomy-mode session is reachable from ``list_sessions``
   and attachable by a CLI client.
5. Accepted advisor output (long, actionable summary) produces a linked
   ``follow_up_draft`` row in the operator journal + an
   ``AWAITING_OPERATOR`` agenda item with worker linkage.
6. Failure modes: bad auth token closes the connection; stale heartbeat
   refuses controller reattach.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.autonomy import journal as operator_journal
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.follow_up_mapper import FollowUpMapper
from tesseract.orchestrator.autonomy.models import AgendaSource, AgendaStatus
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.tars_controller import (
    ControllerDaemon,
    SessionRegistry,
    TranscriptReader,
    auth,
)
from tesseract.orchestrator.tars_controller import paths as tc_paths
from tesseract.orchestrator.tars_controller.events import AssistantTextEvent
from tesseract.orchestrator.workers.heartbeat import STALENESS_THRESHOLD_SECONDS
from tesseract.orchestrator.workers.record import (
    RiskClass,
    WorkerRecord,
    WorkerStatus,
)


CONTROLLER_ID = "ctrl-tc9-parity"



# ── IPC helpers (mirror TC-4 test_daemon_ipc.py) ──────────────────────


async def _open_client(daemon: ControllerDaemon) -> tuple[
    asyncio.StreamReader, asyncio.StreamWriter
]:
    host, port = daemon.address
    return await asyncio.open_connection(host, port)


async def _send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()


async def _recv(reader: asyncio.StreamReader, timeout: float = 5.0) -> dict[str, Any] | None:
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not line:
        return None
    return json.loads(line.decode("utf-8"))


@pytest.fixture
async def running_daemon(isolated_home: Path):
    token = auth.mint_token()
    daemon = ControllerDaemon(
        controller_id=CONTROLLER_ID,
        token=token,
        registry=SessionRegistry(),
        heartbeat_interval=3600,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        yield daemon, token
    finally:
        await daemon.stop()


# ── Scenario 2 — headless session, late attach replays transcript ─────


@pytest.mark.asyncio
async def test_parity_2_headless_then_attach_replays_full_transcript(
    running_daemon,
) -> None:
    """A session created without an interactive client (autonomy origin)
    accumulates transcript events via ``daemon.append_event``; a later
    ``tars`` attach must receive every event in the replay."""
    daemon, token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="autonomy", origin="autonomy", controller_id=CONTROLLER_ID
    ).session_id

    # Drop three events server-side BEFORE any client attaches.
    for text in ("step 1 done", "step 2 done", "step 3 done"):
        await daemon.append_event(
            sid, AssistantTextEvent(session_id=sid, origin="autonomy", text=text)
        )

    # Late attach: should see all three in the replay payload.
    reader, writer = await _open_client(daemon)
    try:
        await _send(writer, {"auth": token})
        await _send(
            writer,
            {"msg": "attach", "session_id": sid, "mode": "interactive"},
        )
        attached = await _recv(reader)
        assert attached is not None and attached["event"] == "attached"
        replay = attached["replay_events"]
        texts = [ev.get("text") for ev in replay if ev.get("kind") == "assistant_text"]
        assert texts == ["step 1 done", "step 2 done", "step 3 done"], texts
    finally:
        writer.close()
        await writer.wait_closed()


# ── Scenario 3 — observer + interactive see same transcript ───────────


@pytest.mark.asyncio
async def test_parity_3_observer_and_interactive_see_same_events(
    running_daemon,
) -> None:
    """Mirror attaches as observer; CLI attaches as interactive. A single
    ``append_event`` must fan out to both clients with the same event
    payload (this is the "Mirror and CLI show the same typed transcript"
    minimum-acceptance criterion)."""
    daemon, token = running_daemon

    # Interactive client creates the session.
    cli_reader, cli_writer = await _open_client(daemon)
    obs_reader, obs_writer = await _open_client(daemon)
    try:
        await _send(cli_writer, {"auth": token})
        await _send(
            cli_writer,
            {"msg": "new_session", "title": "parity", "mode": "chat", "origin": "cli"},
        )
        cli_attached = await _recv(cli_reader)
        sid = cli_attached["session"]["session_id"]

        # Observer attaches to the same session.
        await _send(obs_writer, {"auth": token})
        await _send(
            obs_writer,
            {"msg": "attach", "session_id": sid, "mode": "observer"},
        )
        await _recv(obs_reader)  # "attached" + replay (empty)

        # Server-side event — both clients must receive an identical push.
        await daemon.append_event(
            sid,
            AssistantTextEvent(
                session_id=sid, origin="chat", text="hello from server"
            ),
        )

        cli_push = await _recv(cli_reader)
        obs_push = await _recv(obs_reader)
        assert cli_push["event"] == "transcript_event"
        assert obs_push["event"] == "transcript_event"
        assert cli_push["transcript_event"] == obs_push["transcript_event"]
        assert cli_push["transcript_event"]["text"] == "hello from server"
    finally:
        cli_writer.close()
        await cli_writer.wait_closed()
        obs_writer.close()
        await obs_writer.wait_closed()


# ── Scenario 4 — scheduler/autonomy session listable + attachable ─────


@pytest.mark.asyncio
async def test_parity_4_scheduler_session_listable_and_attachable(
    running_daemon,
) -> None:
    """A scheduler-triggered session (no terminal at start) must show up
    in ``list_sessions`` and be attachable by a CLI client."""
    daemon, token = running_daemon
    sid = daemon._registry.create_session(  # noqa: SLF001
        mode="scheduler",
        origin="scheduler",
        controller_id=CONTROLLER_ID,
        title="nightly-eval",
    ).session_id

    reader, writer = await _open_client(daemon)
    try:
        await _send(writer, {"auth": token})

        # list_sessions returns the scheduler row.
        await _send(writer, {"msg": "list_sessions"})
        listing = await _recv(reader)
        assert listing["event"] == "session_list"
        modes = {s["mode"]: s["session_id"] for s in listing["sessions"]}
        assert "scheduler" in modes
        assert modes["scheduler"] == sid

        # Attach succeeds and returns the right session record.
        await _send(
            writer,
            {"msg": "attach", "session_id": sid, "mode": "interactive"},
        )
        attached = await _recv(reader)
        assert attached["event"] == "attached"
        assert attached["session"]["title"] == "nightly-eval"
        assert attached["session"]["mode"] == "scheduler"
    finally:
        writer.close()
        await writer.wait_closed()


# ── Scenario 5 — accepted advisor → follow-up draft + journal row ─────


@pytest.mark.asyncio
async def test_parity_5_accepted_advisor_creates_followup_and_journal_row(
    isolated_home: Path,
) -> None:
    """An advisor worker that completes DONE with a long, actionable
    summary and no diff artifacts must produce: (a) one new
    ``follow_up_draft`` journal row, and (b) one ``AWAITING_OPERATOR``
    agenda item linked back to the parent worker."""
    store = AgendaStore()
    mapper = FollowUpMapper(store)
    summary = (
        "We should implement the missing follow-up dispatcher so accepted "
        "advisor output flows automatically into a code-edit worker. Without "
        "this, the operator must retype the bridging directive every time."
    ) * 2
    now = datetime.now(timezone.utc)
    parent = WorkerRecord(
        id="wk-tc9-advisor",
        kind=WorkerKind.CLAUDE_CLI,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-tc9-parent",
        risk_class=RiskClass.OPERATOR_GATE,
        role="advisor",
        prompt="parent advisor prompt",
        status=WorkerStatus.DONE,
        summary=summary,
        artifacts=[],
    )

    draft = mapper.create_draft_if_actionable(parent)
    assert draft is not None, "actionable summary should produce a draft"
    assert draft.status is AgendaStatus.AWAITING_OPERATOR
    assert draft.source is AgendaSource.SELF_REFLECTION
    assert "wk-tc9-advisor" in draft.linked_workers
    assert draft.risk_class is RiskClass.OPERATOR_GATE
    assert len(draft.approvals_required) == 1
    assert draft.approvals_required[0].kind == "operator_review"

    rows = list(operator_journal.read_recent(limit=20))
    follow_ups = [r for r in rows if r["event_type"] == "follow_up_draft"]
    assert follow_ups, "no follow_up_draft journal row written"
    latest = follow_ups[-1]
    assert latest["worker_id"] == "wk-tc9-advisor"
    assert latest["follow_up_draft_id"] == draft.id


# ── Scenario 6 — failure-mode parity ──────────────────────────────────


@pytest.mark.asyncio
async def test_parity_6a_bad_token_closes_connection(running_daemon) -> None:
    """Token auth failure: daemon returns ``auth_failed`` and closes the
    socket."""
    daemon, _token = running_daemon
    reader, writer = await _open_client(daemon)
    try:
        await _send(writer, {"auth": "not-the-token"})
        reply = await _recv(reader)
        assert reply is not None
        assert reply["event"] == "error"
        assert reply["code"] == "auth_failed"
        # Connection closed by the daemon → next read is EOF.
        eof = await reader.read()
        assert eof == b""
    finally:
        writer.close()
        await writer.wait_closed()


def test_parity_6b_stale_heartbeat_refuses_controller_reattach(
    isolated_home: Path,
) -> None:
    """``TarsControllerRecoveryHandler.can_recover`` returns False when
    the controller heartbeat is older than the staleness threshold."""
    from tesseract.orchestrator.tars_controller import TarsControllerRecoveryHandler

    hb = tc_paths.heartbeat_path(CONTROLLER_ID)
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.touch()
    stale_ts = time.time() - (STALENESS_THRESHOLD_SECONDS + 30)
    os.utime(hb, (stale_ts, stale_ts))

    now = datetime.now(timezone.utc)
    record = WorkerRecord(
        id="wk-tc9-stale",
        kind=WorkerKind.TARS_CONTROLLER,
        created_at=now,
        updated_at=now,
        agenda_item_id="ag-tc9-stale",
        risk_class=RiskClass.OPERATOR_GATE,
        role="",
        status=WorkerStatus.RUNNING,
        controller_id=CONTROLLER_ID,
        controller_pid=os.getpid(),
        controller_hb_path=str(hb),
        session_id="2026-05-23-tc9stale",
    )
    handler = TarsControllerRecoveryHandler()
    assert handler.can_recover(record) is False


