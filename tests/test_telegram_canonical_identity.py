"""Focused regression tests for the Telegram canonical-identity invariant.

The production startup/recovery/live path (``_run_telegram_loop`` and the
state machines it drives -- ``channel_locks``, ``recovery_blocked``,
``retry_wake``, ``failure_generation``, and the durable watermark) must key
every internal channel by ONE canonical identity: the resolved Telegram
numeric ``chat_id``.

A configured identifier is:

    configured identifier -> resolve Telegram entity -> canonical chat_id

and the canonical ``chat_id`` is then used EVERYWHERE internally. This
prevents ``"@channel"`` and ``"-1001234567890"`` naming the same Telegram
channel from creating duplicate state (two watermark keys, a split lock, a
barrier that never lines up with live events).
"""
import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest

import app.adapters.sources.telegram as telegram
from app.adapters.sources.telegram import TelegramChannelJobSource

USERNAME = "@jobs"
JOBS_ID = -10001
NUMERIC = -10002


class _FakeStateStore:
    """Durable per-channel watermark with the same str() key normalization as
    the real state store. Mirrors the async StateStore contract used by both
    the live path (via the monkeypatched telegram.state) and the poll path
    (TelegramChannelJobSource.state_store)."""

    def __init__(self):
        self.data = {}

    async def get_last_message_id(self, channel):
        return int(self.data.get(str(channel), 0))

    async def async_set_last_message_id(self, channel, message_id):
        self.data[str(channel)] = message_id


class _StubParser:
    def parse(self, source, text):
        return {"title": text, "url": ""}


class _CanonicalClient:
    """Fake Telethon client that resolves a configured username/alias to its
    canonical numeric chat id, pages messages, registers the live handler, and
    fires a scripted set of live events once connected."""

    def __init__(self, id_by_token, by_chat_id, live_events=()):
        self.id_by_token = id_by_token
        self.by_chat_id = {c: list(v) for c, v in by_chat_id.items()}
        self.live_events = list(live_events)
        self.registered_handler = None
        self.handler_chats = None

    def _resolve(self, token):
        return self.id_by_token.get(token, token)

    async def get_me(self):
        return SimpleNamespace(first_name="Test")

    async def get_dialogs(self):
        return []

    async def get_entity(self, token):
        chat_id = self.id_by_token.get(token)
        if chat_id is None:
            raise ValueError(f"unknown entity {token}")
        return SimpleNamespace(id=chat_id)

    async def iter_messages(self, channel, min_id, reverse, limit):
        resolved = self._resolve(channel)
        newer = [m for m in self.by_chat_id.get(resolved, []) if m.id > int(min_id)]
        ordered = sorted(newer, key=lambda m: m.id, reverse=not reverse)
        for m in ordered[:limit]:
            yield m

    async def get_messages(self, channel, limit=1):
        resolved = self._resolve(channel)
        msgs = self.by_chat_id.get(resolved, [])
        return [msgs[-1]] if msgs else []

    def on(self, builder):
        return builder

    async def run_until_disconnected(self):
        handler = self.registered_handler
        for ev in self.live_events:
            await handler(ev)
        return


class _LiveEvent:
    def __init__(self, eid, chat_id, title="Test Channel"):
        self.id = eid
        self.chat_id = chat_id
        self._title = title

    async def get_chat(self):
        return SimpleNamespace(title=self._title)


def _rec_msg(mid, chat_id):
    return SimpleNamespace(
        id=mid,
        chat_id=chat_id,
        raw_text=f"job {mid}",
        chat=SimpleNamespace(title=f"Channel {chat_id}"),
    )


def _patch_new_message(monkeypatch, client):
    def fake_new_message(**kwargs):
        client.handler_chats = tuple(kwargs["chats"])

        def decorator(handler):
            client.registered_handler = handler
            return handler

        return decorator

    monkeypatch.setattr(telegram.events, "NewMessage", fake_new_message)


def _patch_state_and_process(monkeypatch, store):
    async def fake_get(channel):
        return await store.get_last_message_id(channel)

    async def fake_set(chat_id, message_id):
        await store.async_set_last_message_id(chat_id, message_id)

    monkeypatch.setattr(telegram.state, "get_last_message_id", fake_get)
    monkeypatch.setattr(
        telegram.state, "async_set_last_message_id", fake_set
    )

    async def fake_process(message):
        return True

    monkeypatch.setattr(telegram, "process_message", fake_process)


