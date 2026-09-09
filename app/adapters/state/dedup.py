"""Independent dedup ports backed by the existing JSON StateManager."""
from app.ports import DedupStore

class StateDedupStore(DedupStore):
    id = "state-json"
    def __init__(self, state_backend): self._state = state_backend
    async def claim_cross_source_project(self, project_id, job_uuid):
        return await self._state.run(self._state.claim_cross_source_project, project_id, job_uuid)
    async def get_seen(self, source):
        return await self._state.run(self._state.get_freehub_seen, source)
    async def set_seen(self, source, values):
        return await self._state.run(self._state.set_freehub_seen, source, values)

    async def get_pending(self, source):
        return await self._state.run(self._state.get_freehub_pending, source)
    async def set_pending(self, source, values):
        return await self._state.run(self._state.set_freehub_pending, source, values)
    async def get_backfill_page(self, source):
        return await self._state.run(self._state.get_freehub_backfill_page, source)
    async def set_backfill_page(self, source, page):
        return await self._state.run(self._state.set_freehub_backfill_page, source, page)
