import asyncio
import os
import time
from collections import deque
from pathlib import Path

from app.config import (
    FREEHUB_BASE_URL,
    FREEHUB_USER_ID,
    FREEHUB_PAGE_SIZE,
)
from app.runtime_config import RUNTIME, RECOVERY, SOURCE_IDS
from app.dependencies import dedup, logger


# Configurable so deployments can switch to the TLS endpoint when
# the backend provides HTTPS without changing application code.
BASE_URL = FREEHUB_BASE_URL

SOURCES = SOURCE_IDS

# aiohttp has no default total timeout, so an unresponsive backend
# could otherwise stall a poll cycle indefinitely.
_REQUEST_TIMEOUT = RUNTIME.http_timeout_seconds

# How many extra pages a single poll_once() call is allowed to walk
# for a source whose first fetched page turned out to be entirely
# unseen -- i.e. more projects were posted since the last poll than
# fit on one page (bot was offline, a poll cycle errored, etc). Before
# this, poll_once() only ever looked at page 1, so anything past it
# was silently unrecoverable -- unlike the Telegram side, which
# already walks its full backlog on restart (see
# app.adapters.sources.telegram.DEFAULT_RECOVERY_MAX_MESSAGES, the same
# bounded-fallback idea applied here).
_MAX_BACKFILL_PAGES = RECOVERY.freehub_max_backfill_pages

# Sized to comfortably hold a full backfill (_MAX_BACKFILL_PAGES pages
# at FREEHUB_PAGE_SIZE each) without the dedup window evicting ids
# from earlier in the same backfill before that backfill even finishes.
_SEEN_MAXLEN = max(500, FREEHUB_PAGE_SIZE * _MAX_BACKFILL_PAGES)

# Seeded from persisted state, not an empty cache -- previously this
# was in-memory only, so every process restart silently reset dedup
# tracking and any project posted between shutdown and the next
# successful poll was lost for good (never recovered, unlike the
# Telegram side which persists a watermark). A restart now resumes
# from whatever was last saved.
#
# Built lazily (on first poll_once() call) rather than at import
# time: app.state.state.load() runs inside app.bot.run(), which
# happens *after* this module has already been imported -- reading
# state at import time would always see the empty pre-load default.
_seen = {source: deque(maxlen=_SEEN_MAXLEN) for source in SOURCES}
_seeded_from_state = False

# Observability: track pending queue metrics for health/debugging
_pending_metrics = {
    "last_poll_time": None,
    "last_pending_count": 0,
    "last_pending_oldest_age_seconds": 0,
    "total_polls": 0,
    "total_discovered": 0,
    "total_errors": 0,
}


async def _log_pending_metrics():
    """Log FreeHub pending queue observability metrics."""
    for source in SOURCES:
        try:
            pending = await dedup.get_pending(source)
            pending_count = len(pending)
            
            # Calculate oldest pending age if we have timestamps
            oldest_age = 0
            if pending:
                now = time.time()
                for item in pending:
                    # Try to get a timestamp from the item
                    ts = item.get("created_at") or item.get("discovered_at") or item.get("timestamp")
                    if ts:
                        try:
                            age = now - float(ts)
                            if age > oldest_age:
                                oldest_age = age
                        except (TypeError, ValueError):
                            pass
            
            # Get state file sizes
            state_file = Path(RUNTIME.state_file_path)
            backup_file = state_file.with_suffix(".bak.json")
            state_size = state_file.stat().st_size if state_file.exists() else 0
            backup_size = backup_file.stat().st_size if backup_file.exists() else 0
            
            _pending_metrics["last_poll_time"] = time.time()
            _pending_metrics["last_pending_count"] = pending_count
            _pending_metrics["last_pending_oldest_age_seconds"] = oldest_age
            
            print(
                f"[FREEHUB METRICS] source={source} "
                f"pending_count={pending_count} "
                f"oldest_pending_age_seconds={oldest_age:.0f} "
                f"state_json_bytes={state_size} "
                f"backup_json_bytes={backup_size}"
            )
        except Exception as e:
            print(f"[FREEHUB METRICS ERROR] source={source}: {e}")


def get_pending_metrics():
    """Return current pending queue metrics for health/debugging."""
    return dict(_pending_metrics)


