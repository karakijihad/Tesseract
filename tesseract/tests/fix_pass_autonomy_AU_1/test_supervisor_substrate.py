"""AU-1 — supervisor substrate: operator_quit, crash, restart_upgrade.

Tests 1, 2, 3 from `Docs/Plan/autonomy/_shared/kill-switch-protocol.md
§Tests`. Drives the supervisor via a deterministic fake backend so the
spawn/wait/route loop is exercised without standing up the real Mirror.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from tesseract.supervisor.daemon import Supervisor


HERE = Path(__file__).parent
FAKE_BACKEND_PATH = HERE / "fake_backend.py"


def _make_supervisor(
    tmp_path: Path,
    *,
    mode: str,
    extra_env: dict[str, str] | None = None,
    max_respawns: int = 2,
) -> Supervisor:
    """Construct a Supervisor pointed at the fake backend with the
    requested mode. ``max_respawns`` defaults low so a buggy test
    can't loop the suite forever."""
    env = {
        "FAKE_BACKEND_HOME": str(tmp_path),
        "FAKE_BACKEND_MODE": mode,
    }
    if extra_env:
        env.update(extra_env)
    return Supervisor(
        tesseract_home=tmp_path,
        backend_cmd=[sys.executable, str(FAKE_BACKEND_PATH)],
        heartbeat_enabled=False,
        extra_env=env,
        max_respawns=max_respawns,
    )


def test_operator_quit_does_not_respawn(tmp_path: Path) -> None:
    """Test 1 (§Tests). Backend writes intent.json {operator_quit};
    supervisor must exit zero. No second spawn. No crash_storm.json."""
    sup = _make_supervisor(
        tmp_path, mode="operator_quit_then_done", max_respawns=5,
    )
    exit_code = sup.run()

    assert exit_code == 0
    # Marker contents prove only one spawn happened.
    marker = tmp_path / "second_spawn_marker.txt"
    assert marker.exists()
    assert marker.read_text(encoding="utf-8") == "first_spawn"
    # No crash storm latched.
    assert not (tmp_path / "runtime" / "crash_storm.json").exists()


def test_crash_triggers_respawn(tmp_path: Path) -> None:
    """Test 2 (§Tests). Backend exits with non-zero status and no
    intent file → supervisor classifies as crash → respawns. Cap the
    loop with max_respawns so the test terminates."""
    sup = _make_supervisor(
        tmp_path, mode="crash", max_respawns=2,
    )
    exit_code = sup.run()

    # Reached max_respawns → exit 1.
    assert exit_code == 1
    # Spawn ledger proves the respawn actually happened — two spawns,
    # not one-and-fail. Pre-fix Supervisor would have spawned only once
    # and either exited 0 (mis-routing crash as operator_quit) or hung.
    counter = (tmp_path / "spawn_counter.txt").read_text(encoding="utf-8").strip()
    assert counter == "2"


def test_restart_upgrade_carries_continuation(tmp_path: Path) -> None:
    """Test 3 (§Tests). Backend writes intent.json {restart_upgrade,
    continuation_id}; supervisor respawns with
    ``TESSERACT_RESUME_CONTINUATION=<id>`` in the env.

    Two-spawn assertion: the FAKE_BACKEND_RESUME_OUT file captures the
    env value on the SECOND spawn. The first spawn produced the intent;
    only the respawn should see the env var populated.
    """
    resume_out = tmp_path / "resume.txt"
    sup = _make_supervisor(
        tmp_path,
        mode="restart_upgrade",
        extra_env={
            "FAKE_CONTINUATION_ID": "ag-2026-05-17-test",
            "FAKE_BACKEND_RESUME_OUT": str(resume_out),
        },
        max_respawns=2,
    )
    exit_code = sup.run()

    # Two spawns expected. The supervisor passed TESSERACT_RESUME_CONTINUATION
    # only on the respawn — fake_backend records the env value per spawn so
    # we can assert spawn #1 had no env and spawn #2 had the continuation id.
    assert exit_code == 1  # capped by max_respawns
    spawn_1 = tmp_path / "resume.1.txt"
    spawn_2 = tmp_path / "resume.2.txt"
    assert spawn_1.read_text(encoding="utf-8") == ""
    assert spawn_2.read_text(encoding="utf-8") == "ag-2026-05-17-test"
    # Final rollup at the base path matches spawn #2.
    assert resume_out.read_text(encoding="utf-8") == "ag-2026-05-17-test"


