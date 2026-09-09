from app.ports import StateStore, DedupStore


class JsonStateStore(StateStore, DedupStore):
    id = "json"

    def __init__(self, state_backend):
        self._state = state_backend

    def load(self):
        return self._state.load()

    async def get_last_message_id(self, channel_id):
        return await self._state.run(self._state.get_last_message_id, channel_id)

    async def async_set_last_message_id(self, channel_id, message_id):
        return await self._state.async_set_last_message_id(channel_id, message_id)

    async def get_freehub_pending(self, source):
        return await self._state.run(self._state.get_freehub_pending, source)

    async def async_set_freehub_pending(self, source, values):
        return await self._state.async_set_freehub_pending(source, values)

    async def get_freehub_backfill_page(self, source):
        return await self._state.run(self._state.get_freehub_backfill_page, source)

    async def async_set_freehub_backfill_page(self, source, page):
        return await self._state.async_set_freehub_backfill_page(source, page)

    async def claim_cross_source_project(self, project_id, job_uuid):
        return await self._state.run(
            self._state.claim_cross_source_project, project_id, job_uuid
        )

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
        # Regression fix (audit finding P2-1): this previously passed
        # the *async* async_set_freehub_backfill_page coroutine
        # function into self._state.run(), which executes its
        # argument as a plain blocking callable in the state executor
        # thread. Calling an async function that way only ever
        # constructs and immediately discards a coroutine object --
        # its body (the actual write) never runs, so the page was
        # silently never persisted through this adapter. Delegate to
        # the synchronous set_freehub_backfill_page via run(),
        # matching every other set_*/get_* method on this class
        # (e.g. set_pending above).
        return await self._state.run(self._state.set_freehub_backfill_page, source, page)
