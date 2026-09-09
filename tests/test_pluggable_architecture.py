import asyncio
import pytest
from app.ports import JobSource
from app.source_worker import SourceWorker


class FakeSource(JobSource):
    id = "fake"
    @property
    def identity_source(self):
        return "fake"
    async def poll(self):
        return [{"job_id": "1"}, {"job_id": "2"}]
    def normalize(self, job):
        return {"title": job["job_id"], "description": "", "url": "", "source": "Fake",
                "budget": "", "job_id": job["job_id"], "identity_source": self.identity_source}
    async def mark_seen(self, job):
        self.marked.append(job["job_id"])


def test_source_worker_isolates_items_and_marks_after_success(monkeypatch):
    async def scenario():
        source = FakeSource()
        source.marked = []
        processed = []
        logged = []
        async def process(job, src):
            processed.append(job["job_id"])
            if job["job_id"] == "2":
                raise RuntimeError("bad item")
        async def sleep(_):
            raise asyncio.CancelledError
        class FakeLogger:
            async def log_error(self, source_id, exc, *args):
                logged.append((source_id, str(exc)))
        monkeypatch.setattr("app.source_worker.asyncio.sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await SourceWorker(source, process, 0, logger=FakeLogger()).run()
        assert processed == ["1", "2"]
        assert source.marked == ["1"]
        assert logged == [("fake", "bad item")]

    asyncio.run(scenario())