def _free_port() -> int:
    """Bind to port 0 to let the OS pick a free port, then close.
    The fake backend rebinds immediately so the race window is small."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_heartbeat_kill_triggers_respawn(tmp_path: Path, monkeypatch) -> None:
    """End-to-end coverage for the supervisor's primary survival fix.

    The fake backend binds /api/health, answers OK twice, then goes
    silent. When the supervisor's heartbeat thread gives up and signals
    the backend, the fake writes intent=operator_quit on the way down
    (exactly as the real backend does — the OS signal is
    indistinguishable from operator Ctrl-C). Before the fix the
    supervisor honored that intent and exited zero. After the fix
    `heartbeat_killed=True` overrides the intent and routes the exit
    as `crash`, so the supervisor respawns.

    Asserts a second spawn happened, which is impossible without the
    override. Caps at max_respawns=2 so the test terminates.
    """
    # Shrink heartbeat windows so the test runs in seconds, not the
    # 30s production budget. _HEARTBEAT_INTERVAL_S is bounded below by
    # the 0.5s sleep slice in the loop, so 0.5 is the floor.
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_INTERVAL_S", 0.5,
    )
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_TIMEOUT_S", 2.0,
    )
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_MAX_FAILURES", 2,
    )
    # _GRACEFUL_STOP_GRACE_S default is 30s; shrink so a stuck fake
    # doesn't make the test linger.
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._GRACEFUL_STOP_GRACE_S", 5.0,
    )
    # _INTENT_FLUSH_GRACE_S fires twice (per spawn exit) at 2s default.
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._INTENT_FLUSH_GRACE_S", 0.1,
    )
    # First-crash backoff is 5s in production — shrink so the respawn
    # gap doesn't dominate the test.
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._CRASH_BACKOFF_S", (0.1, 0.5, 1.0, 2.0),
    )

    port = _free_port()
    sup = Supervisor(
        tesseract_home=tmp_path,
        backend_cmd=[sys.executable, str(FAKE_BACKEND_PATH)],
        heartbeat_enabled=True,
        health_url=f"http://127.0.0.1:{port}/api/health",
        extra_env={
            "FAKE_BACKEND_HOME": str(tmp_path),
            "FAKE_BACKEND_MODE": "heartbeat_then_silent",
            "FAKE_BACKEND_PORT": str(port),
            "FAKE_BACKEND_ANSWER_LIMIT": "2",
        },
        max_respawns=2,
    )
    exit_code = sup.run()

    # Hit max_respawns → exit 1. Without the fix, the supervisor would
    # honor the backend's operator_quit and exit 0 after the FIRST kill.
    assert exit_code == 1, (
        f"supervisor exited {exit_code} — heartbeat-kill was not "
        "overridden to crash, so no respawn happened"
    )
    counter = (tmp_path / "spawn_counter.txt").read_text(encoding="utf-8").strip()
    assert int(counter) >= 2, (
        f"expected ≥2 spawns (proves heartbeat-kill respawned), got {counter}"
    )


def test_heartbeat_soft_failure_records_without_respawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft heartbeat misses are diagnostic, not an immediate backend kill."""
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_INTERVAL_S", 0.5,
    )
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_TIMEOUT_S", 0.5,
    )
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_SOFT_FAILURES", 2,
    )
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._HEARTBEAT_MAX_FAILURES", 100,
    )
    monkeypatch.setattr(
        "tesseract.supervisor.daemon._INTENT_FLUSH_GRACE_S", 0.1,
    )

    port = _free_port()
    sup = Supervisor(
        tesseract_home=tmp_path,
        backend_cmd=[sys.executable, str(FAKE_BACKEND_PATH)],
        heartbeat_enabled=True,
        health_url=f"http://127.0.0.1:{port}/api/health",
        extra_env={
            "FAKE_BACKEND_HOME": str(tmp_path),
            "FAKE_BACKEND_MODE": "heartbeat_then_silent",
            "FAKE_BACKEND_PORT": str(port),
            "FAKE_BACKEND_ANSWER_LIMIT": "1",
        },
        max_respawns=2,
    )

    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(sup.run()), daemon=True)
    thread.start()

    incident_path = tmp_path / "logs" / "supervisor" / "heartbeat-incidents.jsonl"
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not incident_path.exists():
        time.sleep(0.05)

    assert incident_path.exists()
    incident = json.loads(incident_path.read_text(encoding="utf-8").splitlines()[-1])
    assert incident["last_probe"]["ok"] is False
    assert incident["last_probe"]["status"] == 503
    assert incident["stack_dump"]["request_path"]
    assert incident["stack_dump"]["output_path"]
    assert Path(incident["stack_dump"]["request_path"]).exists()
    assert (tmp_path / "spawn_counter.txt").read_text(encoding="utf-8") == "1"

    sup.request_stop(source="test")
    thread.join(timeout=10.0)
    assert not thread.is_alive()
    assert result == [0]
    assert (tmp_path / "spawn_counter.txt").read_text(encoding="utf-8") == "1"


def test_classify_decision_matrix() -> None:
    """Unit-level: ``Supervisor._classify`` must route correctly for
    every (intent, exit_code) combination the supervisor sees."""
    from tesseract.supervisor.intent import IntentFile, now_utc

    sup = Supervisor(
        tesseract_home=Path("."),
        backend_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        heartbeat_enabled=False,
    )
    base_ts = now_utc()

    # No intent → crash regardless of exit code.
    assert sup._classify(None, 0) == "crash"
    assert sup._classify(None, 1) == "crash"

    # operator_quit wins regardless of exit code.
    op_intent = IntentFile(intent="operator_quit", timestamp=base_ts, source="ui_button")
    assert sup._classify(op_intent, 0) == "operator_quit"
    assert sup._classify(op_intent, 137) == "operator_quit"

    # restart_upgrade routes to its own path.
    ru_intent = IntentFile(
        intent="restart_upgrade",
        timestamp=base_ts,
        source="upgrade_manager",
        continuation_id="ag-X",
    )
    assert sup._classify(ru_intent, 0) == "restart_upgrade"

    # Explicit crash intent is a crash.
    crash_intent = IntentFile(intent="crash", timestamp=base_ts, source="health_timeout")
    assert sup._classify(crash_intent, -11) == "crash"

    # heartbeat_killed=True overrides EVERY intent label — including
    # operator_quit, which the backend writes when it receives our own
    # termination signal (indistinguishable from operator Ctrl-C). Without
    # this override the supervisor would honor its own kill and exit zero.
    assert sup._classify(op_intent, 0, heartbeat_killed=True) == "crash"
    assert sup._classify(ru_intent, 0, heartbeat_killed=True) == "crash"
    assert sup._classify(None, 0, heartbeat_killed=True) == "crash"
    assert sup._classify(crash_intent, 0, heartbeat_killed=True) == "crash"
