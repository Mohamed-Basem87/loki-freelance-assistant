# Refactor Changes — Summary

## Overview

This refactor keeps the existing durable Business Node guarantees while tightening the **Postgres + Redis Stream architecture**, **failure recovery**, **timeouts**, **event contracts**, and **test/CI coverage**.

The main direction is:

> **Postgres owns durable state → Redis Stream carries finalized notification events → downstream delivery consumes the stream.**

---

## 1. Infrastructure & Configuration

### Postgres
- Postgres is now the explicit persistence backend.
- Schema migrations under `migrations/postgres/*.sql` are applied automatically at startup.
- The migration runner can still be invoked manually when needed.
- CI/Docker test environments now start a real Postgres service with health checks.
- Tests use the same `DATABASE_URL` contract as production.

### Redis
- Redis is treated as an external/shared node rather than a local dependency.
- `REDIS_URL` is documented as pointing to the reachable Redis host over the network.
- Localhost Redis remains available only as a development-only option.

### Dependencies
- Added/pinned `scrapling[fetchers]==0.4.15`.

---

## 2. Postgres Repository Hardening

`app/adapters/repositories/postgres.py` was substantially strengthened.

### Timeout protection
Every repository operation is bounded by multiple layers:

- Application-level database timeout.
- PostgreSQL server-side `statement_timeout`.
- Connection timeout.
- Pool timeout.
- `asyncio.wait_for()` around the executor dispatch.

This prevents a database operation from silently hanging a worker indefinitely.

### Stuck executor quarantine
The previous `StuckExecutorError` safety model is restored for Postgres:

- A timed-out repository worker is treated as potentially still running.
- The repository is **quarantined** instead of replacing the executor/pool.
- Subsequent repository calls fail closed.
- DB-worker liveness is marked dead.
- Process restart is the intentional recovery mechanism.

This avoids the dangerous situation where a new executor starts touching the same rows while the old worker may still be executing.

### Connection/pool lifecycle
- Repository disposal is now wired into application composition.
- Pool/executor lifecycle is handled explicitly.
- Pool recycling is bounded.

---

## 3. Postgres Compatibility & Data Normalization

The Postgres adapter now normalizes legacy values at its boundary.

Covered conversions include:

- Integer-like values.
- Legacy floats.
- Boolean values.
- Numeric strings.
- Epoch timestamps.
- ISO timestamps.
- Empty/invalid timestamp values.
- Empty category IDs → `NULL`.
- Typed update values for Postgres columns.

The existing field-name contract is preserved through the `COLUMN_MAP`, so callers can continue using the established job field names.

Dropped legacy filter-only keys are tolerated instead of causing unnecessary failures.

---

## 4. Automatic Database Bootstrap

Startup now handles database preparation automatically:

1. Connect to Postgres.
2. Apply versioned migrations.
3. Seed the category catalog.
4. Verify connectivity.

Initialization is designed to be **idempotent**, so restarting the application does not repeatedly corrupt or duplicate bootstrap state.

---

## 5. Notification Event DTO

`job_processor.py` now has a single canonical notification-event builder:

`build_notification_event(final_job)`

The Redis event is intentionally a **small, stable DTO**, rather than the entire Postgres row.

### DTO contract

The event contains the canonical notification fields:

- `Job UUID`
- `Title`
- `Description`
- `Source`
- `URL`
- `Short URL`
- `Decision Reason`
- `Categories`
- `Category ID`
- `Category Selection Method`

`Job UUID` is mandatory and remains the first field.

### Important durability rule

The DTO is built from a **freshly-read durable Postgres row**, not from a potentially stale caller snapshot.

This means guard-driven durable reclassification is preserved in the event, including:

- `Category ID`
- `Categories`
- `Category Selection Method`

The same durable row therefore produces the same event on the initial publish and on replay.

---

## 6. Notification State Machine & Crash Recovery

The notification flow remains:

```text
Pending
   ↓
publish to Redis
   ↓
Complete
```

The critical ordering is preserved:

> **Publish first → mark Complete second**

Therefore:

- If publishing fails, the row remains `Pending`.
- If the process crashes after Redis publish but before the `Complete` write, the row remains recoverable.
- The retry sweep can republish the pending row.
- The event is reconstructed from the durable row.
- `Job UUID` provides the downstream idempotency key.

Terminal states such as `Complete` and `Suppressed` are not repeatedly processed by the notification sweep.

---

## 7. Redis Stream Safety

The Redis publisher is hardened around the shared notification stream:

- Uses the `jobs:notify` stream.
- Stream length is bounded with `MAXLEN ≈ 100,000`.
- XADD values are normalized/stringified for the Redis contract.
- Publisher shutdown releases its Redis client.

Publish failures are **not swallowed**.

This keeps the Postgres row recoverable instead of falsely marking a notification as completed.

---

## 8. Job Deduplication & Atomicity

The refactor restores and expands coverage for the identity guarantees.

### Canonical identity
`job_uuid` remains deterministic and stable.

Tests verify:

- Same source + job ID → same UUID.
- Different job IDs → different UUIDs.
- Different identity sources → different UUIDs.
- UUID remains stable across process runs.

### Legacy compatibility
Old source-based UUIDs are still recognized so historical rows do not become invisible to the new identity scheme.

### Atomic create-if-absent
Concurrent processing of the same job is tested against the durable repository contract.

