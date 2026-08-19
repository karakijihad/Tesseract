"""Put a setting back to the value this release ships with.

Two copies of every config file, which is the whole mechanism. The factory
copy sits beside the code in the sealed `app/` tree (`TESSERACT_DIR/config`)
and is replaced wholesale by every update; the operator's lives under
`TESSERACT_HOME` (`config_dir()`) and is what a Settings pane writes into.
Reset reads the first and writes the second — the same pair
`config_seed.replace_config_from_templates` reads, and the same early return
when they are one file, which is what a dev checkout has.

**Scoped by key path, never by file.** `roles.yaml` alone carries the loop
caps, the compaction knobs, every role's budget, the voice lanes AND which
model each role points at. A pane that restored the file would revert four
things the operator never asked about, so each scope declares the keys its own
pane writes and reaches nothing else.

**Restores, never deletes.** A key the operator added that the factory copy has
no opinion about is left alone. So a role you invented keeps its budget when Cost is
reset, and a tool you registered keeps its posture when Tools is.

Nothing here syncs in-memory state: `mirror/server/config_watcher.py` watches
all four of these files and reloads on the write, exactly as it does for the
hand edit the panes tell you is equivalent.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.paths import TESSERACT_DIR, config_dir

#: What one pane's reset button restores: per config file, the key patterns
#: that pane's own writers touch. Every entry below is the read of a writer in
#: `mirror/server/routes/`, not a guess at what a screen looks like.
#:
#: `*` matches one level. Patterns are split on `.`, so a `*` is also the only
#: way to address a key that CONTAINS a dot — which the voice settings blocks
#: do, being keyed by catalog refs like `local.kokoro.af_heart`.
SCOPES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    # `routes/capabilities.py::capabilities_set_provider_enabled`. The tier and
    # service switches plus every provider under them. `cost_tracking` carries
    # an `enabled` of its own and is not a tier, so it is not reachable from
    # here; `channels` is credentials in another file, and switching one back
    # on is opening a bridge, not restoring a default.
    "capabilities": (
        (
            "providers.yaml",
            (
                "api.enabled", "api.*.enabled",
                "cli.enabled", "cli.*.enabled",
                "local.enabled", "local.*.enabled",
                "services.enabled", "services.*.enabled",
            ),
        ),
    ),
    # `routes/settings.py::set_session_policy` + `_apply_compaction_updates`.
    "session": (
        (
            "mirror.yaml",
            (
                "session.autosave",
                "session.autosave_interval_seconds",
                "session.resume_policy",
                "session.resume_days",
                "ui.show_config_reload_toasts",
            ),
        ),
        (
            "roles.yaml",
            (
                "roles.chat_brain.compact_threshold",
                "roles.chat_brain.keep_recent_turns",
            ),
        ),
    ),
    # `routes/settings.py::_apply_session_caps`.
    "loop_limits": (
        (
            "roles.yaml",
            (
                "roles.chat_brain.tool_iteration_cap",
                "roles.chat_brain.consecutive_error_cap",
            ),
        ),
    ),
    # `routes/settings.py::_apply_cost_update_*` + `_apply_voice_cost_*`. The
    # trailing `models.*.<rate>` is what pins the leading `*.*` to catalog
    # entries: no other top-level block in `providers.yaml` has that shape.
    "cost": (
        (
            "providers.yaml",
            (
                "cost_tracking.warning_at_pct",
                "*.*.models.*.cost_per_million_chars",
                "*.*.models.*.cost_per_audio_hour",
            ),
        ),
        (
            "roles.yaml",
            (
                "roles.*.daily_budget_usd",
                "voice.tts.settings.*.daily_budget_usd",
                "voice.stt.settings.*.daily_budget_usd",
            ),
        ),
    ),
    # `routes/settings.py::set_tool_permission`. The baseline posture only —
    # mode overrides and path overrides are separate blocks and no pane writes
    # them, so no reset reaches them either.
    "tools": (("permissions.yaml", ("tools.*",)),),
}


def _walk(doc: Any, parts: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Every `(key path, value)` in `doc` matching `parts`.

    A branch whose shape does not match simply yields nothing — which is what
    makes a leading `*` safe: `cost_tracking` is walked and discarded because
    it has no `models` beneath it.
    """
    if not parts or not isinstance(doc, dict):
        return
    head, rest = parts[0], parts[1:]
    names = list(doc) if head == "*" else ([head] if head in doc else [])
    for name in names:
        if not rest:
            yield (name,), doc[name]
            continue
        for sub_path, value in _walk(doc[name], rest):
            yield (name, *sub_path), value


def _held(doc: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    """`(reachable, current value)` for `path` in the operator's document."""
    for part in path:
        if not isinstance(doc, dict) or part not in doc:
            return False, None
        doc = doc[part]
    return True, doc


def _place(doc: Any, path: tuple[str, ...], value: Any) -> None:
    for part in path[:-1]:
        doc = doc[part]
    doc[path[-1]] = value


@dataclass(frozen=True)
class Change:
    """One setting put back, named by where it lives rather than by a label a
    caller would have to parse apart again."""

    file: str
    path: tuple[str, ...]
    value: Any

    def __str__(self) -> str:
        return f"{self.file}::{'.'.join(self.path)}"


def restore(scope: str) -> tuple[list[Change], list[str]]:
    """Restore one pane's settings to what this release ships with.

    Returns `(changed, missing)`. `missing` is a key the factory copy has and
    the operator's does not: a release that adds one delivers it at boot,
    not a reset's, and reporting it beats silently skipping it.

    Raises `KeyError` on an unknown scope and `OSError`/`yaml.YAMLError` when a
    factory file cannot be read — nothing is written if any file in the scope
    fails, so a reset never lands half-applied.
    """
    changed: list[Change] = []
    missing: list[str] = []
    pending: list[tuple[Path, list[Change]]] = []

    for filename, patterns in SCOPES[scope]:
        factory = TESSERACT_DIR / "config" / filename
        live = config_dir() / filename
        if not live.is_file() or factory.resolve() == live.resolve():
            # No operator copy, or a dev checkout where the two are one file.
            continue
        shipped = yaml.safe_load(factory.read_text(encoding="utf-8")) or {}
        current = yaml.safe_load(live.read_text(encoding="utf-8")) or {}
        wanted: dict[tuple[str, ...], Any] = {}
        for pattern in patterns:
            for path, value in _walk(shipped, tuple(pattern.split("."))):
                wanted[path] = value

        moved = []
        for path in sorted(wanted):
            reachable, held = _held(current, path)
            if not reachable:
                missing.append(f"{filename}::{'.'.join(path)}")
            elif held != wanted[path]:
                moved.append(Change(filename, path, wanted[path]))
        if moved:
            pending.append((live, moved))
            changed.extend(moved)

    # Written only after every file in the scope has parsed. Cost spans two
    # files, and half a reset is worse than none on a panel of budgets.
    for live, moved in pending:

        def _write(doc: Any, moved: list[Change] = moved) -> None:
            for change in moved:
                _place(doc, change.path, change.value)

        round_trip_yaml(live, _write)

    return changed, missing


__all__ = ["SCOPES", "Change", "restore"]