def _run_loop(client, store, channels, monkeypatch):
    """Drive _run_telegram_loop with a fake client, capturing the real
    channel_locks / recovery_blocked dicts the live handler receives."""
    captured = {}

    _patch_new_message(monkeypatch, client)
    _patch_state_and_process(monkeypatch, store)

    real_handle = telegram._handle_live_message

    async def recording_handle(event, channel_locks, recovery_blocked=None, on_failure=None):
        captured["channel_locks"] = channel_locks
        captured["recovery_blocked"] = recovery_blocked
        captured["live_chat_id"] = event.chat_id
        return await real_handle(event, channel_locks, recovery_blocked, on_failure)

    monkeypatch.setattr(telegram, "_handle_live_message", recording_handle)

    asyncio.run(telegram._run_telegram_loop(client, tuple(channels)))
    return captured


# ---------------------------------------------------------------------------
# Test 1 -- Username / configured identifier.
# ---------------------------------------------------------------------------
def test_username_channel_is_canonical_everywhere(monkeypatch):
    store = _FakeStateStore()
    # Seed the durable watermark under the CANONICAL chat id, as recovery would.
    asyncio.run(store.async_set_last_message_id(JOBS_ID, 100))

    live_event = _LiveEvent(103, JOBS_ID, "Jobs Channel")
    client = _CanonicalClient(
        id_by_token={USERNAME: JOBS_ID},
        by_chat_id={JOBS_ID: [_rec_msg(101, JOBS_ID), _rec_msg(102, JOBS_ID)]},
        live_events=[live_event],
    )

    captured = _run_loop(client, store, [USERNAME], monkeypatch)

    # 1. entity resolution -> canonical chat id (used for the live handler chat
    #    filter too, so a live event's chat_id matches the internal state key).
    assert list(client.handler_chats) == [JOBS_ID], (
        "the NewMessage filter must be the canonical chat id, so event.chat_id "
        "always matches the internal state key"
    )

    # 2. Watermark is written under the canonical id (recovery read the
    #    canonical key 100, advanced through 101/102 by message.chat_id, and
    #    the later live event advanced to 103 under the SAME key) -- never
    #    under the configured username string.
    assert store.data.get(USERNAME) is None, (
        "no watermark state may exist under the raw configured string"
    )

    # 3. The recovery barrier dict passed to the live handler is keyed by the
    #    canonical id, and a live event (chat_id == canonical) touches the SAME
    #    key -- not a new key nor the configured string.
    assert USERNAME not in captured["recovery_blocked"]
    assert JOBS_ID in captured["recovery_blocked"]
    assert captured["live_chat_id"] == JOBS_ID

    # 4. The per-channel lock dict is keyed by the canonical id too, so recovery
    #    and live processing serialize on ONE lock per channel.
    assert list(captured["channel_locks"].keys()) == [JOBS_ID]
    assert USERNAME not in captured["channel_locks"]

    # 5. The live event advanced the watermark under the SAME canonical key.
    assert store.data.get(str(JOBS_ID)) == 103, (
        "the live event must advance the durable watermark under the canonical "
        "chat id, matching recovery's key"
    )
    # And there is exactly ONE state entry per channel: no duplicate under the
    # configured string.
    assert set(store.data.keys()) == {str(JOBS_ID)}


# ---------------------------------------------------------------------------
# Test 2 -- Numeric identifier behaves exactly as before.
# ---------------------------------------------------------------------------
def test_numeric_channel_is_unchanged(monkeypatch):
    store = _FakeStateStore()
    asyncio.run(store.async_set_last_message_id(NUMERIC, 100))

    live_event = _LiveEvent(203, NUMERIC, "Numeric Channel")
    client = _CanonicalClient(
        id_by_token={NUMERIC: NUMERIC},
        by_chat_id={NUMERIC: [_rec_msg(201, NUMERIC), _rec_msg(202, NUMERIC)]},
        live_events=[live_event],
    )

    captured = _run_loop(client, store, [NUMERIC], monkeypatch)

    # A numeric id resolves to itself: every key is str(NUMERIC).
    assert list(client.handler_chats) == [NUMERIC]
    assert store.data.get(str(NUMERIC)) == 203
    assert list(captured["channel_locks"].keys()) == [NUMERIC]
    assert NUMERIC in captured["recovery_blocked"]
    assert set(store.data.keys()) == {str(NUMERIC)}


