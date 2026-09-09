"""FreeHub JobSource adapter.

Every collaborator (HTTP client, poller, marker) is injected by the
composition root. This module never reads app.config / app.runtime_config
and never constructs infrastructure itself -- there is no hidden
service-locator or adapter-owned dependency construction here.
"""

from app.ports import JobSource


class FreeHubApiClient:
    """FreeHub API adapter. HTTP mechanics are delegated to an injected
    HttpTransport (app.ports). Configuration values (base_url, user_id,
    timeout, page_size) are injected by the composition root, never
    resolved from app.config here."""

    def __init__(self, base_url, user_id, *, timeout=None, page_size=None, transport=None):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.timeout = timeout
        self.page_size = page_size
        self.transport = transport

    async def fetch_projects(self, source, *, page=1):
        if self.transport is None:
            raise RuntimeError(
                "FreeHubApiClient requires an injected transport; construct "
                "it via app.composition.compose()."
            )
        if self.page_size is None:
            raise RuntimeError(
                "FreeHubApiClient requires an injected page_size; construct "
                "it via app.composition.compose()."
            )
        url = f"{self.base_url}/{self.user_id}/projects?page={page}&page_size={self.page_size}&sort=newest&source={source}"
        return await self.transport.get_json(url, timeout=self.timeout)


class FreeHubJobSource(JobSource):
    id = "freehub"

    def __init__(self, poller, marker, http_client):
        self.http_client = http_client
        self._poller = poller
        self._marker = marker

    @property
    def identity_source(self):
        return self.id

    async def poll(self):
        return await self._poller()

    async def mark_seen(self, job):
        return await self._marker(job)

    async def backfill(self):
        return await self.poll()

    async def aclose(self):
        transport = getattr(self.http_client, "transport", None)
        if transport is not None:
            await transport.close()

    def normalize(self, project):
        return {
            "title": project["title"],
            "description": project["description"],
            "raw_text": f"{project['title']}\n\n{project['description']}",
            "source": project.get("platform", "FreeHub"),
            "budget": project.get("price", ""),
            "url": project.get("project_link", ""),
            "job_id": project["uid"],
            "identity_source": project.get("_poll_source", project.get("platform", "FreeHub")),
        }