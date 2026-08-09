"""First-run trust prompt for the ``agent`` CLI.

The Claude / Codex CLIs ask once per workspace ("trust this directory?
[y/N]") and persist the answer so subsequent runs are silent. We do the
same so the ``agent`` UX matches operator expectations.

Storage: ``<TESSERACT_HOME>/trusted_dirs.json`` — a single JSON object
keyed by resolved-absolute-cwd. Value is the ISO timestamp the operator
trusted it. Workspace-private (gitignored under ``tesseract/logs/`` only
applies to logs; ``<TESSERACT_HOME>/`` is the operator's local state
tree and is already excluded from the repo).

API:

* :func:`is_trusted(cwd)` — has this directory been blessed before?
* :func:`mark_trusted(cwd)` — persist a positive answer.
* :func:`prompt_trust(cwd)` — synchronously prompt the operator;
  returns the bool answer AND persists the positive case so the
  caller never has to remember a two-step pattern.

The trust store is intentionally tiny: no expiry, no revocation
workflow. The operator can edit / delete ``trusted_dirs.json`` by
hand if they want to revoke. Keeping the surface small means there's
nothing to misconfigure.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


_TRUST_FILE_NAME = "trusted_dirs.json"


def _trust_file_path() -> Path:
    """Resolve ``<TESSERACT_HOME>/trusted_dirs.json`` at call time so a
    monkeypatched env or a reloaded ``tesseract.paths`` is honored.
    """
    from tesseract.paths import TESSERACT_HOME

    override = os.environ.get("TESSERACT_HOME")
    home = Path(override).resolve() if override else Path(TESSERACT_HOME)
    return home / _TRUST_FILE_NAME


def _normalize(cwd: Path | str) -> str:
    return str(Path(cwd).resolve())


def _load() -> dict[str, str]:
    path = _trust_file_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("trust: store unreadable at %s", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    # Coerce every value to str so corrupt entries don't blow up callers.
    return {str(k): str(v) for k, v in raw.items()}


def _save(payload: dict[str, str]) -> None:
    """Atomic write via tmp+rename. Reviewer Bug 4 alignment: random-hex
    tmp suffix (same pattern as ``sessions.py::_atomic_write_json``) so
    two concurrent writes against the same trust file don't race on a
    shared ``trusted_dirs.json.tmp`` before the ``os.replace``. This
    does NOT close the read-modify-write window — concurrent
    ``mark_trusted`` calls for DIFFERENT cwds can still lose updates;
    that needs a real lock and is a tracked follow-up. The atomic write
    guarantees no torn JSON file on disk, which is the minimum bar.
    """
    path = _trust_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def is_trusted(cwd: Path | str | None = None) -> bool:
    """Return ``True`` when ``cwd`` (default: current directory) has
    been previously marked trusted via :func:`mark_trusted`."""
    target = _normalize(cwd or Path.cwd())
    return target in _load()


def mark_trusted(cwd: Path | str | None = None) -> None:
    """Persist a positive trust decision for ``cwd``. Idempotent."""
    target = _normalize(cwd or Path.cwd())
    store = _load()
    store[target] = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    _save(store)


def revoke(cwd: Path | str) -> None:
    """Remove a directory from the trust store. Idempotent."""
    target = _normalize(cwd)
    store = _load()
    if target in store:
        store.pop(target)
        _save(store)


def prompt_trust(
    cwd: Path | str | None = None,
    *,
    prompt_fn: object | None = None,
) -> bool:
    """Prompt the operator y/N for ``cwd``, persist on yes, return bool.

    ``prompt_fn`` lets tests inject a stub (``lambda _: "y"``) so the
    function is unit-testable without touching real stdin. Production
    callers pass ``None`` and get :func:`input`.

    Accepts: ``y``, ``yes``, ``Y``, ``YES`` (case-insensitive) as
    positive. Anything else — including bare Enter — is a no.
    """
    target_path = Path(cwd) if cwd is not None else Path.cwd()
    target_str = _normalize(target_path)
    ask = prompt_fn if callable(prompt_fn) else input
    banner = (
        "\nTESSERACT hasn't been run from this directory before:\n"
        f"  {target_str}\n"
        "Trust this directory and let the assistant read/write files here? [y/N]: "
    )
    try:
        answer = ask(banner)  # type: ignore[misc]
    except (EOFError, KeyboardInterrupt):
        return False
    if not isinstance(answer, str):
        return False
    if answer.strip().lower() in ("y", "yes"):
        mark_trusted(target_path)
        return True
    return False


__all__ = [
    "is_trusted",
    "mark_trusted",
    "prompt_trust",
    "revoke",
]
