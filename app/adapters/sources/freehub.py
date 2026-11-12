"""FreeHub JobSource adapter.

Every collaborator (HTTP client, poller, marker) is injected by the
composition root. This module never reads app.infra.config / app.infra.runtime_config
and never constructs infrastructure itself -- there is no hidden
service-locator or adapter-owned dependency construction here.
"""

from app.ports import JobSource


class FreeHubApiClient:
    """FreeHub API adapter. HTTP mechanics are delegated to an injected
    HttpTransport (app.ports). Configuration values (base_url, user_id,
    timeout, page_size) are injected by the composition root, never
    resolved from app.infra.config here."""

    def __init__(self, base_url, user_id, *, timeout=None, page_size=None, transport=None):
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.timeout = timeout
        self.page_size = page_size
        self.transport = transport

    def _require_transport(self):
        if self.transport is None:
            raise RuntimeError(
                "FreeHubApiClient requires an injected transport; construct "
                "it via app.wiring.composition.compose()."
            )
        return self.transport

    @property
    def api_root(self):
        """The API root, without the per-user ``/users`` suffix.

        The listing endpoint lives under ``.../v1/users/{id}/projects``,
        but the project-detail endpoint is the public
        ``.../v1/projects/{uid}`` (no user id), so derive it from the
        configured base URL rather than requiring a second config value.
        """
        if self.base_url.endswith("/users"):
            return self.base_url[: -len("/users")]
        return self.base_url

    async def fetch_projects(self, source, *, page=1):
        transport = self._require_transport()
        if self.page_size is None:
            raise RuntimeError(
                "FreeHubApiClient requires an injected page_size; construct "
                "it via app.wiring.composition.compose()."
            )
        url = f"{self.base_url}/{self.user_id}/projects?page={page}&page_size={self.page_size}&sort=newest&source={source}"
        return await transport.get_json(url, timeout=self.timeout)

    async def fetch_project(self, uid):
        """Fetch one project's detail payload (``/v1/projects/{uid}``)."""
        transport = self._require_transport()
        url = f"{self.api_root}/projects/{uid}"
        return await transport.get_json(url, timeout=self.timeout)

    async def fetch_text(self, url, *, headers=None):
        """Fetch a non-JSON page (e.g. the public project page as a fallback)."""
        transport = self._require_transport()
        response = await transport.request(
            "GET", url, headers=headers, timeout=self.timeout
        )
        try:
            return await response.text()
        finally:
            close = getattr(response, "aclose", None)
            if close is not None:
                await close()


class FreeHubJobSource(JobSource):
    id = "freehub"

    def __init__(self, poller, marker, http_client, description_recovery=None):
        self.http_client = http_client
        self._poller = poller
        self._marker = marker
        self._description_recovery = description_recovery

    @property
    def identity_source(self):
        return self.id

    async def poll(self):
        projects = await self._poller()
        if self._description_recovery is None:
            return projects
        # Repair upstream placeholder descriptions (client name, price,
        # empty text) before the project is persisted. See
        # app.adapters.sources.freehub_recovery for the detection rules.
        return await self._description_recovery.enrich_all(projects)

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