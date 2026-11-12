"""Tests for scraper partial runs (--sources), snapshot merging and the
Upwork detail cache/cooldown.

A 1-minute Upwork-only scheduler group runs the same scraper script as the
300s LinkedIn+Wuzzuf group. These tests pin the two properties that make
that safe: a partial run never erases the platforms it did not scrape, and
already-enriched Upwork detail pages are not fetched again on every poll.
"""
import asyncio
import json
import time

import pytest

from scraper import scraper as sc


@pytest.fixture(autouse=True)
def _restore_run_options():
    saved = dict(sc.RUN_OPTIONS)
    try:
        yield
    finally:
        sc.RUN_OPTIONS.clear()
        sc.RUN_OPTIONS.update(saved)


def _records(platform, count=1):
    return [
        {
            "title": f"{platform} job {index}",
            "description": "desc",
            "raw_text": "desc",
            "source": platform,
            "url": f"https://example.com/{platform.lower()}/{index}",
            "company": "Acme",
        }
        for index in range(count)
    ]


# ------------------------------------------------------------
# CLI / source selection
# ------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, sc.ALL_SOURCES),
        ("all", sc.ALL_SOURCES),
        ("", sc.ALL_SOURCES),
        ("upwork", ("upwork",)),
        ("UPWORK,linkedin", ("linkedin", "upwork")),
        (["upwork", "wuzzuf"], ("wuzzuf", "upwork")),
    ],
)
def test_normalize_source_list(value, expected):
    assert sc._normalize_source_list(value) == expected


def test_normalize_source_list_rejects_unknown_sources():
    with pytest.raises(ValueError, match="unknown source"):
        sc._normalize_source_list("upwork,monster")


def test_parse_args_defaults_preserve_the_full_manual_run():
    args = sc.parse_args([])

    assert args.sources == "all"
    assert args.max_upwork_pages is None
    assert args.upwork_detail_max is None
    assert args.upwork_snapshot_limit is None


def test_configure_run_applies_upwork_fast_path_options():
    args = sc.parse_args(
        [
            "--sources", "upwork",
            "--max-upwork-pages", "1",
            "--upwork-detail-max", "3",
            "--upwork-snapshot-limit", "15",
        ]
    )

    sc.configure_run(args)

    assert sc.RUN_OPTIONS["sources"] == ("upwork",)
    assert sc.RUN_OPTIONS["max_upwork_pages"] == 1
    assert sc.RUN_OPTIONS["upwork_detail_budget"] == 3
    assert sc.RUN_OPTIONS["upwork_snapshot_limit"] == 15


def test_configure_run_rejects_unknown_source_with_system_exit():
    args = sc.parse_args(["--sources", "monster"])

    with pytest.raises(SystemExit):
        sc.configure_run(args)


def test_all_sources_run_keeps_defaults():
    sc.configure_run(sc.parse_args([]))

    assert sc.RUN_OPTIONS["sources"] == sc.ALL_SOURCES
    assert sc.RUN_OPTIONS["max_upwork_pages"] == sc.DEFAULT_RUN_OPTIONS[
        "max_upwork_pages"
    ]
    assert sc.RUN_OPTIONS["upwork_detail_budget"] == sc.DEFAULT_RUN_OPTIONS[
        "upwork_detail_budget"
    ]


# ------------------------------------------------------------
# Snapshot merge
# ------------------------------------------------------------


def test_partial_run_merges_and_preserves_other_platforms(tmp_path):
    snapshot = tmp_path / "jobs_results.json"
    snapshot.write_text(
        json.dumps(
            _records("Wuzzuf", 1)
            + _records("LinkedIn", 1)
            + _records("Upwork", 2)
        ),
        encoding="utf-8",
    )

    sc.RUN_OPTIONS["sources"] = ("upwork",)
    new_upwork = _records("Upwork", 1)

    sc.save_results(new_upwork, filename=str(snapshot))

    merged = json.loads(snapshot.read_text(encoding="utf-8"))
    by_source = {}
    for record in merged:
        by_source.setdefault(record["source"], []).append(record["url"])

    # The scraped platform is replaced wholesale (old Upwork record gone)...
    assert by_source["Upwork"] == [new_upwork[0]["url"]]
    # ...while platforms this run did not scrape survive untouched.
    assert by_source["Wuzzuf"] == [_records("Wuzzuf", 1)[0]["url"]]
    assert by_source["LinkedIn"] == [_records("LinkedIn", 1)[0]["url"]]


