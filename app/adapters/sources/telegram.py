"""Canonical Telegram production implementation.

This module is the single home for every Telegram ingestion path:

* ``TelegramChannelJobSource`` -- the pluggable JobSource adapter
  (``poll()`` -> ``mark_seen()`` contiguous watermark semantics).
* The live + startup-recovery + bounded-retry + per-channel-barrier
  machinery previously maintained in ``app.handlers.telegram``
  (``_warm_entity_cache``, ``_recover_channel``,
  ``_handle_live_message``, ``_retry_recovery_channel``,
  ``_recover_all_channels``, ``_run_telegram_loop``).
* ``TelegramChannelWorker`` -- the production worker driven by the
  worker registry over a TelegramChannelJobSource the composition root
  constructs with all collaborators injected.

Configuration contract: this module has NO application/config-global
dependencies. It never imports ``app.config`` or ``app.runtime_config``:
every Telegram credential, channel set, and recovery option is injected by
the composition root (app.composition.compose) and flows down through
``TelegramChannelJobSource`` / ``TelegramChannelWorker``.
``app.handlers.telegram`` is the compatibility seam: it re-exports this
module's machinery and owns only the config-reading ``start()`` /
``build_telegram_source()`` delegators. There is exactly ONE Telegram
implementation here.

Watermark semantics (unlike the raw newest-first API iteration the previous
version used):

* poll() returns messages strictly newer than the durable per-channel
  watermark, in OLDEST->NEWEST order, regardless of the Telegram API's
  default newest-first ordering (``iter_messages(reverse=True)``).
* The watermark only moves forward: mark_seen() raises it to a confirmed
  message id exclusively when that message is the contiguous head of the
  current batch. A failed earlier message therefore can never be skipped over
  by a later successful one (matching the barrier guarantee the live
  recovery machinery enforces), so a batch can never regress or jump the
  watermark.
* Each poll is bounded by the same 2,000-message recovery cap the live
  recovery machinery uses, so a large backlog drains in bounded, idempotent
  chunks.
* A batch whose tail is only partially confirmed leaves the watermark below
  the gap; the next poll re-fetches the gap plus everything after it, and
  SQLite job dedup makes the re-processing a no-op.

The live recovery machinery (`_recover_channel` / `_recover_all_channels`
/ `_retry_recovery_channel`) distinguishes three outcomes per pass --
COMPLETE (caught up), CAPPED (hit the 2,000 cap, more may remain) and
FAILED (a message failed) -- and only releases a channel's live barrier on
COMPLETE. This is the recovery cap/barrier invariant: reaching the 2,000
safety cap is NOT proof the channel has caught up, so the barrier stays
closed until a later pass confirms completion, guaranteeing a live message
can never advance the durable watermark past still-unrecovered messages.

The live path (``_recover_channel`` / ``_handle_live_message`` /
``_retry_recovery_channel`` / ``_recover_all_channels`` /
``_run_telegram_loop``) preserves every existing Telegram guarantee:
startup recovery in oldest->newest order, live-message per-channel
serialization, startup race protection (locks acquired before the live
handler is registered), the shared ``recovery_blocked`` barrier across
recovery and live failures, generation-guarded retry watchers, bounded
backoff, and deterministic shutdown of the retry watchers.
"""

import asyncio
import re
import time as _time
from collections import defaultdict, deque

from telethon import TelegramClient, events, errors as _errors

from app.dependencies import logger, state
from app.message_processor import process_message
from app.ports import JobSource
from app.timeouts import call_with_timeout, iter_with_timeout
from app.heartbeat import (
    liveness,
    STATE_ALIVE,
    STATE_RECONNECTING,
)

# Config-free defaults for the recovery options the composition root
# injects. These match the project defaults (config/project.json), so a
# directly-constructed source behaves identically to a composed one.
DEFAULT_RECOVERY_MAX_MESSAGES = 2000
DEFAULT_RECOVERY_RETRY_BASE_SECONDS = 5.0
DEFAULT_RECOVERY_RETRY_CAP_SECONDS = 300.0

# The same bounded-recovery cap the live recovery machinery uses
# (DEFAULT_RECOVERY_MAX_MESSAGES). Kept identical so the poll-mode
# JobSource and the live path share one recovery-cost envelope, and a
# single poll can never walk an unbounded backlog in one API call.
DEFAULT_BATCH_LIMIT = 2000

# ---------------------------------------------------------------------------
# Recovery outcome vocabulary.
#
# A recovery pass over one channel returns one of these three states. The
# distinction between COMPLETE and CAPPED is the heart of the recovery
# cap/barrier invariant (see _recover_channel): reaching the 2,000-message
# safety cap means the channel has NOT necessarily caught up -- there may
# still be unrecovered messages newer than the watermark. Only a COMPLETE
# pass (fewer messages fetched than the cap, i.e. the channel is actually
# drained) may release the channel's live barrier. A CAPPED or FAILED pass
# must keep the barrier closed so live messages can never advance the
# durable watermark past messages that are still potentially unrecovered.
#
#   RECOVERING
#     |
#     +--> COMPLETE --> release live barrier (channel caught up)
#     |
#     +--> CAPPED   --> keep channel blocked (another batch remains; must
#                       continue from the durable watermark next pass)
#     |
#     +--> FAILED   --> keep channel blocked (a message failed; must retry
#                       from the held watermark)
# ---------------------------------------------------------------------------
RECOVERED_COMPLETE = "complete"
RECOVERED_CAPPED = "capped"
RECOVERED_FAILED = "failed"

# ---------------------------------------------------------------------------
# Telegram worker reconnection (BUG #1).
#
# When run_until_disconnected() returns (network hiccup, Telethon
# transient error), the worker must reconnect with bounded exponential
# backoff instead of silently dying. Fatal errors (auth/session --
# classified by explicit Telethon exception type, with a narrow RPC-code
# text fallback; see _is_fatal_telethon_error) propagate so TaskGroup
# shuts down the process.
# ---------------------------------------------------------------------------
_RECONNECT_BASE_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 60.0
_RECONNECT_MAX_ATTEMPTS = 0  # 0 = unlimited (bounded only by backoff cap)

