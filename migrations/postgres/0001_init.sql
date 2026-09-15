-- Fresh Postgres schema for THIS service only: ingest -> classify ->
-- guard -> persist -> publish to Redis. Column names and quoting match
-- the old SQLite COLUMN_MAP (app/services/logger.py, now removed) so
-- the Postgres repository adapter could reuse the same snake_case ->
-- "Header" mapping.
--
-- Subscriber/notification-delivery tables (users, subscription_events,
-- user_notifications) are deliberately NOT defined here -- that whole
-- flow is partitioned onto a separate machine/service (a Node.js bot),
-- which owns its own schema/migrations for them, even though it reads
-- this service's `jobs`/`categories` data via the Redis stream this
-- service publishes to (see app.adapters.streams.redis_publisher).
--
-- This is a brand-new, empty database: no data is carried over from
-- SQLite. Nine write-only jobs columns that nothing in the app ever
-- reads back (Filter Text, Category Candidates, Hard Reject Matches,
-- Title Core Positive/Negative, Core/Supporting Positive/Negative
-- Matches) were dropped rather than ported. Every write-only log table
-- (gemini, notifications, errors) is kept, per decision to preserve
-- the audit-trail shape going forward even though nothing reads them
-- back today.

CREATE TABLE categories (
    "Category ID"  TEXT PRIMARY KEY,
    "Name"         TEXT NOT NULL,
    "Description"  TEXT,
    "Enabled"      BOOLEAN NOT NULL DEFAULT TRUE,
    "Created At"   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    "Timestamp"                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    "Job UUID"                       UUID PRIMARY KEY,
    "Job ID"                         TEXT,
    "Source"                         TEXT,
    "Identity Source"                TEXT,
    "Title"                          TEXT,
    "Description"                    TEXT,
    "Raw Message"                    TEXT,
    "Company"                        TEXT,
    "URL"                            TEXT,
    "Short URL"                      TEXT,
    "Decision"                       TEXT,
    "Decision Reason"                TEXT,
    "Categories"                     TEXT,
    "Negative Categories"            TEXT,
    "Has Core Positive"              BOOLEAN,
    "Has Core Negative"              BOOLEAN,
    "Core Positive Hit Count"        INTEGER,
    "Supporting Positive Weight"     DOUBLE PRECISION,
    "Supporting Negative Weight"     DOUBLE PRECISION,
    "Hard Reject"                    BOOLEAN,
    "Notify Directly"                BOOLEAN,
    "Needs Gemini"                   BOOLEAN,
    "Gemini Decision"                TEXT,
    "Notification Status"            TEXT,
    "Final Decision"                 TEXT,
    "Category ID"                    TEXT REFERENCES categories ("Category ID") ON DELETE SET NULL,
    "Category Selection Method"      TEXT,
    "Filter Time (ms)"               INTEGER,
    "Classification Retry Not Before" TIMESTAMPTZ
);

CREATE INDEX idx_jobs_notification_status ON jobs ("Notification Status");
CREATE INDEX idx_jobs_final_decision_reason ON jobs ("Final Decision", "Decision Reason");

CREATE TABLE notification_guard (
    id                    BIGSERIAL PRIMARY KEY,
    "Timestamp"           TIMESTAMPTZ NOT NULL DEFAULT now(),
    "Job UUID"            UUID NOT NULL REFERENCES jobs ("Job UUID") ON DELETE CASCADE,
    "Source"              TEXT,
    "Title"               TEXT,
    "Original Decision"   TEXT,
    "Guard Decision"      TEXT,
    "Provider"            TEXT,
    "Model"               TEXT,
    "Response Time (ms)"  INTEGER,
    "Error"               TEXT,
    "Guard Category"      TEXT
);

-- get_latest_guard_decision(_with_category) looks up the most recent
-- row for a given job; this is a real operational read path, not just
-- an audit log (see app/notification_guard/integration.py).
CREATE INDEX idx_notification_guard_job_latest
    ON notification_guard ("Job UUID", id DESC);

-- ---------------------------------------------------------------
-- Write-only audit/log tables. Nothing in the application currently
-- reads these back; kept for historical record only.
-- ---------------------------------------------------------------

CREATE TABLE gemini (
    id                    BIGSERIAL PRIMARY KEY,
    "Timestamp"           TIMESTAMPTZ NOT NULL DEFAULT now(),
    "Job UUID"            UUID,
    "Decision Before"     TEXT,
    "Reason Before"       TEXT,
    "Prompt Tokens"       INTEGER,
    "Completion Tokens"   INTEGER,
    "Response Time (ms)"  INTEGER,
    "Decision"            TEXT,
    "Confidence"          TEXT,
    "Provider"            TEXT
);

CREATE TABLE notifications (
    id           BIGSERIAL PRIMARY KEY,
    "Timestamp"  TIMESTAMPTZ NOT NULL DEFAULT now(),
    "Job UUID"   UUID,
    "Platform"   TEXT,
    "Status"     TEXT
);

CREATE TABLE errors (
    id           BIGSERIAL PRIMARY KEY,
    "Timestamp"  TIMESTAMPTZ NOT NULL DEFAULT now(),
    "Job UUID"   UUID,
    "Module"     TEXT,
    "Error"      TEXT
);
