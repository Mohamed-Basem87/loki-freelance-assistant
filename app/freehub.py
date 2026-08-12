import aiohttp
from collections import deque

from app.config import (
    FREEHUB_USER_ID,
    FREEHUB_PAGE_SIZE,
)
from app.state import state

BASE_URL = "http://ec2-51-21-119-160.eu-north-1.compute.amazonaws.com/v1/users"

SOURCES = (
    "kafiil",
    "freelancer",
)

# aiohttp has no default total timeout, so an unresponsive backend
# could otherwise stall a poll cycle indefinitely.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# How many extra pages a single poll_once() call is allowed to walk
# for a source whose first fetched page turned out to be entirely
# unseen -- i.e. more projects were posted since the last poll than
# fit on one page (bot was offline, a poll cycle errored, etc). Before
# this, poll_once() only ever looked at page 1, so anything past it
# was silently unrecoverable -- unlike the Telegram side, which
# already walks its full backlog on restart (see
# app.handlers.telegram.MAX_RECOVERY_MESSAGES, the same bounded-
# fallback idea applied here).
_MAX_BACKFILL_PAGES = 10

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
# happens *after* this module has already been imported (bot.py
# imports app.freehub_worker, which imports this module, before it
# calls state.load()) -- reading state at import time would always
# see the empty pre-load default.
_seen = {source: deque(maxlen=_SEEN_MAXLEN) for source in SOURCES}
_seeded_from_state = False


def _ensure_seeded_from_state():
    global _seeded_from_state

    if _seeded_from_state:
        return

    for source in SOURCES:
        _seen[source].extend(state.get_freehub_seen(source))

    _seeded_from_state = True


async def _persist_seen(source: str):
    # Goes through state.async_set_freehub_seen (a dedicated
    # single-worker executor thread, mirroring app.logger.ExcelLogger)
    # rather than calling state.set_freehub_seen directly, since this
    # performs blocking file I/O and poll_once() runs concurrently
    # with the Telegram side under asyncio.gather -- see app.state.
    await state.async_set_freehub_seen(source, list(_seen[source]))


async def fetch_projects(session: aiohttp.ClientSession, source: str, page: int = 1):

    url = (
        f"{BASE_URL}/{FREEHUB_USER_ID}/projects"
        f"?page={page}"
        f"&page_size={FREEHUB_PAGE_SIZE}"
        f"&sort=newest"
        f"&source={source}"
    )

    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def poll_once():
    """
    Returns only new projects since the previous poll.
    The first-ever poll (no persisted state for this source) seeds
    the cache and returns nothing; every poll after that -- including
    the first one after a restart, since the cache is now persisted
    -- compares against what was actually seen before.

    If the first fetched page turns out to be entirely unseen (more
    projects were posted since the last poll than fit on one page),
    walk additional pages -- oldest direction -- until a page contains
    a project we've already seen, or `_MAX_BACKFILL_PAGES` is reached.
    Without this, a slow/errored poll cycle or downtime longer than
    one page's worth of new activity would silently drop the backlog
    past page 1, the same gap the Telegram side already closed with
    its own bounded recovery walk.
    """

    _ensure_seeded_from_state()

    new_projects = []

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:

        for source in SOURCES:

            seen = _seen[source]

            page_1 = await fetch_projects(session, source, page=1)
            projects = page_1.get("items", [])

            if not projects:
                continue

            # First-ever run for this source (nothing persisted, and
            # nothing seen yet this process) -> seed cache only.
            if not seen:

                for project in projects:
                    seen.append(project["uid"])

                await _persist_seen(source)

                print(
                    f"[FREEHUB] Seeded {source} cache ({len(projects)} jobs)"
                )

                continue

            fetched_pages = [projects]
            page_num = 1
            hit_backfill_cap = False

            # Every project on the last fetched page was unseen ->
            # there may be more of a backlog than one page can show.
            # Walk deeper pages until one contains a project we've
            # already seen (caught up), or we hit the safety cap.
            while all(
                project["uid"] not in seen for project in fetched_pages[-1]
            ):

                if page_num >= _MAX_BACKFILL_PAGES:
                    hit_backfill_cap = True
                    break

                page_num += 1

                next_page = await fetch_projects(session, source, page=page_num)
                next_projects = next_page.get("items", [])

                if not next_projects:
                    # No more pages available -- the backlog is fully
                    # exhausted, not capped, so no warning below.
                    break

                fetched_pages.append(next_projects)

            if hit_backfill_cap:
                print(
                    f"[FREEHUB WARNING] {source}: hit the "
                    f"{_MAX_BACKFILL_PAGES}-page backfill safety cap -- "
                    f"there may still be unrecovered projects older "
                    f"than what was just fetched. The next poll will "
                    f"continue from here."
                )

            # Oldest -> newest, across every page fetched this cycle
            # (fetched_pages[0] is the newest page, so walk it last).
            changed = False

            for page_projects in reversed(fetched_pages):

                for project in reversed(page_projects):

                    uid = project["uid"]

                    if uid in seen:
                        continue

                    seen.append(uid)
                    # Tag with the fixed poll-source ("kafiil"/
                    # "freelancer", the same value this function's own
                    # seen-cache dedup is keyed by) so downstream
                    # consumers (see app.freehub_worker) can derive job
                    # identity from it instead of the API's own
                    # "platform" field, which is live response data
                    # and not guaranteed to stay constant for the same
                    # project across polls. "_poll_source" is an
                    # underscore-prefixed key to avoid colliding with
                    # any real field the FreeHub API returns.
                    new_projects.append({**project, "_poll_source": source})
                    changed = True

            if changed:
                await _persist_seen(source)

    return new_projects