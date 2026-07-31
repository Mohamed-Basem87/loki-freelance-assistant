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

_SEEN_MAXLEN = 100

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


def _persist_seen(source: str):
    state.set_freehub_seen(source, list(_seen[source]))


async def fetch_projects(session: aiohttp.ClientSession, source: str):

    url = (
        f"{BASE_URL}/{FREEHUB_USER_ID}/projects"
        f"?page=1"
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
    """

    _ensure_seeded_from_state()

    new_projects = []

    async with aiohttp.ClientSession(timeout=_REQUEST_TIMEOUT) as session:

        for source in SOURCES:

            data = await fetch_projects(session, source)
            projects = data.get("items", [])

            if not projects:
                continue

            seen = _seen[source]

            # First-ever run for this source (nothing persisted, and
            # nothing seen yet this process) -> seed cache only.
            if not seen:

                for project in projects:
                    seen.append(project["uid"])

                _persist_seen(source)

                print(
                    f"[FREEHUB] Seeded {source} cache ({len(projects)} jobs)"
                )

                continue

            # Oldest -> newest
            changed = False

            for project in reversed(projects):

                uid = project["uid"]

                if uid in seen:
                    continue

                seen.append(uid)
                new_projects.append(project)
                changed = True

            if changed:
                _persist_seen(source)

    return new_projects