# ---------------------------------------------------------------------------
# Test 3 -- Recovery/live consistency: one channel, one state.
# ---------------------------------------------------------------------------
def test_recovery_live_then_poll_mark_seen_share_one_state(monkeypatch):
    store = _FakeStateStore()
    asyncio.run(store.async_set_last_message_id(JOBS_ID, 100))

    live_event = _LiveEvent(103, JOBS_ID, "Jobs Channel")
    client = _CanonicalClient(
        id_by_token={USERNAME: JOBS_ID},
        by_chat_id={JOBS_ID: [_rec_msg(101, JOBS_ID), _rec_msg(102, JOBS_ID)]},
        live_events=[live_event],
    )

    # configured identifier -> recovery -> live event -> durable watermark.
    _run_loop(client, store, [USERNAME], monkeypatch)
    assert store.data.get(str(JOBS_ID)) == 103

    # A poll-mode source configured with the SAME username, over the SAME
    # durable store, must address the exact same channel state: it resumes from
    # the canonical watermark (min_id = 103) and its mark_seen writes under the
    # same canonical key -- no second/duplicate key.
    poll_client = _CanonicalClient(
        id_by_token={USERNAME: JOBS_ID},
        by_chat_id={JOBS_ID: [_rec_msg(104, JOBS_ID)]},
    )
    source = TelegramChannelJobSource(
        client=poll_client,
        channels=[USERNAME],
        parser=_StubParser(),
        state_store=store,
    )

    jobs = asyncio.run(source.poll())
    assert source._channel_ids[USERNAME] == str(JOBS_ID)
    assert [j["job_id"] for j in jobs] == ["104"]
    assert asyncio.run(source._last_message_id(JOBS_ID)) == 103

    asyncio.run(source.mark_seen(jobs[0]))
    assert store.data.get(str(JOBS_ID)) == 104
    assert set(store.data.keys()) == {str(JOBS_ID)}, (
        "all operations must address exactly one channel state (canonical key)"
    )


# ---------------------------------------------------------------------------
# Test 4 -- Restart/recovery consistency (canonical watermark is restored).
# ---------------------------------------------------------------------------
def test_restart_resumes_from_canonical_watermark(monkeypatch):
    store = _FakeStateStore()

    # First process: recovery + live events, watermark lands on the canonical
    # key at 303.
    live1 = _LiveEvent(303, JOBS_ID, "Jobs")
    client1 = _CanonicalClient(
        id_by_token={USERNAME: JOBS_ID},
        by_chat_id={JOBS_ID: [_rec_msg(301, JOBS_ID), _rec_msg(302, JOBS_ID)]},
        live_events=[live1],
    )
    _run_loop(client1, store, [USERNAME], monkeypatch)
    assert store.data.get(str(JOBS_ID)) == 303

    # "Restart": a brand-new client + loop share only the durable store. It must
    # resume from the canonical watermark -- nothing below 303 is reprocessed --
    # and keep writing under the same canonical key.
    client2 = _CanonicalClient(
        id_by_token={USERNAME: JOBS_ID},
        by_chat_id={
            JOBS_ID: [
                _rec_msg(301, JOBS_ID),
                _rec_msg(302, JOBS_ID),
                _rec_msg(304, JOBS_ID),
            ]
        },
    )

    recovered_calls = []

    real_iter = client2.iter_messages

    async def capturing_iter_messages(channel, min_id, reverse, limit):
        recovered_calls.append(int(min_id))
        async for m in real_iter(channel, min_id=min_id, reverse=reverse, limit=limit):
            yield m

    client2.iter_messages = capturing_iter_messages

    _run_loop(client2, store, [USERNAME], monkeypatch)

    # Recovery started from the persisted canonical watermark 303, so 301/302
    # were NOT reprocessed; only 304 (newer than 303) was recovered.
    assert 303 in recovered_calls
    assert store.data.get(str(JOBS_ID)) == 304
    assert set(store.data.keys()) == {str(JOBS_ID)}, (
        "restart must keep using the canonical watermark key and never duplicate "
        "state under the configured string"
    )


