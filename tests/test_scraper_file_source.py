"""app.adapters.sources.scraper_file tests: JSON-snapshot JobSources.

The adapters are configuration-free: a ScraperFileClient reads one
platform's slice of the scraper's jobs_results.json snapshot and
FileJobSource drives poll()/normalize()/mark_seen() exactly like the
in-process source_worker expects. No config or network is touched here
(links resolve against a tmp_path JSON file).
"""

import asyncio
import json

from app.adapters.sources.scraper_file import (
    LinkedInFileJobSource,
    ScraperFileClient,
    WuzzufFileJobSource,
)


def _record(title="Senior Backend Engineer", platform="LinkedIn",
            url="https://linkedin.invalid/jobs/view/1", company="Acme"):
    return {
        "title": title,
        "description": "A detailed job description.",
        "raw_text": f"{title}\n\nA detailed job description.",
        "source": platform,
        "url": url,
        "company": company,
    }


def _write_snapshot(path, records):
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def test_client_filters_platform_and_tolerates_garbage(tmp_path):
    snap = _write_snapshot(
        tmp_path / "jobs_results.json",
        [
            _record(platform="LinkedIn", url="https://linkedin.invalid/jobs/view/1"),
            _record(platform="Wuzzuf", url="https://wuzzuf.invalid/job/2"),
            None,
            {"source": "WUZZUF", "title": "Mixed case", "url": "https://wuzzuf.invalid/job/3"},
        ],
    )
    client = ScraperFileClient(file_path=str(snap), platform="Wuzzuf")
    records = client.read()
    assert [r["url"] for r in records] == [
        "https://wuzzuf.invalid/job/2",
        "https://wuzzuf.invalid/job/3",
    ]


def test_client_missing_file_returns_empty(tmp_path):
    client = ScraperFileClient(
        file_path=str(tmp_path / "does_not_exist.json"), platform="LinkedIn"
    )
    assert client.read() == []


def test_client_invalid_json_returns_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")
    client = ScraperFileClient(file_path=str(bad), platform="LinkedIn")
    assert client.read() == []


def test_poll_returns_only_this_platforms_normalized_records(tmp_path):
    snap = _write_snapshot(
        tmp_path / "jobs_results.json",
        [
            _record(platform="LinkedIn", url="https://linkedin.invalid/jobs/view/1"),
            _record(platform="Wuzzuf", url="https://wuzzuf.invalid/job/2"),
        ],
    )
    source = WuzzufFileJobSource(
        client=ScraperFileClient(file_path=str(snap), platform="Wuzzuf")
    )
    jobs = asyncio.run(source.poll())
    assert [job["url"] for job in jobs] == ["https://wuzzuf.invalid/job/2"]
    job = jobs[0]
    assert job["identity_source"] == "wuzzuf"
    assert job["job_id"] == job["url"]
    assert job["source"] == "Wuzzuf"
    assert job["title"] == "Senior Backend Engineer"
    assert job["company"] == "Acme"


def test_normalize_falls_back_to_built_raw_text(tmp_path):
    snap = _write_snapshot(
        tmp_path / "jobs_results.json",
        [{"source": "LinkedIn", "title": "DevOps", "description": "Ops work.", "url": "https://linkedin.invalid/jobs/view/9"}],
    )
    source = LinkedInFileJobSource(
        client=ScraperFileClient(file_path=str(snap), platform="LinkedIn")
    )
    job = asyncio.run(source.poll())[0]
    assert job["raw_text"] == "DevOps\n\nOps work."
    assert job["identity_source"] == "linkedin"


def test_poll_skips_already_seen_jobs(tmp_path):
    snap = _write_snapshot(
        tmp_path / "jobs_results.json",
        [_record(url="https://linkedin.invalid/jobs/view/7")],
    )
    source = LinkedInFileJobSource(
        client=ScraperFileClient(file_path=str(snap), platform="LinkedIn")
    )
    assert len(asyncio.run(source.poll())) == 1
    asyncio.run(source.mark_seen({"url": "https://linkedin.invalid/jobs/view/7"}))
    assert asyncio.run(source.poll()) == []


def test_poll_dedupes_duplicate_urls_in_one_batch(tmp_path):
    snap = _write_snapshot(
        tmp_path / "jobs_results.json",
        [
            _record(url="https://linkedin.invalid/jobs/view/3"),
            _record(url="https://linkedin.invalid/jobs/view/3"),
        ],
    )
    source = LinkedInFileJobSource(
        client=ScraperFileClient(file_path=str(snap), platform="LinkedIn")
    )
    jobs = asyncio.run(source.poll())
    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://linkedin.invalid/jobs/view/3"


def test_identity_source_matches_source_id():
    assert LinkedInFileJobSource(
        client=ScraperFileClient(file_path="unused.json", platform="LinkedIn")
    ).identity_source == "linkedin"
    assert WuzzufFileJobSource(
        client=ScraperFileClient(file_path="unused.json", platform="Wuzzuf")
    ).identity_source == "wuzzuf"