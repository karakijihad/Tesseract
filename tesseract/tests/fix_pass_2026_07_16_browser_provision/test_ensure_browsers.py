"""ensure_browsers is best-effort: success/failure/exception never leak,
and the real playwright CLI is never spawned (subprocess exec is faked)."""

import sys

from tesseract.orchestrator.browser import provision


class _FakeProc:
    def __init__(self, rc: int, out: bytes = b"ok"):
        self.returncode = rc
        self._out = out
        self.killed = False

    async def communicate(self):
        return self._out, b""

    def kill(self):
        self.killed = True


async def test_ensure_browsers_success(monkeypatch):
    calls = {}

    async def fake_exec(*argv, **kw):
        calls["argv"] = argv
        return _FakeProc(0)

    monkeypatch.setattr(provision.asyncio, "create_subprocess_exec", fake_exec)
    assert await provision.ensure_browsers() is True
    assert calls["argv"][0] == sys.executable
    assert ("-m", "playwright", "install", "chromium") == calls["argv"][1:]


async def test_ensure_browsers_nonzero_exit_returns_false(monkeypatch):
    async def fake_exec(*argv, **kw):
        return _FakeProc(1, b"download failed")

    monkeypatch.setattr(provision.asyncio, "create_subprocess_exec", fake_exec)
    assert await provision.ensure_browsers() is False


async def test_ensure_browsers_spawn_failure_returns_false(monkeypatch):
    async def fake_exec(*argv, **kw):
        raise FileNotFoundError("no interpreter")

    monkeypatch.setattr(provision.asyncio, "create_subprocess_exec", fake_exec)
    assert await provision.ensure_browsers() is False
