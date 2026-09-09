"""Minimal example JobSource adapter; disabled and never imported by default."""
from app.ports import JobSource

class ExampleJobSource(JobSource):
    id = "example"
    @property
    def identity_source(self):
        return "example"
    async def poll(self):
        return []
    def normalize(self, job):
        return {
            "title": job["title"], "description": job.get("description", ""),
            "url": job.get("url", ""), "source": "Example",
            "budget": job.get("budget", ""), "job_id": str(job["job_id"]),
            "identity_source": self.identity_source,
        }
