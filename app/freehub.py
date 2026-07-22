import aiohttp
from collections import deque

from app.config import (
    FREEHUB_USER_ID,
    FREEHUB_PAGE_SIZE,
)

BASE_URL = "http://ec2-51-21-119-160.eu-north-1.compute.amazonaws.com/v1/users"

SOURCES = (
    "kafiil",
    "freelancer",
)

_seen = {
    source: deque(maxlen=100)
    for source in SOURCES
}


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
    The first poll seeds the in-memory cache and returns nothing.
    """

    new_projects = []

    async with aiohttp.ClientSession() as session:

        for source in SOURCES:

            data = await fetch_projects(session, source)
            projects = data.get("items", [])

            if not projects:
                continue

            seen = _seen[source]

            # First run -> seed cache only
            if not seen:

                for project in projects:
                    seen.append(project["uid"])

                print(
                    f"[FREEHUB] Seeded {source} cache ({len(projects)} jobs)"
                )

                continue

            # Oldest -> newest
            for project in reversed(projects):

                uid = project["uid"]

                if uid in seen:
                    continue

                seen.append(uid)
                new_projects.append(project)

    return new_projects
