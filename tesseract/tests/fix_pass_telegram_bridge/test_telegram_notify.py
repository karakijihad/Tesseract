from __future__ import annotations

from types import SimpleNamespace

import pytest

from tesseract.scheduler.tasks.telegram_notify import TelegramNotifyJob


@pytest.mark.asyncio
async def test_telegram_notify_job_uses_config_chat_id(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeAPI:
        def __init__(self, token: str) -> None:
            assert token == "secret-token"

        async def send_message(self, *, chat_id: int, text: str, parse_mode=None):
            calls.append(
                {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
            )
            return {"message_id": 321}

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.telegram_notify.TelegramAPI",
        _FakeAPI,
    )

    ctx = SimpleNamespace(
        job_name="telegram_ping",
        run_id="run-1",
        config={"chat_id": 1234, "text": "hi there", "parse_mode": "HTML"},
    )
    result = await TelegramNotifyJob().run(ctx)

    assert result.ok is True
    assert result.payload == {"chat_id": 1234, "message_id": 321}
    assert calls == [{"chat_id": 1234, "text": "hi there", "parse_mode": "HTML"}]


@pytest.mark.asyncio
async def test_telegram_notify_job_falls_back_to_env_chat_id(monkeypatch) -> None:
    class _FakeAPI:
        def __init__(self, token: str) -> None:
            assert token == "secret-token"

        async def send_message(self, *, chat_id: int, text: str, parse_mode=None):
            assert chat_id == 5678
            assert text == "scheduled"
            assert parse_mode is None
            return {"message_id": 999}

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_DEFAULT_CHAT_ID", "5678")
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.telegram_notify.TelegramAPI",
        _FakeAPI,
    )

    ctx = SimpleNamespace(
        job_name="telegram_ping",
        run_id="run-1",
        config={"text": "scheduled"},
    )
    result = await TelegramNotifyJob().run(ctx)

    assert result.ok is True
    assert result.payload == {"chat_id": 5678, "message_id": 999}
