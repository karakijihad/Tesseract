"""``python -m tesseract.supervisor`` — entry point.

AU-1 Session 1 covers:
  (default)     run supervisor in foreground
  --status      report current backend state without starting
  --force       reserved for Session 2 (crash-storm bypass); currently
                logs a warning and proceeds normally

Reads the backend port from ``tesseract/config/mirror.yaml`` so the
heartbeat URL matches the configured listener.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from tesseract.config_seed import (
    ensure_agents_seeded,
    ensure_config_seeded,
    ensure_env_seeded,
    ensure_memory_store_seeded,
    ensure_tars_workshop_seeded,
    ensure_vault_seeded,
    ensure_workspace_seeded,
)
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.alarms import ensure_alarms_state_migrated
from tesseract.supervisor.breaker import CrashStormBreaker, crash_storm_path
from tesseract.supervisor.daemon import (
    Supervisor,
    clear_pid_file,
    write_pid_file,
)
from tesseract.supervisor.intent import runtime_dir


def _resolve_health_url() -> str:
    # Resolve at call time so a SUPERVISOR_HEALTH_URL env var (used by
    # tests against a tmp port) wins over the YAML default.
    override = os.environ.get("SUPERVISOR_HEALTH_URL")
    if override:
        return override
    from tesseract.mirror.server.config import load_server_config
    cfg = load_server_config()
    return f"http://127.0.0.1:{cfg.port}/api/health"


def _setup_logging(home: Path) -> None:
    log_dir = home.parent / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "supervisor.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Also echo to stderr so the operator's foreground terminal sees
    # what's happening without tailing the log file.
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(stderr)


def _status(home: Path) -> int:
    """Report whether a supervisor PID file is present and whether the
    process is alive. Does NOT spawn anything."""
    pid_file = runtime_dir(home) / "supervisor.pid"
    if not pid_file.exists():
        print("supervisor: not running (no pid file)")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        print(f"supervisor: pid file unreadable at {pid_file}")
        return 1
    alive = _pid_alive(pid)
    state = "alive" if alive else "stale (pid file present, process gone)"
    print(f"supervisor: {state} (pid={pid})")
    return 0 if alive else 1


def _pid_alive(pid: int) -> bool:
    """Routed through the canonical cross-platform probe in
    :mod:`tesseract.supervisor.process_probe`."""
    from tesseract.supervisor.process_probe import pid_alive
    return pid_alive(pid)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tesseract.supervisor",
        description="Out-of-process supervisor for the Mirror backend.",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Report current state without starting.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reserved for AU-1 S2 (crash-storm bypass).",
    )
    args = parser.parse_args(argv)

    home = TESSERACT_HOME

    if args.status:
        # Read-only path; don't open the log handler (would leave an
        # empty file behind on every probe).
        return _status(home)

    ensure_config_seeded()
    ensure_workspace_seeded()
    ensure_agents_seeded()
    ensure_env_seeded()
    ensure_memory_store_seeded()
    ensure_vault_seeded()
    ensure_tars_workshop_seeded()
    _setup_logging(home)
    # After logging is attached, not before — see the identical comment in
    # `mirror/server/__main__.py::main`.
    ensure_alarms_state_migrated()

    # Reap daemon/backend children leaked by a prior supervisor that was
    # hard-killed (skipping its finally-block teardown). Runs at the production
    # entry, before any child spawns, so every match is a genuine orphan —
    # leaving them alive lets stacked generations kill each other's healthy
    # backend/Vite (exit code=1, no traceback). Best-effort; never blocks boot.
    # Kept out of Supervisor.run() so unit tests that call run() directly don't
    # enumerate/kill real processes. Opt out with SUPERVISOR_DISABLE_REAP=1.
    try:
        from tesseract.supervisor.reap import reap_orphans
        reap_orphans()
    except Exception:  # noqa: BLE001
        logging.exception("supervisor: orphan reap raised — continuing boot")

    # Crash-storm gate. Marker present + no --force → refuse to start.
    # --force clears the marker (archived) and proceeds normally; the
    # next start with no marker behaves like a fresh boot.
    breaker = CrashStormBreaker(tesseract_home=home)
    if breaker.is_latched():
        if not args.force:
            print(
                f"supervisor: refusing to start — crash storm marker at {crash_storm_path(home)}.\n"
                f"  inspect with: type {crash_storm_path(home)}\n"
                f"  clear with:   python -m tesseract.scripts.clear_crash_storm\n"
                f"  or one-shot:  python -m tesseract.supervisor --force",
                file=sys.stderr,
            )
            return 2
        archive = breaker.clear()
        logging.warning("supervisor: --force cleared crash storm; archived to %s", archive)

    pid_file = write_pid_file(home)
    try:
        backend_cmd_env = os.environ.get("SUPERVISOR_BACKEND_CMD")
        backend_cmd: list[str] | None = None
        if backend_cmd_env:
            try:
                parsed = json.loads(backend_cmd_env)
                if isinstance(parsed, list) and all(isinstance(p, str) for p in parsed):
                    backend_cmd = parsed
                    logging.warning(
                        "supervisor: using SUPERVISOR_BACKEND_CMD override (%d args) — "
                        "this should only be set for smoke / debug runs",
                        len(parsed),
                    )
            except (json.JSONDecodeError, TypeError):
                logging.warning("supervisor: SUPERVISOR_BACKEND_CMD set but unparseable; ignoring")
        # Production launch — backend gets its own visible console
        # on Windows so the operator sees two windows side-by-side.
        # Suppressible via SUPERVISOR_HEADLESS=1 for headless / CI.
        # The smoke harness can opt back IN via SMOKE_FORCE_SEPARATE_CONSOLE=1
        # so the CREATE_NEW_CONSOLE production code path stays under coverage.
        separate_console = (
            sys.platform == "win32"
            and os.environ.get("SUPERVISOR_HEADLESS") != "1"
            and (
                backend_cmd is None
                or os.environ.get("SMOKE_FORCE_SEPARATE_CONSOLE") == "1"
            )
        )
        sup = Supervisor(
            tesseract_home=home,
            health_url=_resolve_health_url(),
            backend_cmd=backend_cmd,
            separate_console=separate_console,
            # 2026-05-24: controller daemon is part of the standard
            # supervised stack now so `tars` in any terminal attaches
            # to it without manual `python -m tesseract.scripts.tars_controller`.
            # `SUPERVISOR_DISABLE_CONTROLLER=1` opts out for hermetic
            # test runs that don't want the sibling process.
            controller_daemon_enabled=(
                os.environ.get("SUPERVISOR_DISABLE_CONTROLLER") != "1"
            ),
        )
        # Operator-requested 2026-05-18: also spawn Vite dev server as a
        # supervised child so `python -m tesseract.supervisor` brings up
        # the full stack (supervisor + Mirror backend + Vite) with one
        # command. Vite's lifecycle is tied to the supervisor — killed
        # via stop_vite() in the finally block, with port_cleanup as the
        # safety net. Fail-soft: missing pnpm / package.json logs and
        # continues; the backend works fine without Vite.
        from tesseract.supervisor.vite import start_vite, stop_vite
        repo_root = Path(__file__).resolve().parents[2]
        vite_proc = start_vite(repo_root)
        try:
            return sup.run()
        finally:
            stop_vite(vite_proc)
    finally:
        clear_pid_file(home)
        _ = pid_file  # silence unused-var lint; kept for symmetry
        # Operator-requested 2026-05-18: free the Mirror backend port
        # (8000) and the Vite dev-server port (1420) on supervisor exit
        # regardless of reason (operator_quit / crash-storm / max-respawns).
        # Without this, zombie listeners can persist across supervisor
        # sessions on Windows and the next start fails to bind.
        try:
            from tesseract.supervisor.port_cleanup import release_ports
            summary = release_ports()
            for port, killed in summary.items():
                if killed:
                    logging.info(
                        "supervisor: released port %d (killed pid(s) %s)",
                        port, ", ".join(str(p) for p in killed),
                    )
        except Exception:
            logging.exception("supervisor: port cleanup raised on exit")


if __name__ == "__main__":
    sys.exit(main())