async def _ensure_seeded_from_state():
    global _seeded_from_state

    if _seeded_from_state:
        return

    for source in SOURCES:
        _seen[source].extend(await dedup.get_seen(source))

    _seeded_from_state = True


async def _persist_seen(source: str):
    # Goes through state.async_set_freehub_seen (a dedicated
    # single-worker executor thread, mirroring app.logger.DBLogger)
    # rather than calling state.set_freehub_seen directly, since this
    # performs blocking file I/O and poll_once() runs concurrently
    # with the Telegram side under asyncio.gather -- see app.state.
    await dedup.set_seen(source, list(_seen[source]))


async def fetch_projects(session, source: str, page: int = 1, *, client=None):
    """Compatibility seam; concrete HTTP is supplied by the source adapter
    (FreeHubJobSource), which never discovers infrastructure itself. When no
    client is supplied this seam constructs one with explicitly-passed
    config-derived collaborators so it stays a thin delegator into the
    canonical FreeHubApiClient."""
    if client is None:
        from app.config import FREEHUB_BASE_URL, get_freehub_user_id
        from app.runtime_config import RUNTIME
        from app.adapters.http.registry import build as _build_transport
        from app.adapters.sources.freehub import FreeHubApiClient
        client = FreeHubApiClient(
            base_url=FREEHUB_BASE_URL,
            user_id=get_freehub_user_id(),
            timeout=RUNTIME.http_timeout_seconds,
            page_size=RUNTIME.freehub_page_size,
            transport=_build_transport(timeout=RUNTIME.http_timeout_seconds),
        )
    return await client.fetch_projects(source, page=page)


def _page_fully_seen(items: list, seen) -> bool:
    """True if every project on this page is already in `seen`.

    Used as the stopping condition for backfill pagination (see
    poll_once/poll_source below, audit finding P1-1). A *mixed* page
    -- some new items followed by older already-seen ones, the normal
    shape of the boundary page in an active feed -- must NOT stop
    pagination, since strictly older, still-unseen items can exist on
    the next page (see test_mixed_seen_page_does_not_starve_older_
    unseen_page). Only a page containing zero new items is a safe
    place to stop, since FreeHub lists newest-first and everything
    past a fully-seen page is guaranteed to be even older.
    """
    return bool(items) and all(str(p.get("uid")) in seen for p in items)


