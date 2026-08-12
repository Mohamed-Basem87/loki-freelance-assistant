import asyncio
from types import SimpleNamespace

import app.handlers.telegram as telegram


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_messages(self, channel, min_id, reverse, limit):
        for message in self.messages:
            yield message


def test_recovery_does_not_advance_past_failed_message(monkeypatch):
    messages = [
        SimpleNamespace(id=101, chat_id=-1001),
        SimpleNamespace(id=102, chat_id=-1001),
        SimpleNamespace(id=103, chat_id=-1001),
    ]
    processed = []
    watermarks = []

    monkeypatch.setattr(telegram.state, "get_last_message_id", lambda _: 100)

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        processed.append(message.id)
        return message.id != 102

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    asyncio.run(telegram._recover_channel(_FakeClient(messages), "test-channel"))

    assert processed == [101, 102]
    assert watermarks == [(-1001, 101)]


def test_recovery_advances_through_successful_messages(monkeypatch):
    messages = [
        SimpleNamespace(id=201, chat_id=-1002),
        SimpleNamespace(id=202, chat_id=-1002),
    ]
    watermarks = []

    monkeypatch.setattr(telegram.state, "get_last_message_id", lambda _: 200)

    async def fake_set_watermark(chat_id, message_id):
        watermarks.append((chat_id, message_id))

    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set_watermark
    )

    async def fake_process_message(message):
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process_message)

    asyncio.run(telegram._recover_channel(_FakeClient(messages), "test-channel"))

    assert watermarks == [(-1002, 201), (-1002, 202)]
