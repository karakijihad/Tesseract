"""SC (cockpit) — boot-race guard for the chat session build.

The Mirror WS listener accepts connections before `_build_chat_infra` finishes
populating `app['adapter_entry']` (brain adapter build + manifest prompt
assembly take ~20-30s at boot). Before the guard, building a session in that
window raised `AttributeError: 'NoneType' object has no attribute
'tool_iteration_cap'` and crashed the WS handler. `_build_chat_session` now
raises the catchable `ChatInfraNotReady`; the WS handler closes with
TRY_AGAIN_LATER so the client reconnects once ready.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from tesseract.mirror.server.session import ChatInfraNotReady, _build_chat_session


def test_build_chat_session_raises_when_adapter_entry_is_none(monkeypatch, tmp_path):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    app = web.Application()
    app["prompt_builder"] = lambda: "sys"
    app["system_prompt"] = "sys"
    app["adapter_entry"] = None  # boot race: chat infra not built yet

    with pytest.raises(ChatInfraNotReady):
        _build_chat_session(app, "cockpit-1", None, None, None)


def test_chat_infra_not_ready_is_a_runtime_error():
    # The WS handler catches it specifically; keep it a RuntimeError subclass so
    # generic `except Exception` paths still treat it as transient.
    assert issubclass(ChatInfraNotReady, RuntimeError)