def test_partial_run_does_not_resurrect_records_from_other_scraped_platforms(
    tmp_path,
):
    snapshot = tmp_path / "jobs_results.json"
    snapshot.write_text(
        json.dumps(_records("Upwork", 2) + _records("LinkedIn", 1)),
        encoding="utf-8",
    )

    sc.RUN_OPTIONS["sources"] = ("upwork",)
    sc.save_results([], filename=str(snapshot))

    remaining = json.loads(snapshot.read_text(encoding="utf-8"))
    assert [record["source"] for record in remaining] == ["LinkedIn"]


def test_full_run_overwrites_the_snapshot(tmp_path):
    snapshot = tmp_path / "jobs_results.json"
    snapshot.write_text(
        json.dumps(_records("Wuzzuf", 2) + _records("Upwork", 1)),
        encoding="utf-8",
    )

    sc.RUN_OPTIONS["sources"] = sc.ALL_SOURCES
    replacement = _records("LinkedIn", 1)

    sc.save_results(replacement, filename=str(snapshot))

    assert json.loads(snapshot.read_text(encoding="utf-8")) == replacement


def test_partial_merge_replaces_file_atomically_leaving_no_temp_file(tmp_path):
    snapshot = tmp_path / "jobs_results.json"
    snapshot.write_text(json.dumps(_records("Wuzzuf", 1)), encoding="utf-8")

    sc.RUN_OPTIONS["sources"] = ("upwork",)
    sc.save_results(_records("Upwork", 1), filename=str(snapshot))

    leftovers = [
        path.name
        for path in tmp_path.iterdir()
        if path.name.startswith(".") or path.suffix == ".tmp"
    ]
    assert leftovers == []


# ------------------------------------------------------------
# Upwork detail cache / cooldown
# ------------------------------------------------------------


def test_detail_cache_round_trips_and_prunes_expired_entries(tmp_path):
    cache_file = tmp_path / "upwork_detail_cache.json"
    now = time.time()
    cache = {
        "entries": {
            "https://upwork.com/jobs/~01": {
                "DatePosted": "2026-11-12",
                "DetailStatus": 200,
                "fetched_at": now,
            },
            "https://upwork.com/jobs/~02": {
                "Description": "stale",
                "DetailStatus": 200,
                "fetched_at": now
                - sc.UPWORK_CACHE_MAX_AGE_SECONDS
                - 1,
            },
            "https://upwork.com/jobs/~03": {
                # Written by an older build from a search card only:
                # no detail fetch ever happened, so it must not suppress
                # the job's future enrichment.
                "Description": "card only",
                "fetched_at": now,
            },
        },
        "cooldown_until": now + 60,
    }

    sc.save_upwork_detail_cache(cache, filename=str(cache_file))
    loaded = sc.load_upwork_detail_cache(filename=str(cache_file))

    assert set(loaded["entries"]) == {"https://upwork.com/jobs/~01"}
    assert loaded["cooldown_until"] == pytest.approx(now + 60)


def test_detail_cache_caps_entry_count_to_the_newest(tmp_path):
    cache_file = tmp_path / "upwork_detail_cache.json"
    now = time.time()
    entries = {
        f"https://upwork.com/jobs/~{index}": {
            "DetailStatus": 200,
            "fetched_at": now - index,
        }
        for index in range(sc.UPWORK_CACHE_MAX_ENTRIES + 25)
    }

    sc.save_upwork_detail_cache(
        {"entries": entries, "cooldown_until": 0},
        filename=str(cache_file),
    )
    loaded = sc.load_upwork_detail_cache(filename=str(cache_file))

    assert len(loaded["entries"]) == sc.UPWORK_CACHE_MAX_ENTRIES
    # ~0 is the newest; the 25 oldest were dropped.
    assert "https://upwork.com/jobs/~0" in loaded["entries"]
    assert (
        f"https://upwork.com/jobs/~{sc.UPWORK_CACHE_MAX_ENTRIES + 24}"
        not in loaded["entries"]
    )


def test_detail_cache_tolerates_missing_or_corrupt_files(tmp_path):
    missing = sc.load_upwork_detail_cache(filename=str(tmp_path / "nope.json"))
    assert missing == {"entries": {}, "cooldown_until": 0.0}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert sc.load_upwork_detail_cache(filename=str(corrupt)) == {
        "entries": {},
        "cooldown_until": 0.0,
    }