The database primary key is the final arbiter:

```text
Concurrent callers
       ↓
create_job_if_absent()
       ↓
exactly one durable row
```

This preserves the **atomicity** guarantee rather than relying on a caller-side existence check.

---

## 9. Durable LLM Classification Retry

LLM failures are treated as durable, retryable state.

A failed classification:

- Persists a retry-not-before schedule.
- Is not immediately retried by another caller.
- Is picked up by the classification retry sweep once due.
- Uses a durable claim/lease so concurrent callers cannot both classify the same pending job.

This protects both provider usage and state consistency.

---

## 10. Guard Error Semantics

Guard outcomes are now explicitly distinguished:

### Guard error
- Fail-closed behavior does **not** permanently suppress the job.
- Durable state remains `Pending`.
- A later retry sweep can evaluate the guard again.

### Durable `do_not_notify`
- The job is durably suppressed.
- It remains terminal and is not endlessly re-evaluated.

### Durable `notify`
- The decision is reused instead of unnecessarily re-running the guard.

### LLM-reviewed jobs
- Correctly bypass the guard where required.

### Full-stack guard reclassification
When the guard changes classification/category data, the durable fields are updated together and the notification is generated from that durable result.

---

## 11. URL Shortening Ordering

The accepted-job pipeline keeps the important ordering:

```text
Create durable job row with original URL
            ↓
Shorten URL
            ↓
Update "Short URL"
            ↓
Publish notification
```

Consequences:

- The job cannot be orphaned because of shortener state.
- Accepted jobs can recover from a shortener failure.
- Fail-closed shortener failures propagate instead of being silently ignored.
- Fail-open behavior can fall back to the original URL.
- Rejected jobs never invoke the shortener.

---

## 12. Cross-Source Deduplication

Cross-source identity coverage was restored at the **pipeline level**, not just at the UUID helper level.

Tested cases include:

- Same Telegram message processed twice.
- Same FreeHub project polled twice.
- Same project arriving through Telegram and FreeHub.
- Same project arriving in the reverse source order.
- An accepted job surviving later duplicate attempts.

The tests exercise the actual `process_job()` boundary with isolated durable/state mechanisms.

---

## 13. Test Architecture

A shared hermetic test layer was added:

`tests/_business_node_fakes.py`

It provides deterministic fakes for:

- Postgres repository behavior.
- Redis publishing.
- Guard.
- URL shortener.

The fake repository deliberately yields to the event loop so concurrent tests still model the real async → worker-thread → database interleaving.

This prevents tests from accidentally passing because the fake implementation serializes everything.

---

## 14. Postgres Integration Coverage

New Postgres contract tests verify:

- Initialization and category seeding.
- Idempotent bootstrap.
- Atomic `create_job_if_absent()`.
- Unknown-field rejection.
- Dropped legacy-key handling.
- Missing-row behavior.
- Classification lease semantics.
- Filtering of recoverable notification rows.
- Full accepted-job → notification DTO round-trip.

Normalization tests separately verify the SQLite → Postgres compatibility boundary.

Timeout tests verify the real timeout/quarantine behavior.

---

## 15. CI & Docker Improvements

CI and Docker test workflows now provision:

```text
Postgres
Redis
   ↓
test suite
   ↓
smoke tests
```

Services include health checks so tests do not race service startup.

The smoke test now:

- Initializes the real database when DB/Redis configuration is present.
- Verifies guarded entrypoint behavior.
- Skips DB-dependent checks cleanly when infrastructure is intentionally absent during plain local testing.
- Keeps CI fully infrastructure-backed.

---

# Durability Guarantees Preserved

The refactor explicitly retains the important durability concepts:

| Concept | Status |
|---|---|
| **Atomicity** | Preserved — DB-backed create-if-absent |
| **Deterministic identity** | Preserved |
| **Deduplication** | Preserved, including legacy/cross-source cases |
| **Durable state** | Preserved in Postgres |
| **LLM retry durability** | Preserved |
| **Classification claiming / leases** | Preserved |
| **Guard retryability** | Preserved |
| **Fail-closed guard semantics** | Preserved without falsely suppressing transient errors |
| **Notification recovery** | Preserved |
| **Publish-before-Complete ordering** | Preserved |
| **Crash recovery** | Preserved through Pending + retry sweep |
| **At-least-once delivery model** | Preserved |
| **Consumer idempotency** | Preserved through `Job UUID` |
| **Fresh durable DTO generation** | Added/strengthened |
| **Redis stream boundedness** | Preserved/strengthened |
| **Database timeout safety** | Restored/strengthened |
| **Stuck-worker quarantine** | Restored |
| **URL-shortening crash safety** | Preserved |
| **Postgres type integrity** | Strengthened |
| **Startup migration/bootstrap** | Added/automated |

---

# Bottom Line

This is primarily a **hardening/refactor**, not a redesign of the durability model.

The core guarantees remain:

```text
Durable Postgres state
        +
Atomic identity/dedup
        +
Durable classification/retry state
        +
Publish-before-Complete
        +
Pending notification recovery
        +
At-least-once Redis events
        +
Job UUID idempotency
        +
Bounded DB/Redis infrastructure
```

The major improvement is that those guarantees are now more explicitly enforced at the **database, repository, event, retry, and integration-test boundaries**, rather than relying on individual callers to maintain them.