# Config-free default for the worker's idle liveness cadence. While the
# live loop is connected, _run_telegram_loop() blocks inside Telethon's
# run_until_disconnected() waiting for events, so there is no per-message
# beat to report progress during long idle stretches. The worker runs a
# companion task that beats liveness on this cadence so the healthcheck's
# staleness window (derived from the heartbeat snapshot interval) can
# distinguish "connected and waiting" from "task present but frozen".
# The composition root injects RUNTIME.heartbeat_interval_seconds via
# TelegramChannelJobSource.liveness_beat_interval_seconds so the beat stays
# coherent with the healthcheck's threshold.
DEFAULT_LIVENESS_BEAT_SECONDS = 15.0

# ---------------------------------------------------------------------------
# Telegram error classification.
#
# The worker must distinguish genuinely-unrecoverable authentication /
# session errors (propagate -> TaskGroup shutdown) from transient network,
# server, flood/rate-limit and generic RPC failures (clean up -> bounded
# reconnect). Classification is explicit exception-TYPE based first --
# Telethon models these with a stable hierarchy (RPCError base, with
# UnauthorizedError/AuthKeyError grouping the credential/session failures
# and FloodError grouping the rate-limit family) -- and only falls back to
# message text for errors Telethon raises without a typed wrapper (e.g. a
# raw ConnectionError whose string is an RPC error code).
#
# Text fallback matches the exact, known Telegram RPC error CODES that are
# unrecoverable, never bare substrings like "auth" or "session": a flood
# wait like FloodTestPhoneWaitError (class name contains "phone") or a
# message mentioning "session" in another context must NOT be treated as a
# credential failure -- reconnecting after a rate limit / server error is
# exactly what the worker is supposed to do.
# ---------------------------------------------------------------------------


# Exception types that are provably unrecoverable; reconnecting cannot help
# and only an operator can fix them:
#   * UnauthorizedError -- covers the SessionExpired/SessionRevoked/
#     SessionPasswordNeeded/AuthKey*(un)registered subclasses: the session
#     auth is broken.
#   * AuthKeyError       -- the raw session/auth-key crypto state is broken.
#   * ForbiddenError     -- the account has no access to a chat/channel
#     (e.g. ChatForbiddenError). Previously this family was caught by the
#     old "forbidden" substring; here it is caught by explicit type, with
#     no false positives from a transient message that merely mentions the
#     word.
#   * ApiIdInvalidError / AccessToken*   -- the configured identity is
#     invalid.
#   * The listed channel-read denial errors: Telethon raises these under
#     BadRequestError (not ForbiddenError) but they equally mean the read
#     is permanently blocked until an operator changes membership/config.
# Every other RPC error family (FloodError, ServerError, TimedOutError,
# generic RPCError, the migrate/*retry hints) and every network error is
# transient and left to the bounded reconnect.
_UNRECOVERABLE_EXC_TYPES = (
    _errors.UnauthorizedError,
    _errors.AuthKeyError,
    _errors.ForbiddenError,
    _errors.ApiIdInvalidError,
    _errors.AccessTokenExpiredError,
    _errors.AccessTokenInvalidError,
    _errors.UserKickedError,
    _errors.UserBannedInChannelError,
    _errors.ChannelPrivateError,
    _errors.ChannelInvalidError,
    _errors.ChannelParicipantMissingError,
    _errors.UserBlockedError,
)

# Fallback: exact unrecoverable Telegram RPC codes, matched against the
# exception's text (uppercased, spaces normalized to underscores) only when
# the exception is not one of the typed classes above. Slanting towards
# specificity on purpose: a false NEGATIVE here just costs a reconnect; a
# false POSITIVE crashes the whole application on a transient failure.
_UNRECOVERABLE_RPC_CODES = frozenset({
    # auth-key / session-key failures
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_PERM_EMPTY",
    "AUTH_KEY_DUPLICATED",
    "TEMP_AUTH_KEY_ALREADY_BOUND",
    "TEMP_AUTH_KEY_EMPTY",
    # session status
    "SESSION_EXPIRED",
    "SESSION_REVOKED",
    "SESSION_PASSWORD_NEEDED",
    "SESSION_TOO_FRESH",
    "PASSWORD_HASH_INVALID",
    "PASSWORD_REQUIRED",
    "PASSWORD_TOO_FRESH",
    # configured identity invalid
    "API_ID_INVALID",
    "API_ID_PUBLISHED_FLOOD",
    "ACCESS_TOKEN_INVALID",
    "ACCESS_TOKEN_EXPIRED",
    "AUTH_TOKEN_INVALID",
    "AUTH_TOKEN_EXPIRED",
    "AUTH_TOKEN_INVALID2",
    # phone number invalid/banned (must be re-provisioned by an operator)
    "PHONE_NUMBER_BANNED",
    "PHONE_NUMBER_INVALID",
    "PHONE_NUMBER_OCCUPIED",
    "PHONE_NUMBER_UNOCCUPIED",
    "PHONE_PASSWORD_PROTECTED",
    "PHONE_CODE_INVALID",
    "PHONE_CODE_EXPIRED",
    "AUTH_BYTES_INVALID",
    # account disabled / removed
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    "TWO_FA_CONFIRM_WAIT",
})


def _is_fatal_telethon_error(exc):
    """True only for genuinely-unrecoverable auth/session/identity errors.

    Type-based classification first (the Telethon hierarchy is the
    authoritative signal); message-text fallback second, matching known
    unrecoverable RPC error codes rather than bare substrings. Everything
    else -- network errors, server errors (incl. AuthRestartError), the
    flood/rate-limit family, timeouts, Telethon's shutdown read errors --
    returns False so the worker reconnects instead of killing the process.
    """
    if isinstance(exc, _UNRECOVERABLE_EXC_TYPES):
        return True
    try:
        text = str(exc).upper().replace(" ", "_")
    except Exception:
        return False
    return any(code in text for code in _UNRECOVERABLE_RPC_CODES)


_BUTTON_URL_RE = re.compile(r"^(?:https?)://[^\s]+$")


