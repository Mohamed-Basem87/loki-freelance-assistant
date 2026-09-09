"""JobSources (LinkedIn / Wuzzuf) backed by the scraper's JSON snapshot.

scripts/linkedin_wuzzuf_scraper.py writes a jobs_results.json snapshot of
already-normalized records (title, description, raw_text, source, url,
company). These adapters attach the app to that file and feed its records
through the normal pipeline exactly like FreeHub: poll() -> normalize() ->
process_job -> mark_seen.

One concrete subclass per platform so each is its own JobSource (its own
worker, its own toggle, its own identity_source). A single class registered
under two config ids would trip the duplicate-adapter guard in app/workers.py
and could not be enabled/toggled independently.

This module never touches app.config: the file path is injected through the
config job-source "settings".
"""

import json

from app.ports import JobSource


class ScraperFileClient:
    """Reads one platform's records from the scraper-produced snapshot.

    The snapshot is the scraper's full discovered-job history, new jobs
    first. Feeding the whole snapshot back each poll is intentional and
    idempotent: process_job() fast-paths rows it already knows and the
    retry sweeps recover the rest (the historical host feeder did exactly
    this on a cron cadence).
    """

    def __init__(self, file_path, platform):
        self.file_path = file_path
        self.platform = platform

    def read(self):
        try:
            with open(self.file_path, "r", encoding="utf-8") as handle:
                records = json.load(handle)
        except (OSError, ValueError):
            return []
        return [
            record
            for record in records
            if isinstance(record, dict)
            and str(record.get("source") or "").strip().lower() == self.platform.lower()
        ]


class FileJobSource(JobSource):
    """Base JobSource over one platform's slice of a scraper snapshot file."""

    platform = ""

    def __init__(self, client):
        self._client = client
        self._seen = set()

    @property
    def identity_source(self):
        return self.id

    async def poll(self):
        fresh = []
        batch_seen = set()
        for record in self._client.read():
            normalized = self.normalize(record)
            key = self.job_identity(normalized)
            if key in self._seen or key in batch_seen:
                continue
            batch_seen.add(key)
            fresh.append(normalized)
        return fresh

    async def mark_seen(self, job):
        key = job.get("job_id") or job.get("uid") or job.get("url")
        if key:
            self._seen.add(str(key))

    def normalize(self, record):
        title = (record.get("title") or "").strip()
        description = (record.get("description") or "").strip()
        url = (record.get("url") or "").strip()
        raw_text = (record.get("raw_text") or "").strip()
        if not raw_text:
            raw_text = f"{title}\n\n{description}".strip()
        return {
            "title": title,
            "description": description,
            "raw_text": raw_text,
            "source": (record.get("source") or self.platform or self.id),
            "company": (record.get("company") or "").strip(),
            "url": url,
            "job_id": url,
            "identity_source": self.identity_source,
        }


class LinkedInFileJobSource(FileJobSource):
    id = "linkedin"
    platform = "LinkedIn"


class WuzzufFileJobSource(FileJobSource):
    id = "wuzzuf"
    platform = "Wuzzuf"