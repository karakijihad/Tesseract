import pytest
from tesseract.orchestrator.tars_controller.interactive.registry import (
    InteractiveSessionRegistry,
)

class _FakeSession:
    def __init__(self, handle, target):
        self.handle, self.target = handle, target
        self.closed = False
    async def close(self): self.closed = True

def test_add_get_list_remove():
    reg = InteractiveSessionRegistry()
    s = _FakeSession("h1", "claude")
    reg.add(s)
    assert reg.get("h1") is s
    assert [r.handle for r in reg.list()] == ["h1"]
    reg.remove("h1")
    assert reg.get("h1") is None

def test_mint_handle_unique():
    reg = InteractiveSessionRegistry()
    a = reg.mint_handle("claude")
    b = reg.mint_handle("claude")
    assert a != b
    assert a.startswith("claude-")

@pytest.mark.asyncio
async def test_close_all():
    reg = InteractiveSessionRegistry()
    s = _FakeSession("h1", "claude"); reg.add(s)
    await reg.close_all()
    assert s.closed is True
    assert reg.get("h1") is None