async def _warm_entity_cache(client, channels):
    """
    Fresh sessions (e.g. a container's first boot) carry no cached
    entities, so Telethon cannot resolve a numeric channel ID on its
    own -- it needs the channel's access_hash, which is only obtained
    from a previous fetch. messages.GetDialogs returns every chat the
    account is in; that caches each monitored channel's hash in memory
    and persists it to the session file, so recovery's
    client.get_messages() can resolve. Locally this was always a no-op
    because the long-lived session file already had the channels
    cached.
    """

    await call_with_timeout(client.get_dialogs(), label="Telegram get_dialogs()")

    missing = []

    for channel in channels:
        try:
            await call_with_timeout(
                client.get_entity(channel), label=f"Telegram get_entity({channel!r})"
            )
        except ValueError as e:
            missing.append((channel, str(e)))

    if missing:
        for channel, error in missing:
            print(f"[ERROR] Cannot resolve monitored channel {channel}: {error}")

        print(
            "Is the Loki account a member of every channel in "
            "TARGET_CHANNEL_IDS? A private channel can only be resolved "
            "by numeric ID when the account is in it."
        )

        print(
            "An unresolvable channel is left unavailable for this run: "
            "no internal state is created for it (no lock/barrier/watermark "
            "under the raw configured string), and it will be retried on "
            "the next startup. Other channels are unaffected."
        )


async def _resolve_channel_ids(client, channels):
    """Resolve every configured Telegram channel identifier to its canonical
    numeric chat id (the Telethon entity id), preserving order.

    This is the production startup/recovery/live-path counterpart to
    ``TelegramChannelJobSource._resolve_channel_id`` (used by the poll-mode
    JobSource). It is called after ``_warm_entity_cache`` has already cached
    every monitored channel's access hash, so ``client.get_entity`` can
    resolve a username/alias to its numeric chat id just as reliably as it
    resolves a numeric id to itself.

    Returning the canonical numeric chat id for every channel is what lets
    the live loop key its whole channel state machine (``channel_locks``,
    ``recovery_blocked``, ``retry_wake``, ``failure_generation``) and the
    durable watermark by ONE canonical identity. A configured numeric id
    resolves to itself (the existing numeric deployment is unchanged); a
    configured username/alias resolves to the numeric chat id it denotes, so
    ``"@channel"`` and ``"-1001234567890"`` cannot create duplicate state
    when they name the same Telegram channel.

    A nonnumeric channel that cannot be resolved (e.g. transient network
    error) is **skipped** rather than falling back to the raw configured
    string: using ``"@channel"`` as an internal key would create a
    split-state where recovery keys one identity and live events fire under
    another.  A numeric identifier is already canonical and is retained even
    when get_entity fails.  Skipped channels remain unavailable and are
    retried on the next startup via the existing retry/recovery mechanism.
    """
    if not channels:
        return []
    resolved_ids = []
    for channel in channels:
        try:
            chat = await call_with_timeout(
                client.get_entity(channel),
                label=f"Telegram get_entity({channel!r})",
            )
            resolved_ids.append(chat.id)
        except Exception:
            # A numeric identifier is already the canonical chat id and
            # can be used as-is even when get_entity fails (e.g. an
            # injected test client without get_entity, or a transient
            # network error).  A nonnumeric username/alias MUST NOT fall
            # back to the raw configured string: live Telegram events
            # carry a numeric chat_id, so using "@channel" as an internal
            # key would create a split-state where recovery keys one
            # identity and live events fire under another.  Skip
            # nonnumeric channels that cannot be resolved -- they remain
            # unavailable and will be retried on the next startup.
            try:
                int(channel)
                resolved_ids.append(channel)
            except (TypeError, ValueError):
                print(
                    f"[WARNING] Cannot resolve nonnumeric channel "
                    f"identifier {channel!r}; skipping. The channel "
                    f"will be retried on next startup."
                )
    return resolved_ids


async def _recover_channel(client, channel, *, max_messages=None):
    """
    Process every message newer than the last one we recorded for
    this channel, not just the most recent 40. The previous
    `limit=40` meant that if a channel received more than 40 messages
    while the bot was offline, everything older than that window was
    silently skipped forever (state still jumped forward to the
    newest of the 40 fetched, so those messages could never be
    recovered on a later run either).

    A channel with no recorded state yet (last_id == 0, i.e. this is
    the first time we've ever tracked it) is seeded from the current
    newest message instead of walking its full history -- we have no
    natural stopping point for "how far back is a missed message"
    the very first time we see a channel.

    `max_messages` bounds this recovery backfill (the composition root
    injects RECOVERY.telegram_max_messages); it defaults to the same
    safety cap as the poll-mode JobSource.

    Returns one of RECOVERED_COMPLETE / RECOVERED_CAPPED /
    RECOVERED_FAILED (see the outcome vocabulary above):

    * COMPLETE -- every message newer than the watermark was processed
      and confirmed, and fewer than `max_messages` were fetched. The
      channel has actually caught up, so the live barrier may be
      released.
    * CAPPED   -- a full `max_messages` batch was fetched and processed
      without any failure, but the cap was reached, so there may still
      be unrecovered messages newer than the watermark. The channel is
      NOT caught up: the live barrier must stay closed and a further
      recovery pass must continue from the durable watermark.
    * FAILED   -- a message failed to process or raised. The watermark
      is left before the failed message and the channel must stay
      blocked for a retry.

    The watermark only advances through messages confirmed in order, so
    both CAPPED and FAILED resumes safely from the durable position on
    the next pass regardless of how many passes precede it.
    """

    max_messages = (
        DEFAULT_RECOVERY_MAX_MESSAGES if max_messages is None else int(max_messages)
    )
    last_id = await state.get_last_message_id(channel)

    if last_id == 0:

        newest = await call_with_timeout(
            client.get_messages(channel, limit=1),
            label=f"Telegram get_messages({channel!r}, limit=1)",
        )

        if newest:
            await state.async_set_last_message_id(channel, newest[0].id)

        print(f"[RECOVERY] {channel}: first run, seeded (no backfill)")
        return RECOVERED_COMPLETE

    messages = []

    async for message in iter_with_timeout(
        client.iter_messages(
            channel,
            min_id=last_id,
            reverse=True,  # oldest -> newest
            limit=max_messages,
        ),
        label=f"Telegram iter_messages({channel!r})",
    ):
        messages.append(message)

    new_messages = 0

    for message in messages:

        try:

            processed = await process_message(message)

            if not processed:
                # Recovery is oldest -> newest. Never advance past a
                # failed message or a later message could make the
                # failed one permanently unrecoverable.
                print(
                    f"[RECOVERY ERROR] "
                    f"Message {message.id} failed; "
                    f"stopping recovery at this watermark."
                )
                return RECOVERED_FAILED

            await state.async_set_last_message_id(
                message.chat_id,
                message.id,
            )

            new_messages += 1

        except Exception as e:

            await logger.log_error("StartupRecovery", e)

            print(
                f"[RECOVERY ERROR] "
                f"Message {message.id}: {e}"
            )

            # Leave the watermark before the failed message and retry
            # it on the next recovery run. Returning FAILED also tells
            # the startup coordinator to keep this channel's recovery
            # barrier active so live messages cannot advance its
            # watermark past the failed message.
            return RECOVERED_FAILED

    if len(messages) >= max_messages:
        # A full cap-sized batch was processed with no failure, but the
        # cap is not proof the channel has caught up: there may still be
        # unrecovered messages newer than the watermark. This is a
        # CAPPED (incomplete) recovery -- the live barrier must NOT be
        # released. The next recovery pass continues from the durable
        # watermark and drains the next batch.
        print(
            f"[RECOVERY WARNING] {channel}: hit the "
            f"{max_messages}-message safety cap -- there may "
            f"still be unrecovered messages newer than what was just "
            f"processed. The channel remains blocked until a later "
            f"recovery pass confirms it has caught up."
        )
        return RECOVERED_CAPPED

    print(
        f"[RECOVERY] {channel}: "
        f"{new_messages} new message(s)"
    )

    return RECOVERED_COMPLETE


