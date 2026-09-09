"""Regression tests for the generic Telegram channel JobSource adapter.

Guards the audit finding that the previous version used the Telethon API's
default newest-first iteration directly: a watermark persisted from a
newest-confirmed message would either never advance (penalizing duration) or
become a fragile window. This adapter is the generic-JobSource counterpart to
the dedicated handler's barrier guarantee, with three invariants:

* poll() returns messages strictly newer than the durable per-channel
  watermark, in OLDEST->NEWEST order, regardless of the API's newest-first
  default (it always requests ``iter_messages(..., reverse=True)``).
* The watermark only moves forward, and only through *contiguous*
  confirmations: an earlier failed/idle message can never be skipped over by a
  later successful one.
* Polls stay bounded by the same 2,000-message recovery cap as the dedicated
  handler; a gap that is only partially confirmed is re-fetched from the held
  watermark on the next poll.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.sources.telegram import DEFAULT_BATCH_LIMIT, TelegramChannelJobSource

CHANNEL = -1001
CHANNEL_2 = -1002


class _FakeStateStore:
    """Durable per-channel watermark with the same str() key normalization as
    the real state store."""
    def __init__(self):
        self.data = {}

    async def get_last_message_id(self, channel):
        return int(self.data.get(str(channel), 0))

    async def async_set_last_message_id(self, channel, message_id):
        self.data[str(channel)] = message_id


class _StubParser:
    def parse(self, source, text):
        return {"title": text, "url": ""}


class _FakeClient:
    """Telethon stand-in that reproduces the API's real behavior: by default it
    yields newest-first; it only returns ascending order when asked for
    ``reverse=True``. Records every call for assertion."""
    def __init__(self, by_channel):
        self.by_channel = {c: list(v) for c, v in by_channel.items()}
        self.calls = []

    async def iter_messages(self, channel, min_id, reverse, limit):
        self.calls.append(
            {"channel": channel, "min_id": min_id, "reverse": reverse, "limit": limit}
        )
        newer = [m for m in self.by_channel.get(channel, []) if m.id > min_id]
        ordered = sorted(newer, key=lambda m: m.id, reverse=not reverse)
        for m in ordered[:limit]:
            yield m


def _msg(mid, chat_id=CHANNEL):
    return SimpleNamespace(
        id=mid,
        chat_id=chat_id,
        raw_text=f"job {mid}",
        chat=SimpleNamespace(title=f"Channel {chat_id}"),
    )


def _job(identity_source, job_id):
    return {"identity_source": identity_source, "job_id": job_id}


def _build(client, store, channels=(CHANNEL,), **kwargs):
    return TelegramChannelJobSource(
        client=client,
        channels=channels,
        parser=_StubParser(),
        state_store=store,
        **kwargs,
    )


def test_batch_limit_defaults_to_two_thousand():
    source = TelegramChannelJobSource(
        channels=[], parser=_StubParser(), state_store=_FakeStateStore()
    )
    assert DEFAULT_BATCH_LIMIT == 2000
    assert source.batch_limit == 2000


def test_normalize_is_identity():
    source = TelegramChannelJobSource(
        channels=[], parser=_StubParser(), state_store=_FakeStateStore()
    )
    job = {"job_id": "1", "identity_source": "x"}
    assert source.normalize(job) is job


def test_poll_requests_reverse_and_emits_oldest_to_newest():
    client = _FakeClient({CHANNEL: [_msg(101), _msg(103), _msg(104)]})
    store = _FakeStateStore()
    source = _build(client, store)
    jobs = asyncio.run(source.poll())

    assert [j["job_id"] for j in jobs] == ["101", "103", "104"]
    call = client.calls[0]
    assert call["reverse"] is True
    assert call["min_id"] <= 0
    assert call["limit"] == 2000
    # parsed fields are pipeline-shaped and identity-stable
    assert jobs[0]["identity_source"] == str(CHANNEL)
    assert jobs[0]["source"] == f"Channel {CHANNEL}"


def test_watermark_only_advances_through_contiguous_confirmations():
    client = _FakeClient({CHANNEL: [_msg(101), _msg(102), _msg(103)]})
    store = _FakeStateStore()
    source = _build(client, store)
    asyncio.run(source.poll())

    # Confirming a later message before its predecessors must NOT move the
    # watermark: the failed/held predecessor would be permanently skipped.
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "102")))
    assert store.data.get(str(CHANNEL)) is None

    # The true head is confirmed first: the watermark advances exactly to it.
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "101")))
    assert store.data[str(CHANNEL)] == 101

    asyncio.run(source.mark_seen(_job(str(CHANNEL), "102")))
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "103")))
    assert store.data[str(CHANNEL)] == 103


def test_unconfirmed_gap_is_refetched_from_the_held_watermark():
    client = _FakeClient({CHANNEL: [_msg(101), _msg(102), _msg(103)]})
    store = _FakeStateStore()
    source = _build(client, store)
    asyncio.run(source.poll())
    # Only 101 is confirmed before the next poll; 102 was skipped/failed.
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "101")))

    next_jobs = asyncio.run(source.poll())
    # Held watermark stays at 101, so the unconfirmed 102 is re-fetched.
    assert store.data[str(CHANNEL)] == 101
    assert [j["job_id"] for j in next_jobs] == ["102", "103"]
    assert client.calls[1]["min_id"] == 101
    # Batch is re-registered with the unconfirmed tail as its head.
    assert list(source._inflight[str(CHANNEL)]) == [102, 103]


def test_poll_never_reprocesses_messages_at_or_below_the_watermark():
    client = _FakeClient({CHANNEL: [_msg(101), _msg(102), _msg(103)]})
    store = _FakeStateStore()
    source = _build(client, store)
    asyncio.run(source.poll())
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "101")))
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "102")))

    jobs = asyncio.run(source.poll())
    # min_id == last confirmed id: only strictly-newer messages are returned.
    assert client.calls[1]["min_id"] == 102
    assert [j["job_id"] for j in jobs] == ["103"]


def test_watermark_persists_across_source_restart():
    client = _FakeClient({CHANNEL: [_msg(101), _msg(102)]})
    store = _FakeStateStore()
    first = _build(client, store)
    asyncio.run(first.poll())
    asyncio.run(first.mark_seen(_job(str(CHANNEL), "101")))
    asyncio.run(first.mark_seen(_job(str(CHANNEL), "102")))

    # A brand-new source instance shares only the durable store: it must not
    # refetch anything at or below the persisted watermark.
    second_client = _FakeClient({CHANNEL: [_msg(101), _msg(102), _msg(103)]})
    second = _build(second_client, store)
    jobs = asyncio.run(second.poll())
    assert second_client.calls[0]["min_id"] == 102
    assert [j["job_id"] for j in jobs] == ["103"]


def test_mark_seen_never_writes_a_regressing_watermark():
    store = _FakeStateStore()
    store.data[str(CHANNEL)] = 300
    client = _FakeClient({CHANNEL: [_msg(200)]})
    source = _build(client, store)
    asyncio.run(source.poll())
    asyncio.run(source.mark_seen(_job(str(CHANNEL), "200")))
    assert store.data[str(CHANNEL)] == 300


def test_mark_seen_without_an_active_batch_is_a_safe_noop():
    store = _FakeStateStore()
    client = _FakeClient({CHANNEL: []})
    source = _build(client, store)
    asyncio.run(source.poll())
    assert asyncio.run(source.mark_seen(_job(str(CHANNEL), "999"))) is None
    assert store.data.get(str(CHANNEL)) is None


def test_multiple_channels_track_independent_watermarks():
    client = _FakeClient(
        {CHANNEL: [_msg(1, CHANNEL), _msg(2, CHANNEL)], CHANNEL_2: [_msg(7, CHANNEL_2)]}
    )
    store = _FakeStateStore()
    source = _build(client, store, channels=(CHANNEL, CHANNEL_2))

    jobs = asyncio.run(source.poll())
    assert [j["job_id"] for j in jobs] == ["1", "2", "7"]
    assert len(client.calls) == 2

    asyncio.run(source.mark_seen(_job(str(CHANNEL), "1")))
    assert store.data.get(str(CHANNEL)) == 1
    assert store.data.get(str(CHANNEL_2)) is None

    asyncio.run(source.mark_seen(_job(str(CHANNEL_2), "7")))
    assert store.data[str(CHANNEL_2)] == 7


def test_identity_source_uses_the_stable_numeric_chat_id():
    client = _FakeClient({CHANNEL: [_msg(101)]})
    source = _build(client, _FakeStateStore())
    jobs = asyncio.run(source.poll())
    assert jobs[0]["identity_source"] == str(CHANNEL)
    assert source.identity_source == str(CHANNEL)


def test_poll_requires_an_injected_parser():
    source = TelegramChannelJobSource(
        client=_FakeClient({CHANNEL: [_msg(101)]}),
        channels=[CHANNEL],
        parser=None,
        state_store=_FakeStateStore(),
    )
    with pytest.raises(RuntimeError, match="parser"):
        asyncio.run(source.poll())


def test_poll_requires_an_injected_state_store():
    source = TelegramChannelJobSource(
        client=_FakeClient({CHANNEL: [_msg(101)]}),
        channels=[CHANNEL],
        parser=_StubParser(),
        state_store=None,
    )
    with pytest.raises(RuntimeError, match="state_store"):
        asyncio.run(source.poll())


def test_source_without_credentials_or_client_raises_a_clear_error():
    source = TelegramChannelJobSource(
        client=None, channels=[CHANNEL], parser=_StubParser(), state_store=_FakeStateStore()
    )
    with pytest.raises(RuntimeError, match="api_id/api_hash"):
        asyncio.run(source.poll())


class _ResolvingClient(_FakeClient):
    """Adds get_entity() so a configured username/alias can be resolved to
    its stable numeric chat id, exercising the canonical-identity path."""

    def __init__(self, by_channel, id_by_token):
        super().__init__(by_channel)
        self._id_by_token = id_by_token

    def _resolve(self, channel):
        return self._id_by_token.get(channel, channel)

    async def iter_messages(self, channel, min_id, reverse, limit):
        # Like Telethon, resolve a configured username/alias token to its
        # numeric chat id before iterating its messages.
        resolved = self._resolve(channel)
        gen = _FakeClient.iter_messages(
            self, resolved, min_id=min_id, reverse=reverse, limit=limit
        )
        async for m in gen:
            yield m

    async def get_entity(self, token):
        chat_id = self._id_by_token.get(token)
        if chat_id is None:
            raise ValueError(f"unknown entity {token}")
        return SimpleNamespace(id=chat_id)


USERNAME = "@jobs"
USERNAME_ID = -10001


def _resolving_source(**kwargs):
    client = _ResolvingClient(
        {USERNAME_ID: [_msg(5, USERNAME_ID), _msg(6, USERNAME_ID)]},
        id_by_token={USERNAME: USERNAME_ID},
    )
    return client, _build(client, _FakeStateStore(), channels=(USERNAME,), **kwargs)


def test_username_channel_resolves_to_numeric_chat_id_for_identity():
    client, source = _resolving_source()
    jobs = asyncio.run(source.poll())

    # The configured token is a username, but the canonical identity used
    # everywhere downstream is the stable numeric chat id.
    assert [j["job_id"] for j in jobs] == ["5", "6"]
    assert jobs[0]["identity_source"] == str(USERNAME_ID)
    # The inflight batch is keyed by the same canonical id.
    assert list(source._inflight[str(USERNAME_ID)]) == [5, 6]


def test_username_channel_mark_seen_confirms_under_the_resolved_identity():
    client, source = _resolving_source()
    asyncio.run(source.poll())

    # mark_seen looks up the batch by identity_source (the resolved numeric
    # id), NOT by the configured username, so the waterfall confirms and the
    # durable watermark advances.
    store = source.state_store
    asyncio.run(source.mark_seen(_job(str(USERNAME_ID), "5")))
    assert store.data.get(str(USERNAME_ID)) == 5
    # And no stale key is written under the username token.
    assert store.data.get(USERNAME) is None

    asyncio.run(source.mark_seen(_job(str(USERNAME_ID), "6")))
    assert store.data[str(USERNAME_ID)] == 6
    assert list(source._inflight[str(USERNAME_ID)]) == []


def test_inflight_key_matches_identity_source_when_alias_and_chat_id_differ():
    # Configure the channel under an alias that is neither the numeric id.
    # The canonical path guarantees identity_source and the _inflight key
    # are the SAME string, so a later mark_seen always finds its batch.
    client, source = _resolving_source()
    asyncio.run(source.poll())
    assert source._channel_ids[USERNAME] == str(USERNAME_ID)
    batch_key = str(USERNAME_ID)
    job = _job(str(USERNAME_ID), "5")
    assert job["identity_source"] == batch_key
    assert batch_key in source._inflight