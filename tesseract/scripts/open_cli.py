"""`python -m tesseract.scripts.open_cli <target>` — the terminal's `open`.

A dedicated entry point rather than a flag on ``agent_cli``: that one is a
client for attaching to the controller daemon, and this neither needs nor wants
a daemon.

It builds a four-tool registry and goes through ``execute_tool`` like every
other caller, so the permission stack applies here exactly as it does to the assistant
and to MCP clients — a terminal invocation is not a way around ``os_launch``
being ASK. Resolution is the shared resolver, so behaviour cannot drift from
the other callers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.base import Tool, ToolContext
from tesseract.kernel.tools.open_target import OpenTool
from tesseract.kernel.tools.os_launch import OsLaunchTool
from tesseract.kernel.tools.os_open_url import OsOpenUrlTool
from tesseract.kernel.tools.surface_create import SurfaceCreateTool
from tesseract.paths import config_dir, home_dir
from tesseract.permissions.policy import load_permission_policy


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (OpenTool(), SurfaceCreateTool(), OsOpenUrlTool(), OsLaunchTool()):
        registry.register(tool)
    return registry


async def _prompt(tool: Tool, tool_input: object, context: ToolContext) -> bool:
    """The operator is at the terminal, so an ASK is a real question. Without
    this, `decide.evaluate` treats the call as unattended and denies it."""
    answer = await asyncio.to_thread(input, f"{tool.name}: allow? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


async def _run(target: str, view: str, intent: str, destination: str) -> int:
    registry = _registry()
    context = ToolContext(tool_registry_provider=lambda: registry, ask_fn=_prompt)

    try:
        policy = load_permission_policy(
            config_dir() / "permissions.yaml", workspace_root=str(home_dir())
        )
    except Exception as exc:  # noqa: BLE001 — a config problem is the answer
        print(f"could not load permissions: {exc}", file=sys.stderr)
        return 1

    result = await execute_tool(
        registry,
        "open",
        {
            "target": target,
            "view": view,
            "intent": intent,
            "destination": destination,
        },
        context,
        ask_fn=_prompt,
        policy=policy,
    )

    if result.is_error:
        print(result.output, file=sys.stderr)
        return 1
    print(result.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tesseract-open",
        description=(
            "Open anything: a URL, a file, a folder, an application, or a "
            "search phrase. Renders it in the cockpit when it can and opens it "
            "in the owning application when it can't."
        ),
    )
    parser.add_argument("target", help="what to open")
    parser.add_argument(
        "--view", default="orb", help="canvas view a cockpit card belongs to"
    )
    parser.add_argument(
        "--intent",
        default="auto",
        choices=("auto", "path", "url", "app", "search"),
        help=(
            "how to read the target. `auto` guesses, and an existing file wins "
            "over every other reading — pass `search` to look a phrase up even "
            "when something on disk shares its name"
        ),
    )
    parser.add_argument(
        "--os",
        dest="destination",
        action="store_const",
        const="os",
        default="auto",
        help="skip the cockpit and open it in the application that owns it",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(args.target, args.view, args.intent, args.destination)
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