# ---------------------------------------------------------------------------
# Test 5 -- Nonnumeric username that fails entity resolution in live path.
# ---------------------------------------------------------------------------
class _FailingClient:
    """Fake client that fails get_entity() for nonnumeric tokens (simulating
    a transient network error or an entity the bot cannot access)."""

    def __init__(self, by_chat_id, live_events=()):
        self.by_chat_id = {c: list(v) for c, v in by_chat_id.items()}
        self.live_events = list(live_events)
        self.registered_handler = None
        self.handler_chats = None

    async def get_me(self):
        return SimpleNamespace(first_name="Test")

    async def get_dialogs(self):
        return []

    async def get_entity(self, token):
        try:
            int(token)
        except (TypeError, ValueError):
            raise ValueError(f"cannot resolve nonnumeric token {token!r}")
        return SimpleNamespace(id=token)

    async def iter_messages(self, channel, min_id, reverse, limit):
        newer = [m for m in self.by_chat_id.get(channel, []) if m.id > int(min_id)]
        ordered = sorted(newer, key=lambda m: m.id, reverse=not reverse)
        for m in ordered[:limit]:
            yield m

    async def get_messages(self, channel, limit=1):
        msgs = self.by_chat_id.get(channel, [])
        return [msgs[-1]] if msgs else []

    def on(self, builder):
        return builder

    async def run_until_disconnected(self):
        handler = self.registered_handler
        for ev in self.live_events:
            await handler(ev)
        return


class _AllFailClient(_FailingClient):
    """Like _FailingClient but get_entity ALWAYS fails, even for numeric
    tokens -- simulating a transient network error that prevents resolving
    any entity, or an injected test client without get_entity."""

    async def get_entity(self, token):
        raise ValueError(f"cannot resolve token {token!r}")


def test_unresolved_nonnumeric_username_creates_no_state(monkeypatch):
    """A nonnumeric channel that cannot be resolved must NOT have any
    internal state (watermark, lock, barrier, inflight) created under the
    raw configured string.  The channel is simply skipped, and the rest of
    the loop continues with no canonical channels."""
    store = _FakeStateStore()

    client = _FailingClient(by_chat_id={})

    captured = {}

    _patch_new_message(monkeypatch, client)
    _patch_state_and_process(monkeypatch, store)

    real_recover_all = telegram._recover_all_channels

    async def recording_recover_all(c, channel_locks, recovery_blocked, channels=None, *, max_messages=None):
        captured["channel_locks"] = channel_locks
        captured["recovery_blocked"] = recovery_blocked
        captured["channels"] = list(channels or ())
        return await real_recover_all(
            c, channel_locks, recovery_blocked, channels, max_messages=max_messages
        )

    monkeypatch.setattr(telegram, "_recover_all_channels", recording_recover_all)

    asyncio.run(telegram._run_telegram_loop(client, (USERNAME,)))

    # 1. The loop ran with NO canonical channels (the username was skipped).
    assert captured["channels"] == []

    # 2. No watermark state was created at all (channel was skipped).
    assert len(store.data) == 0
    assert store.data.get(USERNAME) is None, (
        "no watermark state may exist under the raw nonnumeric configured "
        "string when entity resolution fails"
    )

    # 3. No lock or barrier was created for the raw username.
    assert USERNAME not in captured.get("channel_locks", {})
    assert USERNAME not in captured.get("recovery_blocked", {})


