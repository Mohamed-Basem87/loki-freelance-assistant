import asyncio
from types import SimpleNamespace

import app.message_processor as message_processor


def _event(message_id=123):
    return SimpleNamespace(
        id=message_id,
        chat_id=-100123,
        chat=SimpleNamespace(title="Test Channel"),
        raw_text="A valid job post",
        buttons=None,
    )


def _fake_job(source, text):
    return {
        "title": "Title",
        "description": "Description",
        "raw_text": text,
        "source": source,
        "url": "",
        "budget": "",
    }


def test_process_message_returns_true_after_successful_processing(monkeypatch):
    async def fake_process_job(**kwargs):
        return None

    monkeypatch.setattr(message_processor, "process_job", fake_process_job)
    monkeypatch.setattr(
        message_processor,
        "parse_job",
        lambda source, text: _fake_job(source, text),
    )

    assert asyncio.run(message_processor.process_message(_event())) is True


def test_process_message_returns_false_when_processing_fails(monkeypatch):
    async def fake_process_job(**kwargs):
        raise RuntimeError("temporary processing failure")

    async def fake_logger_run(*args, **kwargs):
        return None

    monkeypatch.setattr(message_processor, "process_job", fake_process_job)
    monkeypatch.setattr(
        message_processor,
        "parse_job",
        lambda source, text: _fake_job(source, text),
    )
    monkeypatch.setattr(message_processor.logger, "run", fake_logger_run)

    assert asyncio.run(message_processor.process_message(_event())) is False
