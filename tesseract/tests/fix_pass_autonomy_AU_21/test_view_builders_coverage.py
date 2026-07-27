"""AU-21 — sanity check that ``viewSnapshot.ts`` is wired in App.tsx and
covers every active view in ``ui.ts::View``.

Pure source-text checks; the runtime contract is enforced server-side by
``operator_view.ALLOWED_VIEWS``.
"""

from __future__ import annotations

import re
from pathlib import Path

# __file__-anchored so the test passes from any cwd (repo root vs tesseract/).
_REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = _REPO_ROOT / "tesseract" / "mirror" / "src"


def _read(path: str) -> str:
    return (SRC / path).read_text(encoding="utf-8")


def test_allowed_views_match_ui_store() -> None:
    """ui.ts View union and operator_view.ALLOWED_VIEWS agree."""
    ui_src = _read("stores/ui.ts")
    match = re.search(r"export type View =\s*([^;]+);", ui_src, re.S)
    assert match
    views = set(re.findall(r"'([^']+)'", match.group(1)))

    from tesseract.mirror.server.routes.operator_view import ALLOWED_VIEWS
    assert views == set(ALLOWED_VIEWS), (
        f"View union vs ALLOWED_VIEWS drift: "
        f"ui-only={views - set(ALLOWED_VIEWS)} server-only={set(ALLOWED_VIEWS) - views}"
    )


def test_state_builders_cover_every_view() -> None:
    src = _read("lib/viewSnapshot.ts")
    ui_src = _read("stores/ui.ts")
    match = re.search(r"export type View =\s*([^;]+);", ui_src, re.S)
    assert match
    views = set(re.findall(r"'([^']+)'", match.group(1)))

    builders_match = re.search(
        r"_STATE_BUILDERS: Record<View, \(\) => Record<string, unknown>>\s*=\s*{(.*?)};",
        src,
        re.S,
    )
    assert builders_match, "missing _STATE_BUILDERS record literal"
    builder_keys = set(re.findall(r"(\w+):\s*_\w+State", builders_match.group(1)))
    assert builder_keys == views, (
        f"State-builder coverage drift: missing={views - builder_keys} "
        f"extra={builder_keys - views}"
    )


def test_emit_path_uses_redactSecrets() -> None:
    src = _read("lib/viewSnapshot.ts")
    assert "redactSecrets" in src
    assert "buildViewSnapshot" in src
    # emitViewSnapshot's debounced timeout body must call buildViewSnapshot
    # (which itself redacts). Find the function body and assert the call.
    emit_match = re.search(
        r"export function emitViewSnapshot\(\): void {(.*?)\n}\n",
        src,
        re.S,
    )
    assert emit_match, "emitViewSnapshot definition not found"
    assert "buildViewSnapshot" in emit_match.group(1)


def test_watcher_installed_in_app_tsx() -> None:
    src = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert "installViewSnapshotWatcher" in src


def test_ws_dispatch_routes_view_snapshot() -> None:
    src = (_REPO_ROOT / "tesseract" / "mirror" / "server" / "ws.py").read_text(encoding="utf-8")
    assert 'kind == "view_snapshot"' in src
    assert "handle_view_snapshot" in src