async def poll_once(*, client=None):
    """Discover FreeHub projects with bounded, durable, concurrent backfill."""
    await _ensure_seeded_from_state()

    async def poll_source(source):
        async def fetch(page):
            if client is None:
                return await fetch_projects(None, source, page=page)
            return await fetch_projects(None, source, page=page, client=client)

        seen = _seen[source]
        pending = await dedup.get_pending(source)
        pending_by_uid = {str(item.get("uid")): item for item in pending if item.get("uid") is not None}
        discovered_result = [{**project, "_poll_source": source} for project in pending_by_uid.values()]

        page_1 = await fetch(1)
        projects = page_1.get("items", [])
        if not projects:
            return discovered_result

        if not seen:
            # Normalized to str() so a source that returns integer uids
            # cannot create a seen-set whose members never match the
            # str()-normalized membership checks everywhere else
            # (discovery, _page_fully_seen, mark_project_seen, pending
            # dedup) -- a raw/str mismatch there silently re-discovers
            # and re-queues every project on every poll and prevents the
            # fully-seen pagination stop condition from ever firing.
            seen.extend(
                str(project["uid"])
                for project in projects
                if project.get("uid") is not None
            )
            await _persist_seen(source)
            print(f"[FREEHUB] Seeded {source} cache ({len(projects)} jobs)")
            return discovered_result

        fetched = [(1, projects)]

        if _page_fully_seen(projects, seen):
            # Regression fix (audit finding P1-1): the previous
            # implementation always walked forward to
            # _MAX_BACKFILL_PAGES on every single poll, even when page
            # 1 -- the newest content -- was already entirely
            # processed (the steady-state case: nothing new has been
            # posted since the last poll). With 4 sources and a
            # 10-page cap on a 60s interval that meant up to 40
            # requests/minute (~57,600/day) to upstream even when
            # there was nothing new to find. When the very first page
            # is already fully seen, there is nothing further back
            # that could possibly be new (FreeHub lists newest-first),
            # so no further pages are fetched at all -- restoring the
            # steady-state cost to exactly 1 request/source/poll.
            #
            # Reset any stale continuation page from a previous
            # cap-hit backfill: having fully caught up now, resuming
            # from an old mid-backfill page next time would only risk
            # re-skipping content near page 1 (see P1-2) for no
            # benefit.
            await dedup.set_backfill_page(source, 2)
        else:
            start_page = await dedup.get_backfill_page(source)
            # NOTE (audit finding P1-2): this persisted page number is
            # a best-effort continuation hint, not a durable cursor --
            # FreeHub's API exposes no stable pagination token, and
            # newest-first offset pagination can shift under
            # concurrent insertion between polls (a project landing on
            # "page 10" now may be "page 11" by the time this resumes
            # there), so this cannot guarantee zero skipped projects
            # across an arbitrarily long gap between polls. What *is*
            # guaranteed, via the identity-based stopping condition
            # below (and the fully-seen-page fast path above), is that
            # a single poll never stops early while unseen content
            # plausibly remains, and never does unbounded work once it
            # actually catches up to previously-seen content.
            page = max(2, start_page)
            pages_fetched = 1
            hit_cap = False

            while pages_fetched < _MAX_BACKFILL_PAGES:
                next_page = await fetch(page)
                items = next_page.get("items", [])
                if not items:
                    await dedup.set_backfill_page(source, 2)
                    break
                fetched.append((page, items))
                pages_fetched += 1
                if _page_fully_seen(items, seen):
                    # This entire page is already-known content --
                    # everything beyond it is strictly older (newest-
                    # first ordering), so there is nothing further
                    # back worth fetching this poll. This is the exact
                    # missing stopping condition from audit finding
                    # P1-1: previously only an *empty* page (end of
                    # upstream data) stopped the loop early; a page
                    # that was entirely already-seen did not, and the
                    # loop kept walking all the way to the safety cap
                    # on every single poll.
                    await dedup.set_backfill_page(source, 2)
                    break
                page += 1
            else:
                hit_cap = True

            if hit_cap:
                await dedup.set_backfill_page(source, page)
                print(f"[FREEHUB WARNING] {source}: hit the {_MAX_BACKFILL_PAGES}-page backfill safety cap; continuation page {page} persisted.")

        pending_items = list(pending)
        discovered_uids = set(pending_by_uid)
        discovered = []
        for _, page_projects in reversed(fetched):
            for project in reversed(page_projects):
                uid = str(project.get("uid", ""))
                if not uid or uid in seen or uid in discovered_uids:
                    continue
                discovered_uids.add(uid)
                discovered.append({**project, "_poll_source": source})

        if discovered:
            await dedup.set_pending(source, pending_items + discovered)
            discovered_result.extend(discovered)
        return discovered_result

    batches = await asyncio.gather(*(poll_source(source) for source in SOURCES), return_exceptions=True)
    new_projects = []
    for source, result in zip(SOURCES, batches):
        if isinstance(result, Exception):
            # Preserve per-source isolation: one upstream outage must not stop
            # the other configured FreeHub platforms from being polled.
            print(f"[FREEHUB ERROR] {source}: {result}")
            _pending_metrics["total_errors"] += 1
            continue
        new_projects.extend(result)

    _pending_metrics["total_polls"] += 1
    _pending_metrics["total_discovered"] += len(new_projects)

    # Log observability metrics
    await _log_pending_metrics()

    return new_projects


async def mark_project_seen(project: dict):
    """
    Persist a FreeHub project as seen only after downstream processing
    completes successfully.

    poll_once() does not mark newly discovered projects as seen because
    doing so before process_job() succeeds can permanently discard a
    project after a processing failure.
    """

    source = project["_poll_source"]
    # Normalized the same way discovery and _page_fully_seen do; a raw
    # uid (e.g. an integer from the API) must never be compared to the
    # str()-normalized seen set, or the project is treated as unseen
    # forever and re-queued on every poll (and re-persisted in its raw
    # form on every mark).
    uid = str(project.get("uid") or "")
    if not uid:
        return

    if uid not in _seen[source]:
        _seen[source].append(uid)
        await _persist_seen(source)

    pending = await dedup.get_pending(source) if hasattr(dedup, "get_pending") else []
    remaining = [item for item in pending if str(item.get("uid")) != str(uid)]
    if len(remaining) != len(pending) and hasattr(dedup, "set_pending"):
        await dedup.set_pending(source, remaining)
