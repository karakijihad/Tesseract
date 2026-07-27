"""Generate `Docs/Logs/CAPABILITIES.md` from the live tool registry.

Phase 18.5 W7-A (audit M1 follow-up, 2026-04-29). The audit's "tools.yaml
references file_edit whose source is gone" finding pointed at a stale
hand-maintained tool roster. This script replaces it: the registry is
the single source of truth and CAPABILITIES.md is regenerated on every
push so drift is impossible — CI fails if the rendered output no longer
matches what the registry produces.

Usage:
    python -m tesseract.scripts.generate_capability_matrix
    python -m tesseract.scripts.generate_capability_matrix --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tesseract.brain.boot import build_tool_registry
from tesseract.kernel.tools.base import PermissionResult
from tesseract.paths import ROOT

OUTPUT_PATH = ROOT / "Docs" / "Logs" / "CAPABILITIES.md"


def _safety_label(tool) -> str:
    if tool.is_concurrency_safe():
        return "parallel"
    return "serial"


def _read_only_label(tool) -> str:
    return "read" if tool.is_read_only() else "write"


def _permission_label(tool) -> str:
    """The `check_permissions` default for empty input. Best-effort —
    most tools branch on the input value, so this is the *baseline* the
    permissions stack starts from when the schema validates an empty
    instance. Tools that require non-empty input show 'see input'."""
    try:
        empty = tool.input_schema()
    except Exception:
        return "see input"
    try:
        result = tool.check_permissions(empty, _StubContext())
    except Exception:
        return "see input"
    if isinstance(result, PermissionResult):
        return result.name
    return str(result)


class _StubContext:
    """Minimal ToolContext stand-in so check_permissions can be probed
    without spinning the live runtime. Tools that touch real fields
    fall back to the 'see input' label via the except branch."""

    workspace_root = "/"
    current_call_id = ""
    session_id = ""
    cli_sink = None
    pty_dispatcher = None
    ask_fn = None


def _ensure_env_independent_tools(registry) -> None:
    """`invoke_agent` + `session_open` only register when a chat adapter
    resolves (i.e. some provider has credentials — see
    `brain/boot.py::build_tool_registry`). A no-credential CI run therefore
    renders two fewer tools than a dev checkout, so the committed matrix fails
    `--check` in CI. Register stub instances when absent so the matrix is
    deterministic across environments. Safe because every field the matrix
    renders (name, description, read/write, safety, permission label) is a
    constant on these tools — independent of the adapter/options/registry the
    stub passes None/placeholders for. Only used for rendering; never run."""
    from tesseract.kernel.adapters.base import AdapterOptions
    from tesseract.kernel.tools.invoke_agent import InvokeAgentTool
    from tesseract.kernel.tools.session_tools import SessionOpenTool

    if registry.get("invoke_agent") is None:
        registry.register(InvokeAgentTool(
            agents_dir=ROOT,
            adapter=None,
            options=AdapterOptions(),
            parent_registry=registry,
            max_tool_iterations=1,
            max_consecutive_adapter_errors=1,
        ))
    if registry.get("session_open") is None:
        registry.register(SessionOpenTool())


def render_matrix() -> str:
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry()
    _ensure_env_independent_tools(registry)
    schemas = sorted(registry.schemas_for_adapter(), key=lambda s: s["name"])

    lines: list[str] = [
        "# TESSERACT — Capability Matrix",
        "",
        "**Generated** by `tesseract/scripts/generate_capability_matrix.py`. Do not hand-edit.",
        "",
        "Single source of truth for the live tool surface. CI regenerates this",
        "on every push and fails if the file drifts from what the registry produces.",
        "",
        f"**Tools registered:** {len(schemas)}",
        "",
        "| Tool | Safety | Class | Default permission | Description |",
        "|------|--------|-------|--------------------|-------------|",
    ]
    for schema in schemas:
        tool = registry.get(schema["name"])
        if tool is None:
            continue
        desc = (tool.description or "").replace("\n", " ").strip()
        if len(desc) > 120:
            desc = desc[:117] + "..."
        lines.append(
            f"| `{tool.name}` | {_safety_label(tool)} | {_read_only_label(tool)} | "
            f"{_permission_label(tool)} | {desc} |"
        )

    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- **Safety**: `parallel` tools may run concurrently in one model turn "
        "(`is_concurrency_safe() is True`); `serial` tools run one at a time, "
        "in `pending_calls` order. See `tesseract/brain/chat.py` "
        "`_run_pending_calls` (audit M5)."
    )
    lines.append(
        "- **Class**: `read` tools are inert (filesystem reads, lookups, etc.); "
        "`write` tools mutate state — file system, mood, voice, scheduler, PTY, "
        "etc. Permission stack treats the two classes differently."
    )
    lines.append(
        "- **Default permission**: the `check_permissions` decision the tool "
        "returns for an empty input. Most tools branch on input, so this is the "
        "baseline before policy posture is consulted. `see input` means the tool "
        "requires non-empty input to evaluate (path-based tools, etc.)."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if CAPABILITIES.md is stale (for CI). Does not write.",
    )
    args = parser.parse_args()

    rendered = render_matrix()

    if args.check:
        if not OUTPUT_PATH.exists():
            print(f"[stale] {OUTPUT_PATH} does not exist", file=sys.stderr)
            return 1
        existing = OUTPUT_PATH.read_text(encoding="utf-8")
        if existing.strip() != rendered.strip():
            print(
                f"[stale] {OUTPUT_PATH} drifted from registry; "
                "run `python -m tesseract.scripts.generate_capability_matrix`",
                file=sys.stderr,
            )
            return 1
        print(f"[ok] {OUTPUT_PATH} matches the live registry")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"[wrote] {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
