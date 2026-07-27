"""AU-1 supervisor smoke — operator-attended end-to-end.

Drives the supervisor in a foreground subprocess, waits for the backend
to come up (poll /api/health on a non-default port), sends CTRL_BREAK_EVENT
to the supervisor (Windows-equivalent of operator Ctrl-C in terminal),
verifies clean shutdown, then reports findings.

Modes:
  default:           backend in same console as supervisor (test mode)
  --separate-console: backend in its own CREATE_NEW_CONSOLE window
                      (exercises the production code path used when
                      `tesseract-start.bat` runs without
                      SUPERVISOR_HEADLESS=1). Windows-only.

Operator-attended; not pytest-collected (under tests/smoke/).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("SMOKE_PORT", "8767"))
SUPERVISOR_BOOT_TIMEOUT_S = 60.0
HEALTH_POLL_INTERVAL_S = 1.0
GRACE_AFTER_BREAK_S = 30.0


def _poll_health() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2.0) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _wait_for_health(deadline: float) -> bool:
    while time.monotonic() < deadline:
        if _poll_health():
            return True
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


def _read_text(path: Path, default: str = "(missing)") -> str:
    if not path.exists():
        return default
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(read error: {exc})"


def main() -> int:
    parser = argparse.ArgumentParser(description="AU-1 supervisor smoke")
    parser.add_argument(
        "--separate-console", action="store_true",
        help=(
            "Spawn the backend with CREATE_NEW_CONSOLE so it lands in its "
            "own visible window — exercises the production path. Windows only."
        ),
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    tmp_home = Path(os.environ.get("SMOKE_TESSERACT_HOME") or (repo_root / ".smoke-au1"))
    tmp_home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["TESSERACT_HOME"] = str(tmp_home)
    env["SUPERVISOR_HEALTH_URL"] = f"http://127.0.0.1:{PORT}/api/health"
    env["SMOKE_PORT"] = str(PORT)
    # SMOKE_BACKEND_CMD lets the supervisor entry point pick up an
    # alternate backend so we don't collide with the operator's
    # production Mirror on port 8000.
    smoke_backend = Path(__file__).with_name("smoke_backend.py")
    env["SUPERVISOR_BACKEND_CMD"] = json.dumps([sys.executable, str(smoke_backend)])
    # Production CREATE_NEW_CONSOLE path is reachable from the smoke
    # via SMOKE_FORCE_SEPARATE_CONSOLE=1 — bypasses the __main__ guard
    # that normally disables it when SUPERVISOR_BACKEND_CMD is set.
    if args.separate_console:
        env["SMOKE_FORCE_SEPARATE_CONSOLE"] = "1"

    print(f"smoke: TESSERACT_HOME={tmp_home}")
    print(f"smoke: backend={smoke_backend.name} port={PORT}")
    print(f"smoke: separate_console={args.separate_console}")
    print(f"smoke: spawning supervisor ...")

    cmd = [sys.executable, "-m", "tesseract.supervisor"]
    kwargs: dict = {"env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
    start = time.monotonic()

    try:
        print(f"smoke: supervisor pid={proc.pid}; waiting up to {SUPERVISOR_BOOT_TIMEOUT_S}s for backend /api/health 200 ...")
        healthy = _wait_for_health(start + SUPERVISOR_BOOT_TIMEOUT_S)
        boot_elapsed = time.monotonic() - start

        if not healthy:
            print(f"smoke: FAILED — backend did not become healthy within {SUPERVISOR_BOOT_TIMEOUT_S}s")
            print(f"smoke: supervisor.log tail:")
            print(_read_text(tmp_home / "logs" / "supervisor.log"))
            try:
                proc.kill()
            except OSError:
                pass
            return 1

        print(f"smoke: backend healthy after {boot_elapsed:.1f}s")

        # Stay up briefly so heartbeat polling runs at least twice.
        print("smoke: holding for 12s to observe heartbeat polling ...")
        time.sleep(12.0)

        # Confirm still healthy (heartbeat is steady — no false crash routing).
        if not _poll_health():
            print("smoke: WARNING — health probe failed during steady-state window")

        # Send CTRL_BREAK_EVENT (Windows) / SIGTERM (POSIX) — the supervisor's
        # signal handler writes intent.json {operator_quit, supervisor_signal}
        # and propagates the stop to the backend.
        print("smoke: sending operator-stop signal to supervisor ...")
        if sys.platform == "win32":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            proc.send_signal(signal.SIGTERM)

        try:
            exit_code = proc.wait(timeout=GRACE_AFTER_BREAK_S)
        except subprocess.TimeoutExpired:
            print(f"smoke: FAILED — supervisor did not exit within {GRACE_AFTER_BREAK_S}s")
            proc.kill()
            return 2

        print(f"smoke: supervisor exited (code={exit_code})")
        intent_path = tmp_home / "runtime" / "intent.json"
        pid_path = tmp_home / "runtime" / "supervisor.pid"
        crash_path = tmp_home / "runtime" / "crash_storm.json"
        log_path = tmp_home / "logs" / "supervisor.log"

        print()
        print("=" * 70)
        print("SMOKE RESULTS")
        print("=" * 70)
        print(f"backend boot time:           {boot_elapsed:.1f}s")
        print(f"supervisor exit code:        {exit_code} (expected 0 = operator_quit honored)")
        print(f"intent.json present:         {intent_path.exists()} (cleared after route — may be absent)")
        if intent_path.exists():
            print(f"intent.json contents:")
            print(intent_path.read_text(encoding="utf-8"))
        print(f"supervisor.pid present:      {pid_path.exists()} (expected False after clean exit)")
        print(f"crash_storm.json present:    {crash_path.exists()} (expected False — no crash)")
        print()
        print(f"supervisor.log (last 60 lines):")
        log_text = _read_text(log_path)
        lines = log_text.splitlines()
        for ln in lines[-60:]:
            print(f"  {ln}")
        print()
        # Quick port-cleared check.
        port_clear = not _poll_health()
        print(f"port {PORT} cleared:           {port_clear} (expected True)")
        print()

        ok = (
            exit_code == 0
            and not pid_path.exists()
            and not crash_path.exists()
            and port_clear
        )
        print(f"OVERALL: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 3

    finally:
        if proc.poll() is None:
            print("smoke: cleanup — supervisor still running, killing")
            try:
                proc.kill()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
