"""``delete_session`` IPC + registry + ``tars --delete`` CLI flag.

Coverage:

* protocol — :class:`DeleteSessionMessage` parses via discriminator
  and rejects extras (forbid).
* :meth:`SessionRegistry.delete_session` — happy path + idempotent
  missing-record + missing-transcript.
* daemon ``_on_delete_session`` — broadcasts on success, refuses with
  ``session_attached`` when a writer is present, returns ack-style
  push only on the requester for solo delete.
* daemon ``_on_rename_session`` — happy path broadcasts.
* CLI parser — ``--delete <id>`` flag is wired and consumes its value.
* CLI offline fallback — ``_delete_session_offline`` removes the
  on-disk record + transcript when no daemon is running.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator.tars_controller.daemon import (
    ControllerDaemon,
    _ClientConn,
)
from tesseract.orchestrator.tars_controller.paths import (
    session_record_path,
    transcript_path,
)
from tesseract.orchestrator.tars_controller.protocol import (
    DeleteSessionMessage,
    RenameSessionMessage,
    parse_client_message,
)
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry


def _make_conn(writer_id: int = 1) -> _ClientConn:
    return _ClientConn(writer_id=writer_id, outbound=asyncio.Queue())


async def _drain(conn: _ClientConn) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not conn.outbound.empty():
        item = conn.outbound.get_nowait()
        if item is None:
            break
        out.append(item)
    return out


# ── protocol ────────────────────────────────────────────────────────────


def test_protocol_parses_delete_session(isolated_home: Path) -> None:
    msg = parse_client_message(
        {"msg": "delete_session", "session_id": "2026-05-24-deadbeef"}
    )
    assert isinstance(msg, DeleteSessionMessage)
    assert msg.session_id == "2026-05-24-deadbeef"


def test_protocol_parses_rename_session(isolated_home: Path) -> None:
    msg = parse_client_message(
        {
            "msg": "rename_session",
            "session_id": "2026-05-24-cafebabe",
            "title": "auth refactor",
        }
    )
    assert isinstance(msg, RenameSessionMessage)
    assert msg.title == "auth refactor"


def test_delete_session_rejects_unknown_fields(isolated_home: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeleteSessionMessage.model_validate(
            {"session_id": "2026-05-24-deadbeef", "force": True}
        )


# ── registry ────────────────────────────────────────────────────────────


def test_registry_delete_session_removes_record_and_transcript(
    isolated_home: Path,
) -> None:
    registry = SessionRegistry()
    record = registry.create_session(mode="chat", origin="cli")
    record_path = session_record_path(record.session_id)
    t_path = transcript_path(record.session_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    t_path.parent.mkdir(parents=True, exist_ok=True)
    t_path.write_text("{}\n", encoding="utf-8")

    assert record_path.exists()
    assert t_path.exists()

    existed = registry.delete_session(record.session_id)

    assert existed is True
    assert not record_path.exists()
    assert not t_path.exists()


def test_registry_delete_session_is_idempotent(isolated_home: Path) -> None:
    registry = SessionRegistry()
    record = registry.create_session(mode="chat", origin="cli")
    sid = record.session_id

    assert registry.delete_session(sid) is True
    assert registry.delete_session(sid) is False  # second call: no-op


def test_registry_delete_session_handles_missing_transcript(
    isolated_home: Path,
) -> None:
    registry = SessionRegistry()
    record = registry.create_session(mode="chat", origin="cli")
    # Never wrote a transcript file — delete must still succeed.
    assert not transcript_path(record.session_id).exists()

    assert registry.delete_session(record.session_id) is True
    assert not session_record_path(record.session_id).exists()


# ── daemon dispatch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_delete_session_broadcasts_and_unlinks(
    isolated_home: Path,
) -> None:
    daemon = ControllerDaemon(
        controller_id="ctrl-test-delete", token="t",
        registry=SessionRegistry(),
    )
    record = daemon._registry.create_session(mode="chat", origin="cli")
    sid = record.session_id

    # A second client is connected (no attach) — must see the broadcast.
    other = _make_conn(writer_id=2)
    daemon._clients[other.writer_id] = other
    requester = _make_conn(writer_id=1)
    daemon._clients[requester.writer_id] = requester

    await daemon._on_delete_session(
        requester, DeleteSessionMessage(session_id=sid)
    )

    assert not session_record_path(sid).exists()
    req_events = [p.get("event") for p in await _drain(requester)]
    other_events = [p.get("event") for p in await _drain(other)]
    assert "session_deleted" in req_events
    assert "session_deleted" in other_events


@pytest.mark.asyncio
async def test_on_delete_session_refuses_when_attached(
    isolated_home: Path,
) -> None:
    daemon = ControllerDaemon(
        controller_id="ctrl-test-delete-attached", token="t",
        registry=SessionRegistry(),
    )
    record = daemon._registry.create_session(mode="chat", origin="cli")
    sid = record.session_id
    # Pretend a writer is attached.
    daemon._sessions_attached[sid] = {99}

    conn = _make_conn()
    daemon._clients[conn.writer_id] = conn
    await daemon._on_delete_session(
        conn, DeleteSessionMessage(session_id=sid)
    )

    pushes = await _drain(conn)
    err = next((p for p in pushes if p.get("event") == "error"), None)
    assert err is not None, pushes
    assert err["code"] == "session_attached"
    # And the record is still on disk.
    assert session_record_path(sid).exists()


@pytest.mark.asyncio
async def test_on_delete_session_missing_id_is_silent_success(
    isolated_home: Path,
) -> None:
    daemon = ControllerDaemon(
        controller_id="ctrl-test-delete-missing", token="t",
        registry=SessionRegistry(),
    )
    sid = "2026-05-24-deadbeef"
    conn = _make_conn()
    daemon._clients[conn.writer_id] = conn

    await daemon._on_delete_session(
        conn, DeleteSessionMessage(session_id=sid)
    )

    pushes = await _drain(conn)
    # Idempotent — the daemon still broadcasts deletion so any picker
    # caching a stale id refreshes.
    deleted = next(
        (p for p in pushes if p.get("event") == "session_deleted"), None
    )
    assert deleted is not None, pushes
    assert deleted["session_id"] == sid


@pytest.mark.asyncio
async def test_on_rename_session_updates_and_broadcasts(
    isolated_home: Path,
) -> None:
    daemon = ControllerDaemon(
        controller_id="ctrl-test-rename", token="t",
        registry=SessionRegistry(),
    )
    record = daemon._registry.create_session(mode="chat", origin="cli")
    sid = record.session_id

    other = _make_conn(writer_id=2)
    daemon._clients[other.writer_id] = other
    requester = _make_conn(writer_id=1)
    daemon._clients[requester.writer_id] = requester

    await daemon._on_rename_session(
        requester, RenameSessionMessage(session_id=sid, title="renamed!")
    )

    on_disk = json.loads(session_record_path(sid).read_text(encoding="utf-8"))
    assert on_disk["title"] == "renamed!"

    req_evts = [p for p in await _drain(requester) if p.get("event") == "session_renamed"]
    other_evts = [p for p in await _drain(other) if p.get("event") == "session_renamed"]
    assert req_evts and req_evts[0]["title"] == "renamed!"
    assert other_evts and other_evts[0]["title"] == "renamed!"


@pytest.mark.asyncio
async def test_on_rename_missing_session_emits_error(
    isolated_home: Path,
) -> None:
    daemon = ControllerDaemon(
        controller_id="ctrl-test-rename-missing", token="t",
        registry=SessionRegistry(),
    )
    conn = _make_conn()
    daemon._clients[conn.writer_id] = conn

    await daemon._on_rename_session(
        conn,
        RenameSessionMessage(
            session_id="2026-05-24-deadbeef", title="ignored"
        ),
    )
    pushes = await _drain(conn)
    err = next((p for p in pushes if p.get("event") == "error"), None)
    assert err is not None, pushes
    assert err["code"] == "session_not_found"


# ── CLI ────────────────────────────────────────────────────────────────


def test_cli_parser_has_delete_flag(isolated_home: Path) -> None:
    from tesseract.scripts.tars_cli import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["--delete", "2026-05-24-deadbeef"])
    assert ns.delete == "2026-05-24-deadbeef"
    ns = parser.parse_args([])
    assert ns.delete is None


def test_cli_offline_delete_removes_record_when_no_daemon(
    isolated_home: Path, capsys: pytest.CaptureFixture,
) -> None:
    from tesseract.scripts.tars_cli import _delete_session_offline

    registry = SessionRegistry()
    record = registry.create_session(mode="chat", origin="cli")
    sid = record.session_id
    assert session_record_path(sid).exists()

    rc = _delete_session_offline(sid)
    assert rc == 0
    assert not session_record_path(sid).exists()
    out = capsys.readouterr().out
    assert sid in out


def test_cli_offline_delete_missing_session_returns_1(
    isolated_home: Path, capsys: pytest.CaptureFixture,
) -> None:
    from tesseract.scripts.tars_cli import _delete_session_offline

    rc = _delete_session_offline("2026-05-24-deadbeef")
    assert rc == 1
    err = capsys.readouterr().err
    assert "no such session" in err


def test_cli_offline_delete_bad_id_returns_2(
    isolated_home: Path, capsys: pytest.CaptureFixture,
) -> None:
    from tesseract.scripts.tars_cli import _delete_session_offline

    rc = _delete_session_offline("not-a-real-id")
    assert rc == 2
    err = capsys.readouterr().err
    assert "delete failed" in err