def test_remember_only_caches_successful_details(tmp_path):
    cache = {"entries": {}, "cooldown_until": 0}

    blocked = {
        "Link": "https://upwork.com/jobs/~blocked",
        "Description": "card text",
        "DetailBlocked": True,
    }
    assert sc._remember_upwork_detail(blocked, cache) is False
    assert cache["entries"] == {}

    ok = {
        "Link": "https://upwork.com/jobs/~ok",
        "Description": "card text",
        "DescriptionSource": "detail_structured",
        "Company": "Acme",
        "DatePosted": "2026-11-12",
        "ClientLocation": "United States",
        "DetailStatus": 200,
        "DetailFetcher": "HTTP",
    }
    assert sc._remember_upwork_detail(ok, cache) is True

    entry = cache["entries"]["https://upwork.com/jobs/~ok"]
    assert entry["ClientLocation"] == "United States"
    assert entry["DetailStatus"] == 200
    assert entry["fetched_at"] > 0
    # The search card already carries the description; caching it would
    # only bloat the file.
    assert "Description" not in entry


def test_remember_does_not_cache_card_only_jobs():
    """A search-card job has a description but never hit its detail page;
    caching it would freeze its DatePosted/client country forever."""
    cache = {"entries": {}, "cooldown_until": 0}
    card_only = {
        "Link": "https://upwork.com/jobs/~card",
        "Description": "card description",
        "DescriptionSource": "search_card",
    }

    assert sc._remember_upwork_detail(card_only, cache) is False
    assert cache["entries"] == {}


def test_cached_detail_overlays_card_and_clears_failure_flags():
    cache = {
        "entries": {
            "https://upwork.com/jobs/~ok": {
                "DatePosted": "2026-11-12",
                "ClientLocation": "Germany",
                "DetailStatus": 200,
            }
        },
        "cooldown_until": 0,
    }
    job = {
        "Link": "https://upwork.com/jobs/~ok",
        "Description": "card description",
        "DetailError": "old failure",
        "DetailNote": "noise",
    }

    assert sc._apply_cached_upwork_detail(job, cache) is True
    assert job["Description"] == "card description"
    assert job["ClientLocation"] == "Germany"
    assert "DetailError" not in job
    assert "DetailNote" not in job


def test_cache_miss_leaves_card_untouched_and_requests_detail():
    cache = {"entries": {}, "cooldown_until": 0}
    job = {"Link": "https://upwork.com/jobs/~new", "Description": "card"}

    assert sc._apply_cached_upwork_detail(job, cache) is False
    assert job == {"Link": "https://upwork.com/jobs/~new", "Description": "card"}


def test_cooldown_persists_across_runs(tmp_path):
    cache_file = tmp_path / "upwork_detail_cache.json"
    sc.save_upwork_detail_cache(
        {"entries": {}, "cooldown_until": time.time() + 600},
        filename=str(cache_file),
    )

    loaded = sc.load_upwork_detail_cache(filename=str(cache_file))
    assert loaded["cooldown_until"] > time.time()


def test_detail_worker_aborts_immediately_when_cooldown_tripped():
    job = {
        "Platform": "Upwork",
        "Link": "https://upwork.com/jobs/~skipped",
        "Description": "card",
    }

    result = asyncio.run(
        sc.detail_worker(None, job, None, abort={"flag": True})
    )

    # Same object, untouched: no network session/request was used.
    assert result is job


# ------------------------------------------------------------
# Budget / cooldown behaviour inside the detail phase
# ------------------------------------------------------------


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc_info):
        return False


def _upwork_jobs(count):
    return [
        {
            "Platform": "Upwork",
            "Title": f"job {index}",
            "Link": f"https://upwork.com/jobs/~{index}",
            "Description": f"card {index}",
        }
        for index in range(count)
    ]


def _run_detail_phase(monkeypatch, jobs, cache, fake_worker):
    saved = {}
    calls = []

    monkeypatch.setattr(sc, "load_upwork_detail_cache", lambda: cache)
    monkeypatch.setattr(
        sc, "save_upwork_detail_cache", lambda value: saved.update(value)
    )
    monkeypatch.setattr(sc, "upwork_session", lambda: _FakeSessionCtx())

    async def worker(session, job, semaphore, abort=None):
        calls.append(job["Link"])
        return await fake_worker(job, abort)

    monkeypatch.setattr(sc, "detail_worker", worker)

    results = asyncio.run(sc.scrape_all_details(jobs))
    return results, calls, saved


