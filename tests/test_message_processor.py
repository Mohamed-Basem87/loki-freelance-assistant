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

    async def fake_logger_log_error(*args, **kwargs):
        return None

    monkeypatch.setattr(message_processor, "process_job", fake_process_job)
    monkeypatch.setattr(
        message_processor,
        "parse_job",
        lambda source, text: _fake_job(source, text),
    )
    monkeypatch.setattr(message_processor.logger, "log_error", fake_logger_log_error)

    assert asyncio.run(message_processor.process_message(_event())) is False


def test_process_message_does_not_log_classification_pending_as_error(monkeypatch):
    """P3-B: ClassificationPendingError is expected control-flow (the job
    is durably pending/claimed on another path), not an application
    error. It must still return False (callers must not advance the
    watermark/barrier), but it must not be recorded as a generic
    'Message Processor' system error."""
    from app.job_processor import ClassificationPendingError

    errors = []

    async def fake_process_job(**kwargs):
        raise ClassificationPendingError("pending or claimed by another worker")

    async def fake_logger_log_error(*args, **kwargs):
        errors.append(args)

    monkeypatch.setattr(message_processor, "process_job", fake_process_job)
    monkeypatch.setattr(
        message_processor,
        "parse_job",
        lambda source, text: _fake_job(source, text),
    )
    monkeypatch.setattr(message_processor.logger, "log_error", fake_logger_log_error)

    assert asyncio.run(message_processor.process_message(_event())) is False
    assert errors == [], (
        "a ClassificationPendingError must not be recorded as a generic "
        "Message Processor error (its underlying cause was already "
        "audited where it happened)"
    )


def test_process_message_still_logs_real_failures(monkeypatch):
    """The generic-error path must be untouched for genuine failures."""
    errors = []

    async def fake_process_job(**kwargs):
        raise RuntimeError("boom")

    async def fake_logger_log_error(*args, **kwargs):
        errors.append(args)

    monkeypatch.setattr(message_processor, "process_job", fake_process_job)
    monkeypatch.setattr(
        message_processor,
        "parse_job",
        lambda source, text: _fake_job(source, text),
    )
    monkeypatch.setattr(message_processor.logger, "log_error", fake_logger_log_error)

    assert asyncio.run(message_processor.process_message(_event())) is False
    assert len(errors) == 1
    assert "boom" in str(errors[0])