async def _handle_live_message(event, channel_locks, recovery_blocked=None, on_failure=None):
    """
    Process one live NewMessage event, serialized per-channel via
    `channel_locks`.

    H-1 fix: Telethon dispatches each NewMessage as its own task, so
    without serialization a fast message (e.g. an instant
    hard_reject) can finish -- and advance the watermark -- before a
    slower earlier message (e.g. one awaiting a Gemini call) does.
    That permanently drops the earlier message from the recovery
    window if it later fails or the process crashes before it
    finishes. Locking per channel forces messages from the same
    channel to be processed, and their watermarks advanced, strictly
    in arrival order -- matching the guarantee `_recover_channel`
    already provides on startup. Different channels still run fully
    concurrently; only same-channel messages are serialized.

    A live message that fails to process (process_message returns
    False) or raises is treated exactly like a failed startup-recovery
    message: the channel's `recovery_blocked` barrier is raised so a
    later successful live message cannot advance the watermark past
    the failed one -- previously it logged and continued, leaving the
    failed message permanently unrecoverable on the next restart. When
    an `on_failure` callback is supplied (see the live loop in
    `_run_telegram_loop`, driven by `TelegramChannelWorker.run()` or
    `app.handlers.telegram.start()`) it wakes that
    channel's retry watcher immediately, so the failed message is
    re-attempted from the held watermark.
    """

    async with channel_locks[event.chat_id]:

        try:

            processed = await process_message(event)

            if processed:
                # If startup recovery for this channel stopped on a
                # failed message, live messages are still captured and
                # processed, but they must not advance the watermark
                # past that failed recovery point. Otherwise the first
                # live message after the failure could make the failed
                # message permanently unrecoverable on the next restart.
                blocked = (
                    recovery_blocked is not None
                    and recovery_blocked.get(event.chat_id, False)
                )

                if not blocked:
                    await state.async_set_last_message_id(
                        event.chat_id,
                        event.id,
                    )
            else:
                print(
                    f"[ERROR] Message {event.id} failed; "
                    f"watermark not advanced. Raising this channel's "
                    f"recovery barrier so the failed message is "
                    f"re-attempted -- a newer live message must not "
                    f"advance the watermark past it."
                )
                if recovery_blocked is not None:
                    recovery_blocked[event.chat_id] = True
                if on_failure is not None:
                    on_failure(event.chat_id)

        except Exception as e:

            await logger.log_error("MessageHandler", e)

            print(
                f"[ERROR] Failed to process message {event.id}: {e} "
                f"Raising this channel's recovery barrier so the "
                f"failed message is re-attempted."
            )
            if recovery_blocked is not None:
                recovery_blocked[event.chat_id] = True
            if on_failure is not None:
                on_failure(event.chat_id)


