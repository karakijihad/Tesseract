"""Coverage for ``tesseract.scripts.slash_dispatch.print_slash_help``."""

from __future__ import annotations

from pydantic import BaseModel

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.scripts.slash_dispatch import print_slash_help


class _Probe(BaseModel):
    text: str


class _Fake(Tool):
    default_posture = "auto"

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return "probe"

    @property
    def input_schema(self) -> type[BaseModel]:
        return _Probe

    async def run(self, tool_input, context):  # noqa: ANN001
        return ToolResult(output="ok")


def test_help_prints_every_tool_grouped(capsys):
    reg = ToolRegistry()
    reg.register(_Fake("session_save"))
    reg.register(_Fake("session_reset"))
    reg.register(_Fake("alarm_set"))
    reg.register(_Fake("memory_search"))
    reg.register(_Fake("vault_query"))

    print_slash_help(reg)
    out = capsys.readouterr().out
    for tool_name in ("session_save", "session_reset", "alarm_set", "memory_search", "vault_query"):
        assert f"/{tool_name}" in out
    # Categories surface as bracketed prefixes.
    assert "[session]" in out
    assert "[alarm]" in out
    assert "[memory]" in out
    assert "[vault]" in out


def test_help_shows_native_commands(capsys):
    reg = ToolRegistry()
    print_slash_help(reg)
    out = capsys.readouterr().out
    assert "/help" in out and "/exit" in out and "/resume" in out


def test_help_handles_empty_registry(capsys):
    reg = ToolRegistry()
    print_slash_help(reg)  # must not crash
    assert "operator slash commands" in capsys.readouterr().out