def test_detail_phase_serves_cache_and_respects_budget(monkeypatch):
    jobs = _upwork_jobs(5)
    cached_job = jobs[0]
    cache = {
        "entries": {
            cached_job["Link"]: {
                "DatePosted": "2026-11-12",
                "ClientLocation": "United States",
                "DetailStatus": 200,
                "fetched_at": time.time(),
            }
        },
        "cooldown_until": 0,
    }
    sc.RUN_OPTIONS["upwork_detail_budget"] = 2
    sc.RUN_OPTIONS["upwork_snapshot_limit"] = 10

    async def ok(job, abort):
        return {**job, "DetailStatus": 200}

    results, calls, saved = _run_detail_phase(monkeypatch, jobs, cache, ok)

    # One cached + two budgeted fetches; the remaining two stay on card data.
    assert calls == [jobs[1]["Link"], jobs[2]["Link"]]
    assert len(results) == 5
    assert results[0]["Description"] == "card 0"
    assert results[0]["ClientLocation"] == "United States"
    assert results[4]["Description"] == "card 4"
    # The two fresh successes are persisted for later runs.
    assert set(saved["entries"]) == {cached_job["Link"], jobs[1]["Link"], jobs[2]["Link"]}
    assert saved["cooldown_until"] == 0


def test_detail_phase_skips_everything_during_cooldown(monkeypatch):
    jobs = _upwork_jobs(4)
    cache = {"entries": {}, "cooldown_until": time.time() + 600}
    sc.RUN_OPTIONS["upwork_detail_budget"] = 3
    sc.RUN_OPTIONS["upwork_snapshot_limit"] = 10

    async def ok(job, abort):
        return {**job, "DetailStatus": 200}

    results, calls, saved = _run_detail_phase(monkeypatch, jobs, cache, ok)

    assert calls == []
    assert results == jobs
    # The cache is still rewritten so pruning is durable, but no detail
    # fetch happened.
    assert saved["entries"] == {}
    assert saved["cooldown_until"] > time.time()


def test_detail_phase_persists_cooldown_after_a_block_streak(monkeypatch):
    jobs = _upwork_jobs(8)
    cache = {"entries": {}, "cooldown_until": 0}
    sc.RUN_OPTIONS["upwork_detail_budget"] = 8
    sc.RUN_OPTIONS["upwork_snapshot_limit"] = 10
    attempted = {"count": 0}

    async def blocked(job, abort):
        if abort and abort.get("flag"):
            # Mirrors the real worker: a task that starts after the streak
            # trips leaves the job on its card data.
            return job
        attempted["count"] += 1
        await asyncio.sleep(0)
        return {
            **job,
            "DetailStatus": None,
            "DetailBlocked": True,
        }

    results, calls, saved = _run_detail_phase(monkeypatch, jobs, cache, blocked)

    assert attempted["count"] >= sc.UPWORK_BLOCK_STREAK_LIMIT
    assert saved["cooldown_until"] > time.time()
    # Boilerplate still yields every job (card data kept); nothing is lost.
    assert len(results) == len(jobs)
    for job in results:
        assert job["Description"].startswith("card ")


def test_detail_phase_does_not_trip_cooldown_on_isolated_failures(monkeypatch):
    jobs = _upwork_jobs(6)
    cache = {"entries": {}, "cooldown_until": 0}
    sc.RUN_OPTIONS["upwork_detail_budget"] = 6
    sc.RUN_OPTIONS["upwork_snapshot_limit"] = 10
    outcomes = iter(["fail", "fail", "ok", "fail", "ok", "ok"])

    async def mixed(job, abort):
        await asyncio.sleep(0)
        if next(outcomes) == "ok":
            return {**job, "DetailStatus": 200}
        return {**job, "DetailStatus": None, "DetailBlocked": True}

    results, calls, saved = _run_detail_phase(monkeypatch, jobs, cache, mixed)

    # Alternating failures never reach the consecutive threshold.
    assert saved["cooldown_until"] == 0
    assert len(results) == len(jobs)

