"""Append JSONL entries to `tesseract/logs/sessions/YYYY-MM-DD.jsonl`.

Logs-stream twin of `daily_notes.append_section`. The four bookkeeping
writers (`[reflect]`, `[session_end]`, `[auto_compact]`, `[scheduler]`)
route here instead of `memory-store/daily/` so the librarian never sees
them.

"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_header(header: str) -> tuple[str | None, str]:
    """Split a `## [type] title` header into `(type, title)`.

    Strips the `##` prefix. Missing `[type]` → `(None, full_text)`.
    Empty token → `(None, full_text)`.
    """
    text = header.lstrip("#").strip()
    if not text.startswith("[") or "]" not in text:
        return None, text
    close = text.index("]")
    token = text[1:close].strip()
    title = text[close + 1 :].strip()
    return (token or None), title


def append_log_entry(
    *,
    header: str,
    body: str,
    log_dir: Path,
    date: datetime | None = None,
    idempotency_probe: str | None = None,
) -> bool:
    """Append one JSON line to `<log_dir>/YYYY-MM-DD.jsonl`.

    Line schema: ``{"ts", "type", "title", "body"}``. `type` parsed from
    the `[token]` prefix in `header`; `None` when missing.

    Returns True on write, False when `idempotency_probe` is found in any
    existing line's body for the target file. Never raises on ordinary
    filesystem misses — callers wrap in their own try/except.
    """
    when = date if date is not None else datetime.now(timezone.utc)
    target = log_dir / f"{when.strftime('%Y-%m-%d')}.jsonl"
    log_dir.mkdir(parents=True, exist_ok=True)

    if idempotency_probe is not None and target.exists():
        # Probe the serialized line as a whole — matches
        # `daily_notes.append_section` semantics (substring check against
        # file contents) so daily_writer's header-as-probe and the WS
        # hooks' body-as-probe both work.
        existing = target.read_text(encoding="utf-8")
        if idempotency_probe in existing:
            return False

    type_token, title = _parse_header(header)
    entry: dict[str, Any] = {
        "ts": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": type_token,
        "title": title,
        "body": body,
    }
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def resolve_runtime_subdir(
    app: Any | None,
    *parts: str,
    fallback_root: Path,
) -> Path:
    """Resolve a path under the runtime tree, following the project-wide
    "tests can pin store_dir at a tmp tree" contract.

    Priority:
      1. `app["memory_bundle"].store.store_dir.parent / *parts` so a test
         that sets `MemoryStore.store_dir = tmp_path/memory-store` gets
         co-located logs without monkey-patching every caller.
      2. `fallback_root / *parts` — callers usually pass `TESSERACT_DIR`.

    Examples:
      `resolve_runtime_subdir(app, "logs", "sessions", fallback_root=R)`
      `resolve_runtime_subdir(app, "logs", "schedule", fallback_root=R)`

    Phase 15X consolidates `_resolve_log_dir` (this module) and
    `daily_writer._resolve_schedule_log_dir` into this single helper.
    """
    if app is not None:
        bundle = app.get("memory_bundle") if hasattr(app, "get") else None
        store = getattr(bundle, "store", None) if bundle is not None else None
        store_dir = getattr(store, "store_dir", None) if store is not None else None
        if store_dir is not None:
            return Path(store_dir).parent.joinpath(*parts)
    return fallback_root.joinpath(*parts)


def _resolve_log_dir(app: Any | None, fallback_root: Path) -> Path:
    """Backwards-compatible alias for the sessions log dir.

    Existing callers (`daily_writer.run`, M1 stream-split tests) keep this
    name; new code should call `resolve_runtime_subdir(app, "logs",
    "sessions", fallback_root=...)` directly.
    """
    return resolve_runtime_subdir(app, "logs", "sessions", fallback_root=fallback_root)