async def _cancel_subtasks(*tasks):
    """Cancel any not-yet-finished asyncio tasks and await them so none
    is left behind pending ("Task was destroyed but it is pending!") and
    so their CancelledError does not propagate as an unretrieved
    exception. Used to clean up the transient sleep/wake subtasks created
    around an interruptible ``asyncio.wait``; run inside a ``finally`` so
    it also covers the case where the parent watcher task itself is
    cancelled (shutdown)."""
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _retry_recovery_channel(
    client,
    channel,
    channel_lock,
    recovery_blocked,
    wake=None,
    failure_generation=None,
    *,
    max_messages=None,
    retry_base_seconds=None,
    retry_cap_seconds=None,
):
    """Retry only a channel whose watermark is blocked behind a failed
    message -- whether the failure came from startup recovery OR from
    a live message that failed to process.

    The per-channel lock preserves the same ordering barrier as startup
    recovery. Watermarks are advanced only by successful processing, and a
    failed attempt leaves the channel blocked for the next retry. Backoff is
    bounded and isolated to the affected channel.

    `wake` (an asyncio.Event, one per channel) lets a live failure
    interrupt the current backoff immediately instead of sleeping it
    out; when None, the watcher just polls. `failure_generation` counts
    live failures per channel so the watcher can tell whether a new
    failure raised the barrier *while* a retry was in flight -- if so,
    the barrier release is withheld and the retry is re-run. Without
    that check, a live message that fails during the retry's snapshot
    walk (and so is not in the walk and does not fail it) could slip
    past the watermark because the retry completed first; the
    generation counter makes that release impossible.

    A recovery attempt that itself raises (e.g. a transient Telethon
    network error) keeps the barrier and backs off rather than killing
    the watcher task and leaving the channel blocked forever.

    `retry_base_seconds`/`retry_cap_seconds` bound the per-channel
    backoff (the composition root injects
    RECOVERY.telegram_retry_{base,cap}_seconds); `max_messages` bounds
    each retry's recovery backfill.
    """
    if wake is None:
        wake = asyncio.Event()
    if failure_generation is None:
        failure_generation = defaultdict(int)

    base_seconds = (
        DEFAULT_RECOVERY_RETRY_BASE_SECONDS
        if retry_base_seconds is None
        else retry_base_seconds
    )
    cap_seconds = (
        DEFAULT_RECOVERY_RETRY_CAP_SECONDS
        if retry_cap_seconds is None
        else retry_cap_seconds
    )

    delay = base_seconds

    while True:
        if recovery_blocked.get(channel, False):
            generation_at_start = failure_generation.get(channel, 0)

            # Wait for EITHER the per-channel backoff delay to elapse OR
            # the channel's wake event to fire. A live failure raises the
            # barrier and sets the wake event precisely so an in-flight
            # retry does NOT have to sleep out the remainder of a (possibly
            # long) backoff before re-attempting the failed message. Without
            # this, `await asyncio.sleep(delay)` would ignore the wake and a
            # live failure could wait up to the full backoff cap (300s)
            # before the message is retried, contradicting the documented
            # "wakes that channel's retry watcher immediately" behavior.
            # `asyncio.wait` with FIRST_COMPLETED yields an interruptible
            # sleep without busy polling or an arbitrary short poll sleep.
            # The event is consumed AFTER the wait -- right before we
            # proceed to recover -- so the failure that woke us is acted on
            # immediately (the very first failure after an idle period is
            # serviced without a fresh backoff) while a latched event cannot
            # spurious-fire an unbounded sequence of immediate retries: once
            # serviced and cleared, a later pass waits out the backoff unless
            # a genuinely new wake is set.
            sleep_task = asyncio.create_task(asyncio.sleep(delay))
            wake_task = asyncio.create_task(wake.wait())
            try:
                await asyncio.wait(
                    {sleep_task, wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                await _cancel_subtasks(sleep_task, wake_task)
            # Consume the wake signal now that we're proceeding to recover.
            wake.clear()

            await channel_lock.acquire()
            try:
                recovered = await _recover_channel(
                    client, channel, max_messages=max_messages
                )
            except Exception as e:
                await logger.log_error("StartupRecovery", e)
                print(f"[RECOVERY ERROR] {channel}: {e}")
                recovered = RECOVERED_FAILED
            finally:
                channel_lock.release()

            if recovered == RECOVERED_COMPLETE:
                # Only a pass that proves the channel actually caught up
                # (fewer messages than the cap, none failed) may release
                # the barrier. A CAPPED pass merely drained one batch --
                # more may remain unrecovered, so the barrier must stay
                # closed until a later pass confirms completion.
                if failure_generation.get(channel, 0) == generation_at_start:
                    print(f"[RECOVERY] {channel}: retry succeeded; barrier released.")
                    recovery_blocked[channel] = False
                else:
                    print(
                        f"[RECOVERY] {channel}: retry progressed but a new "
                        f"live message failed during the attempt; keeping "
                        f"the barrier to re-run recovery over it."
                    )
                delay = base_seconds
            elif recovered == RECOVERED_CAPPED:
                # Progress was made (messages confirmed) but the channel
                # is not caught up. Keep the barrier closed and retry
                # again promptly so the backlog drains; there is no
                # failure to back off on, but the pass cost a real API
                # walk so keep a modest base delay to avoid a tight loop.
                print(
                    f"[RECOVERY] {channel}: retry drained a capped batch; "
                    f"channel still not caught up, keeping the barrier."
                )
                delay = base_seconds
            else:
                delay = min(delay * 2, cap_seconds)
        else:
            wake.clear()
            if recovery_blocked.get(channel, False):
                continue
            await wake.wait()


async def _recover_all_channels(client, channel_locks, recovery_blocked, channels=None, *, max_messages=None):
    """
    Recover every configured Telegram channel, one at a time, while
    guaranteeing that a live NewMessage event can never be processed
    ahead of that channel's own recovery.

    Startup race fix: previously the live NewMessage handler was only
    registered *after* every channel finished recovering, so a message
    arriving in that window -- after a channel's recovery snapshot was
    taken but before the handler existed -- was silently dropped by
    both mechanisms. The fix has two parts, split across this function
    and the live-loop driver (`_run_telegram_loop`, which
    `app.handlers.telegram.start()` and `TelegramChannelWorker.run()`
    both delegate to):

    1. The caller acquires every channel's lock (see `channel_locks`,
       shared with `_handle_live_message`) up front, then registers
       the live handler, and only then calls this function. Because
       none of that involves an actual suspension point (an
       uncontended `asyncio.Lock.acquire()` never yields control back
       to the event loop), the handler is guaranteed to be registered
       -- and every lock guaranteed to already be held -- before
       Telethon can dispatch a single live update. A live event can
       therefore never be lost: `_handle_live_message` always finds a
       registered handler waiting for it.

    2. This function then recovers each channel in turn and keeps that
       channel's lock held for the entire recovery attempt. Once the
       attempt finishes -- successfully or by stopping at a failed
       message -- the lock is released. A live event for a channel
       whose lock is still held
       (recovery not yet reached it, or still running) is captured by
       the handler immediately but blocks inside
       `_handle_live_message`'s `async with channel_locks[chat_id]`
       until this function releases it -- so it is processed strictly
       after that channel's recovery, never before and never
       concurrently with it. This preserves recovery's oldest -> newest
       watermark ordering: a live event can't advance a channel's
       watermark past a point recovery hasn't reached yet.

    The same underlying message can therefore be seen once by recovery
    (in its snapshot) and once by the live handler (queued behind the
    lock). That's harmless: the existing SQLite job_uuid dedup in
    app.job_processor.process_job makes the second observation a
    no-op.

    If recovery stops on a failed message, the lock is still released
    so live processing can continue, but `recovery_blocked[channel]`
    remains true. Live processing then deliberately does not advance
    that channel's watermark, preventing a newer live message from
    making the failed recovery message unrecoverable.

    `channels` and `max_messages` are injected by the caller (the live
    loop passes the JobSource's own channels and recovery cap); with no
    explicit channels the loop recovers nothing.
    """

    if channels is None:
        channels = ()

    for channel in channels:
        try:
            recovered = await _recover_channel(
                client, channel, max_messages=max_messages
            )

            # A channel is only considered caught up (barrier released)
            # when its recovery pass completed -- i.e. it drained to
            # fewer messages than the cap (RECOVERED_COMPLETE). A CAPPED
            # pass (hit the safety cap, more may remain) or a FAILED pass
            # (a message failed) must keep the shared barrier active so
            # live messages cannot advance the watermark past messages
            # that are still potentially unrecovered. The channel lock is
            # still released in every case so live events are never lost
            # forever; they are processed for real-time notifications
            # while their watermark is held behind the unfinished
            # recovery until a later pass truly catches up.
            recovery_blocked[channel] = recovered != RECOVERED_COMPLETE
        finally:
            channel_locks[channel].release()


async def _run_telegram_loop(client, channels, *, max_recovery_messages=None, recovery_retry_base_seconds=None, recovery_retry_cap_seconds=None):
    """Shared production loop over an already-started Telethon client:
    entity-cache warmup -> canonical identity resolution -> per-channel lock
    acquisition -> live handler registration -> startup recovery ->
    per-channel retry watchers -> run until disconnected. See
    `_recover_all_channels` for why the ordering of lock acquisition and
    handler registration is load-bearing.

    `channels` and the recovery options are injected: the composition
    root constructs the TelegramChannelJobSource (and the
    TelegramChannelWorker passes them down); the compat entry
    (app.handlers.telegram.start) passes the configured values.
    """

    me = await client.get_me()

    print("=" * 70)
    print(f"Logged in as: {me.first_name}")
    print("=" * 70)

    # See _warm_entity_cache for why this must run before recovery.
    await _warm_entity_cache(client, channels)

    # Resolve every configured identifier (numeric id, username, alias) to its
    # canonical numeric chat id BEFORE any state is created, and key every
    # internal channel state machine below by that ONE canonical identity. A
    # live event (and recovery walk) uses the resolved chat_id, so keying
    # channel_locks/recovery_blocked/retry_wake/failure_generation and the
    # durable watermark by the canonical id means a configured
    # "@channel" and its numeric "-1001234567890" can never create duplicate
    # state or split a channel's lock/barrier across two keys. The existing
    # numeric-id deployment is unchanged: a numeric id resolves to itself.
    canonical_channels = await _resolve_channel_ids(client, channels)

    # See _handle_live_message for why this needs to be per-channel
    # locked (H-1), and _recover_all_channels for why every lock is
    # acquired here -- synchronously, before the live handler below is
    # registered -- rather than inside the recovery loop itself.
    channel_locks = defaultdict(asyncio.Lock)
    recovery_blocked = {
        channel: True
        for channel in canonical_channels
    }
    # One wake event per channel so a live failure can interrupt that
    # channel's retry backoff immediately; one failure counter per
    # channel so a watcher can tell whether a new failure raised the
    # barrier *while* its retry was in flight (see
    # _retry_recovery_channel).
    retry_wake = defaultdict(asyncio.Event)
    failure_generation = defaultdict(int)

    for channel in canonical_channels:
        await channel_locks[channel].acquire()

    def raise_live_barrier(chat_id):
        recovery_blocked[chat_id] = True
        failure_generation[chat_id] += 1
        retry_wake[chat_id].set()

    @client.on(events.NewMessage(chats=list(canonical_channels)))
    async def handler(event):

        # Metadata lookup (event.get_chat()) is BEST-EFFORT only.
        # It must NEVER prevent the actual message from entering the
        # durable processing/recovery state machine. If get_chat()
        # fails or times out, we log the failure and continue with
        # message processing using the numeric chat_id from the event
        # itself. This prevents the failure sequence:
        #   Telegram message N -> get_chat() timeout -> handler exits
        #   -> recovery barrier never raised -> later message N+1 succeeds
        #   -> watermark advances -> message N permanently skipped
        chat = None
        try:
            chat = await call_with_timeout(
                event.get_chat(),
                label="Telegram event.get_chat()",
            )
        except Exception as e:
            # Metadata lookup failed; continue processing with numeric ID only.
            # Do NOT swallow unrelated exceptions from _handle_live_message.
            print(
                f"[TELEGRAM] get_chat() failed for message {event.id} "
                f"in chat {event.chat_id}: {e}. Continuing without metadata."
            )

        if chat is not None:
            print(
                f"[TARGET] {chat.title} | "
                f"Message ID: {event.id}"
            )
        else:
            print(
                f"[TARGET] chat_id={event.chat_id} | "
                f"Message ID: {event.id} (metadata unavailable)"
            )

        await _handle_live_message(
            event,
            channel_locks,
            recovery_blocked,
            raise_live_barrier,
        )

    print("Recovering missed messages...\n")

    await _recover_all_channels(
        client,
        channel_locks,
        recovery_blocked,
        canonical_channels,
        max_messages=max_recovery_messages,
    )

    # One watcher per canonical channel, not just the ones blocked at
    # startup: a live message can fail at any time, and when it does it
    # raises the channel's barrier (see _handle_live_message) and wakes
    # this watcher via its per-channel event.
    retry_tasks = [
        asyncio.create_task(
            _retry_recovery_channel(
                client,
                channel,
                channel_locks[channel],
                recovery_blocked,
                retry_wake[channel],
                failure_generation,
                max_messages=max_recovery_messages,
                retry_base_seconds=recovery_retry_base_seconds,
                retry_cap_seconds=recovery_retry_cap_seconds,
            )
        )
        for channel in canonical_channels
    ]

    print("Recovery complete.")
    print("Listening for new jobs...\n")

    try:
        await client.run_until_disconnected()
    finally:
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)


class TelegramChannelWorker:
    """Canonical live Telegram production worker.

    The composition root constructs a TelegramChannelJobSource with every
    collaborator injected (channels, parser, state_store, credentials,
    recovery options) and hands it to this worker; ``run()`` drives the
    shared Telegram live/recovery/retry/barrier machinery (module functions
    above) over the source's own channels, client, and recovery options.
    Shutdown is deterministic: ``run()`` cancels the per-channel retry
    watchers, and ``aclose()`` disconnects the Telethon client exactly once.
    """

    def __init__(self, source):
        self.source = source

    async def _beat_while_connected(self):
        """Companion task: beat liveness on a fixed cadence while the
        live loop is inside Telethon's run_until_disconnected(), where
        a perfectly healthy connection produces no message-driven beats.

        It runs for exactly as long as _run_telegram_loop(); run()
        cancels it the moment the loop returns, raises, or is cancelled,
        and it exits silently on cancellation so it can never outlive
        the connection it represents.
        """
        while True:
            liveness.beat("telegram", STATE_ALIVE)
            try:
                await asyncio.sleep(self.source.liveness_beat_interval_seconds)
            except asyncio.CancelledError:
                return

    async def run(self):
        """Run the Telegram live/recovery/retry loop with automatic
        reconnection on transient disconnects.

        When run_until_disconnected() returns (network hiccup, Telethon
        transient error), the client is cleaned up and a fresh connection
        is established after a bounded exponential backoff. Fatal errors
        (auth/session/forbidden) propagate immediately so TaskGroup can
        shut down the process -- no amount of reconnecting will fix them.

        CancelledError (from TaskGroup shutdown) propagates immediately
        without triggering a reconnection attempt.

        The 2,000-message recovery cap, strict per-channel watermark
        ordering, and exhaustive provider semantics are all preserved:
        reconnection only replaces the Telethon client; the recovery
        machinery, watermark state, and channel locks are rebuilt fresh
        on each successful connection.
        """
        delay = _RECONNECT_BASE_SECONDS
        attempts = 0
        liveness.register("telegram", STATE_ALIVE)

        while True:
            try:
                client = await self.source._client()
                # Beating while connected (and during recovery/retry) ensures
                # the healthcheck sees an alive Telegram worker, not just a
                # ticking global heartbeat.
                liveness.beat("telegram", STATE_ALIVE)
                # While connected, a companion task keeps beating liveness on
                # the configured cadence so an idle-but-healthy connection is
                # not mistaken for a stale/hung one (see _beat_while_connected).
                beat_task = asyncio.create_task(self._beat_while_connected())
                try:
                    await _run_telegram_loop(
                        client,
                        self.source.channels,
                        max_recovery_messages=self.source.max_recovery_messages,
                        recovery_retry_base_seconds=self.source.recovery_retry_base_seconds,
                        recovery_retry_cap_seconds=self.source.recovery_retry_cap_seconds,
                    )
                finally:
                    beat_task.cancel()
                    await asyncio.gather(beat_task, return_exceptions=True)
                # run_until_disconnected() returned normally -- transient
                # disconnect. Clean up the stale client and reconnect.
                print(
                    "[TELEGRAM] Worker disconnected; "
                    "attempting reconnection..."
                )
            except asyncio.CancelledError:
                # TaskGroup shutdown -- do not reconnect, propagate.
                raise
            except Exception as exc:
                if _is_fatal_telethon_error(exc):
                    print(
                        f"[TELEGRAM] Fatal error: {exc}; "
                        "propagating to TaskGroup for shutdown."
                    )
                    raise
                print(
                    f"[TELEGRAM] Transient error: {exc}; "
                    "attempting reconnection..."
                )

            # Clean up the old client before creating a fresh one. A failed
            # cleanup (e.g. disconnect() raising on an already-broken
            # socket) must NOT block the reconnection attempt -- surface it
            # and move on; the stale client is replaced on the next attempt.
            try:
                await self.source.aclose()
            except Exception as exc:
                print(
                    f"[TELEGRAM] Client cleanup before reconnect failed "
                    f"(continuing anyway): {exc}"
                )

            attempts += 1
            if (
                _RECONNECT_MAX_ATTEMPTS > 0
                and attempts >= _RECONNECT_MAX_ATTEMPTS
            ):
                raise RuntimeError(
                    f"Telegram worker failed to reconnect after "
                    f"{attempts} attempts; giving up."
                )

            # Report the bounded reconnect/backoff state so the healthcheck
            # treats this as temporarily degraded (reconnecting), not dead.
            liveness.beat("telegram", STATE_RECONNECTING)
            print(
                f"[TELEGRAM] Reconnecting in {delay:.1f}s "
                f"(attempt {attempts})..."
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX_SECONDS)

            # Reset the client so _client() creates a fresh one on
            # the next connection attempt.
            self.source.client = None

    async def aclose(self):
        await self.source.aclose()


class TelegramChannelJobSource(JobSource):
    id = "telegram"

    def __init__(
        self,
        client=None,
        channels=None,
        *,
        parser=None,
        state_store=None,
        api_id=None,
        api_hash=None,
        phone_number=None,
        session_name=None,
        batch_limit=DEFAULT_BATCH_LIMIT,
        max_recovery_messages=None,
        recovery_retry_base_seconds=None,
        recovery_retry_cap_seconds=None,
        liveness_beat_interval_seconds=None,
    ):
        self.client = client
        self.channels = tuple(channels or ())
        self.parser = parser
        self.state_store = state_store
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone_number = phone_number
        self.session_name = session_name
        self.batch_limit = int(batch_limit or DEFAULT_BATCH_LIMIT)
        self.max_recovery_messages = int(
            max_recovery_messages or DEFAULT_RECOVERY_MAX_MESSAGES
        )
        self.recovery_retry_base_seconds = (
            DEFAULT_RECOVERY_RETRY_BASE_SECONDS
            if recovery_retry_base_seconds is None
            else recovery_retry_base_seconds
        )
        self.recovery_retry_cap_seconds = (
            DEFAULT_RECOVERY_RETRY_CAP_SECONDS
            if recovery_retry_cap_seconds is None
            else recovery_retry_cap_seconds
        )
        self.liveness_beat_interval_seconds = (
            DEFAULT_LIVENESS_BEAT_SECONDS
            if liveness_beat_interval_seconds is None
            else float(liveness_beat_interval_seconds)
        )
        # Per-channel message ids returned by the most recent poll, oldest
        # first. mark_seen() pops the head as each message is durably
        # confirmed and only advances the watermark through contiguous
        # confirmations. Keyed by the CANONICAL channel identity (resolved
        # numeric chat id, or the configured identifier when not resolvable)
        # so poll()/mark_seen() always agree on the same key regardless of
        # whether configuration uses a numeric id, a username, or an alias.
        self._inflight = {}
        # configured channel -> canonical identity (resolved numeric chat id).
        # Populated the first time a channel is resolved and reused, so all
        # watermark/inflight/identity operations stay consistent across
        # polls within one source instance.
        self._channel_ids = {}

    @property
    def identity_source(self):
        # A numeric Telegram chat ID is rename-stable and is the source identity.
        return str(self.channels[0]) if len(self.channels) == 1 else "telegram"

    async def _client(self):
        if self.client is None:
            if not (self.api_id and self.api_hash and self.session_name):
                raise RuntimeError(
                    "TelegramChannelJobSource requires api_id/api_hash/"
                    "session_name (or an injected client) to build a "
                    "TelegramClient."
                )
            from telethon import TelegramClient

            self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
            await call_with_timeout(
                self.client.start(phone=self.phone_number),
                label="Telegram client.start()",
            )
        return self.client

    def _channel_id_of(self, message):
        # Telethon Message objects expose .chat_id; plain test stand-ins
        # expose .id (message id) and are keyed by the channel they were
        # fetched from, which poll() tracks itself.
        return str(getattr(message, "chat_id", None) or "")

    async def _resolve_channel_id(self, channel):
        # One canonical identity per configured channel, resolved lazily and
        # cached for the source's lifetime. Using the resolved numeric chat
        # id (instead of the raw configured token) means poll(), mark_seen(),
        # and job["identity_source"] all agree on the SAME key whether the
        # configuration names the channel by numeric id, username, or alias.
        #
        # Returns None when a nonnumeric channel cannot be resolved: the
        # channel is skipped for this poll cycle and retried on the next one.
        # A numeric identifier that cannot be resolved falls back to the
        # configured token (it is already the canonical chat id).
        cached = self._channel_ids.get(channel)
        if cached is not None:
            return cached
        resolved = None
        if self.client is not None:
            try:
                chat = await call_with_timeout(
                    self.client.get_entity(channel),
                    label=f"Telegram get_entity({channel!r})",
                )
                resolved = str(chat.id)
            except Exception:
                resolved = None
        if resolved is None:
            # A numeric identifier is already canonical and can be used as-is
            # even when get_entity fails (e.g. an injected test client
            # without get_entity).  A nonnumeric username/alias MUST NOT
            # fall back to the raw string: live Telegram events carry a
            # numeric chat_id, so using "@channel" as an internal key would
            # create a split-state where poll/mark_seen/watermark keys one
            # identity and live events fire under another.
            try:
                int(channel)
                resolved = str(channel)
            except (TypeError, ValueError):
                print(
                    f"[WARNING] Cannot resolve nonnumeric channel "
                    f"identifier {channel!r}; skipping for this poll "
                    f"cycle. Will retry on next poll."
                )
                return None
        self._channel_ids[channel] = resolved
        return resolved

    async def _last_message_id(self, channel):
        if self.state_store is None:
            raise RuntimeError(
                "TelegramChannelJobSource requires an injected state_store "
                "to read/persist the watermark."
            )
        value = await self.state_store.get_last_message_id(channel)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def _set_last_message_id(self, channel, message_id):
        await self.state_store.async_set_last_message_id(channel, message_id)

    def _parse_message(self, message, channel, identity_source):
        text = message.raw_text or ""
        source = message.chat.title if getattr(message, "chat", None) else str(channel)
        job = self.parser.parse(source, text)
        job["job_id"] = str(message.id)
        # identity_source is the canonical channel identity resolved by
        # poll(): it always matches the _inflight key and the durable
        # watermark key that mark_seen() writes under, so a job confirmed
        # downstream is always confirmed against the right channel.
        job["identity_source"] = identity_source
        job["source"] = source
        if not job.get("url") and getattr(message, "buttons", None):
            for row in message.buttons:
                for button in row:
                    url = getattr(button, "url", None) or getattr(
                        getattr(button, "button", None), "url", None
                    )
                    if url:
                        job["url"] = url
                        break
                if job.get("url"):
                    break
        return job

    async def poll(self):
        if self.parser is None:
            raise RuntimeError(
                "TelegramChannelJobSource requires an injected parser "
                "to turn raw messages into job payloads."
            )
        client = await self._client()
        jobs = []
        for channel in self.channels:
            # Resolve the canonical identity FIRST so the watermark read, the
            # _inflight key, and job["identity_source"] all share one key and
            # can never drift for username/alias-configured channels.
            canonical = await self._resolve_channel_id(channel)
            if canonical is None:
                # Nonnumeric channel that could not be resolved this cycle;
                # skip it entirely -- no state is created under the
                # unresolved string, and the next poll retries resolution.
                continue
            watermark = await self._last_message_id(canonical)
            fetched = []
            async for message in iter_with_timeout(
                client.iter_messages(
                    channel,
                    min_id=watermark,
                    reverse=True,  # oldest -> newest, regardless of API default
                    limit=self.batch_limit,
                ),
                label=f"Telegram iter_messages({channel!r})",
            ):
                fetched.append(message)
            # Register the batch in order so mark_seen() can keep the
            # watermark contiguous. Keyed by the canonical identity to match
            # the job["identity_source"] that mark_seen() resolves.
            # Any still-unconfirmed ids from the previous batch are
            # re-fetched (they are all > watermark), so replacing the
            # deque is safe and self-healing.
            self._inflight[canonical] = deque(m.id for m in fetched)
            jobs.extend(
                self._parse_message(m, channel, canonical) for m in fetched
            )
        return jobs

    def normalize(self, job):
        # poll() already produces pipeline-shaped job dicts (parser output
        # plus job_id/identity_source/source/url); the generic worker calls
        # normalize() on raw items, so identity is correct here.
        return job

    async def mark_seen(self, job):
        channel_id = job.get("identity_source")
        message_id = job.get("job_id")
        if channel_id is None or message_id is None:
            return None

        pending = self._inflight.get(channel_id)
        if not pending:
            # No active batch for this channel (e.g. poll() returned nothing
            # and a stale job was re-confirmed). Nothing to gate; leave the
            # watermark untouched -- never regress, never jump.
            return None

        candidate = int(message_id)
        if pending[0] != candidate:
            # An older message in this batch is still unconfirmed (it failed
            # or was skipped). Advancing past it would permanently skip it
            # on the next poll, so hold the watermark where it is; the next
            # poll re-fetches from the gap and the already-Complete message
            # ids dedupe to no-ops in process_job.
            return None

        current = await self._last_message_id(channel_id)
        if candidate <= current:
            return None

        pending.popleft()
        await self._set_last_message_id(channel_id, candidate)
        return None

    async def aclose(self):
        # Deterministic shutdown: disconnect the Telethon client if one was
        # built (or injected). Idempotent -- Telethon's disconnect is safe
        # to call more than once. Bounded so a hung disconnect (e.g. a
        # dead socket that never completes its close handshake) cannot
        # stall process shutdown indefinitely.
        if self.client is not None:
            try:
                await call_with_timeout(
                    self.client.disconnect(), label="Telegram client.disconnect()"
                )
            except Exception as exc:
                print(
                    f"[TELEGRAM] Client disconnect during shutdown failed "
                    f"(continuing anyway): {exc}"
                )