# ---------------------------------------------------------------------------
# Test 6 -- Numeric channel that fails entity resolution still works.
# ---------------------------------------------------------------------------
def test_unresolved_numeric_id_remains_canonical(monkeypatch):
    """A numeric identifier is already the canonical chat id.  Even when
    get_entity() fails (e.g. an injected test client, or a transient
    error), the numeric ID is used as-is and all state is keyed by it."""
    store = _FakeStateStore()
    asyncio.run(store.async_set_last_message_id(NUMERIC, 50))

    # A client whose get_entity ALWAYS fails (simulating a transient network
    # error).  The numeric id must still be retained as the canonical key via
    # the numeric fallback: it already IS the chat id.
    client = _AllFailClient(by_chat_id={NUMERIC: [_rec_msg(51, NUMERIC)]})

    captured = {}

    _patch_new_message(monkeypatch, client)
    _patch_state_and_process(monkeypatch, store)

    real_recover_all = telegram._recover_all_channels

    async def recording_recover_all(c, channel_locks, recovery_blocked, channels=None, *, max_messages=None):
        captured["channel_locks"] = channel_locks
        captured["recovery_blocked"] = recovery_blocked
        captured["channels"] = list(channels or ())
        return await real_recover_all(
            c, channel_locks, recovery_blocked, channels, max_messages=max_messages
        )

    monkeypatch.setattr(telegram, "_recover_all_channels", recording_recover_all)

    asyncio.run(telegram._run_telegram_loop(client, (NUMERIC,)))

    # The numeric id is used as the canonical key even though it is not
    # resolved through get_entity: it IS the chat id already.
    assert list(client.handler_chats) == [NUMERIC]
    assert captured["channels"] == [NUMERIC]
    assert str(NUMERIC) in store.data
    assert NUMERIC in captured["recovery_blocked"]
    assert list(captured["channel_locks"].keys()) == [NUMERIC]


# ---------------------------------------------------------------------------
# Test 7 -- Poll-mode: nonnumeric channel fails resolution, no state created.
# ---------------------------------------------------------------------------
def test_poll_skips_unresolved_nonnumeric_channel():
    """When poll-mode _resolve_channel_id returns None for a nonnumeric
    channel, poll() skips it entirely: no _inflight entry, no watermark
    read/write, and no job is emitted under the raw string."""
    store = _FakeStateStore()

    # A client that fails get_entity for nonnumeric tokens.
    class _PollFailingClient:
        async def get_entity(self, token):
            try:
                int(token)
            except (TypeError, ValueError):
                raise ValueError(f"cannot resolve {token!r}")

        async def iter_messages(self, channel, min_id, reverse, limit):
            return
            yield  # pragma: no cover

    source = TelegramChannelJobSource(
        client=_PollFailingClient(),
        channels=[USERNAME],
        parser=_StubParser(),
        state_store=store,
    )

    jobs = asyncio.run(source.poll())

    # No jobs returned -- the channel was skipped.
    assert jobs == []

    # No state created under the raw username.
    assert store.data.get(USERNAME) is None
    assert len(store.data) == 0

    # No inflight entry under the raw username.
    assert USERNAME not in source._inflight

    # The resolution was NOT cached (so next poll retries).
    assert USERNAME not in source._channel_ids


# ---------------------------------------------------------------------------
# Test 8 -- Poll-mode: numeric channel fails resolution, still usable.
# ---------------------------------------------------------------------------
def test_poll_keeps_unresolved_numeric_id_as_canonical():
    """A numeric channel that fails get_entity (e.g. test client without
    it) falls back to the configured numeric string, which is already the
    canonical chat id.  poll() proceeds normally under that key."""
    store = _FakeStateStore()
    msg = _rec_msg(42, NUMERIC)

    class _PollNumericClient:
        async def get_entity(self, token):
            raise ValueError("no get_entity in test client")

        async def iter_messages(self, channel, min_id, reverse, limit):
            newer = [m for m in [msg] if m.id > int(min_id)]
            for m in sorted(newer, key=lambda m: m.id, reverse=not reverse):
                yield m

    source = TelegramChannelJobSource(
        client=_PollNumericClient(),
        channels=[NUMERIC],
        parser=_StubParser(),
        state_store=store,
    )

    jobs = asyncio.run(source.poll())

    # The numeric channel was resolved to itself (the fallback).
    assert source._channel_ids[NUMERIC] == str(NUMERIC)

    # Jobs are emitted with the canonical numeric identity.
    assert len(jobs) == 1
    assert jobs[0]["identity_source"] == str(NUMERIC)

    # Inflight is keyed by the canonical numeric id.
    assert str(NUMERIC) in source._inflight
    assert list(source._inflight[str(NUMERIC)]) == [42]

    # mark_seen works under the canonical key.
    asyncio.run(source.mark_seen(jobs[0]))
    assert store.data.get(str(NUMERIC)) == 42
