"""
Regression tests for DBLogger._migrate_user_uniqueness's notification
re-point and for the narrowed OperationalError handling in the two
legacy migrations.

The unique index idx_user_notifications_job_user forbids two
user_notifications rows for the same ("Job UUID", "User ID"). The old
dedup re-pointed every duplicate user's queued rows with a single bulk
UPDATE user_notifications SET "User ID" = survivor WHERE "User ID" =
dup -- which, the moment the survivor already owned a row for that same
job (entirely possible legacy data, both rows having been written by
the pre-uniqueness era), raised IntegrityError and aborted the entire
startup migration. The fix resolves each collision deterministically by
delivery progress -- Sent > Sending > Pending > RateLimited > Failed >
Cancelled -- keeping the more-completed row and dropping the other, so
a real delivery is never lost and the migration never fails.

The second concern here is the two `except sqlite3.OperationalError:
return` guards (legacy notification states, legacy user_categories): a
missing table is a legitimate "nothing to migrate", but a "database is
locked" (or any other operational error) must NOT be swallowed -- a
locked database still leaves legacy rows un-normalized, and the next
sweep would silently re-send a legacy "Telegram: Sent" row.
"""

import sqlite3

import pytest

from app.logger import logger


class _LockedOnPrefix:
    """Delegating connection proxy that raises "database is locked" for
    statements starting with a given SQL prefix and forwards everything
    else to the real connection (sqlite3.Connection.execute is a
    read-only C attribute, so the failure must be injected this way)."""

    def __init__(self, real, fail_prefix):
        self._real = real
        self._fail_prefix = fail_prefix

    def execute(self, sql, *args, **kwargs):
        if sql.startswith(self._fail_prefix):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def db(tmp_path):
    """A fresh DBLogger wired to a throwaway file (mirrors
    test_legacy_notification_migration.legacy_db)."""
    original_path = logger.path
    logger.path = tmp_path / "user_uniqueness.db"
    logger.initialize()
    try:
        yield logger
    finally:
        logger.close()
        logger.path = original_path


def _seed_duplicate_users(db, telegram_id="12345", labels=("old", "newest")):
    """Insert two users rows sharing one Telegram User ID -- normally
    impossible because of idx_users_telegram_user_id, so that index is
    dropped first exactly like a pre-uniqueness-era database would
    have lacked it. Returns the two inserted "User ID" values in the
    order their rows were written (old first, survivor = newest/last)."""
    db._conn.execute("DROP INDEX IF EXISTS idx_users_telegram_user_id")
    user_ids = []
    for label in labels:
        user_id = f"user-{label}"
        db._conn.execute(
            'INSERT INTO users ("User ID", "Telegram User ID", "Username", '
            '"First Name", "Destination Type", "Is Active", '
            '"Created At", "Updated At") '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                telegram_id,
                label,
                label,
                "user",
                "1",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        user_ids.append(user_id)
    return user_ids


def _insert_user_notification(db, notification_id, job_uuid, user_id, telegram_id, status):
    db._conn.execute(
        'INSERT INTO user_notifications '
        '("Notification ID", "Job UUID", "User ID", "Telegram User ID", '
        '"Category ID", "Status", "Created At", "Updated At") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notification_id,
            job_uuid,
            user_id,
            telegram_id,
            "data_analysis",
            status,
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )


def _notifications_for_user(db, user_id):
    return db._conn.execute(
        'SELECT "Notification ID", "Job UUID", "User ID", "Telegram User ID", "Status" '
        'FROM user_notifications WHERE "User ID" = ?',
        (user_id,),
    ).fetchall()


# ----------------------------------------------------------------------
# Collision resolution by delivery progress
# ----------------------------------------------------------------------

def test_repoints_dup_rows_when_survivor_has_no_same_job_row(db):
    """No collision: every duplicate's queued row is simply re-pointed
    at the survivor so nothing is orphaned, and the Telegram User ID is
    carried over for robustness."""
    old, survivor = _seed_duplicate_users(db)
    _insert_user_notification(db, "nid-dup-a", "job-a", old, "12345", "Pending")
    _insert_user_notification(db, "nid-sur-b", "job-b", survivor, "12345", "Pending")

    db._migrate_user_uniqueness()

    rows = _notifications_for_user(db, survivor)
    job_user_ids = sorted((row[1], row[2]) for row in rows)
    assert job_user_ids == [("job-a", survivor), ("job-b", survivor)], (
        "the duplicate's row must be re-pointed at the survivor"
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM users WHERE \"Telegram User ID\" = '12345'"
    ).fetchone()[0] == 1, "duplicate users row must be removed"


def test_keeps_survivor_row_when_dup_is_lower_precedence(db):
    """Collision, survivor wins: survivor owns a durably "Sent" row for
    the same job while the duplicate's is merely "Pending" -- the
    duplicate's row is dropped, the real delivery survives untouched."""
    old, survivor = _seed_duplicate_users(db)
    _insert_user_notification(db, "nid-dup", "job-s", old, "12345", "Pending")
    _insert_user_notification(db, "nid-sur", "job-s", survivor, "12345", "Sent")

    db._migrate_user_uniqueness()

    rows = _notifications_for_user(db, survivor)
    assert [row[4] for row in rows] == ["Sent"], (
        "the higher-precedence (Sent) survivor row must be the only one left"
    )
    assert not _notifications_for_user(db, old), "dup user no longer exists"


def test_keeps_dup_row_when_dup_is_higher_precedence(db):
    """Collision, duplicate wins: the duplicate owns a durably "Sent"
    row while the survivor's is only "Pending". The sent row is kept and
    re-pointed at the survivor (the dup's own Notification ID rows are
    the real delivery), and the survivor's lesser row for that job is
    deleted so the unique index is not violated."""
    old, survivor = _seed_duplicate_users(db)
    _insert_user_notification(db, "nid-dup", "job-s", old, "12345", "Sent")
    _insert_user_notification(db, "nid-sur", "job-s", survivor, "12345", "Pending")

    db._migrate_user_uniqueness()

    rows = _notifications_for_user(db, survivor)
    assert len(rows) == 1
    notif_id, _, user_id, telegram_id, status = rows[0]
    assert status == "Sent", "the further-along (Sent) row must be kept"
    assert user_id == survivor, "the kept row must be owned by the survivor"
    assert telegram_id == "12345"
    assert notif_id == "nid-dup", "the kept row is the duplicate's own Sent row"


def test_tie_keeps_survivor_row(db):
    """Tie (same status): keep the survivor's row to stay deterministic,
    drop the duplicate's."""
    old, survivor = _seed_duplicate_users(db)
    _insert_user_notification(db, "nid-dup", "job-t", old, "12345", "RateLimited")
    _insert_user_notification(db, "nid-sur", "job-t", survivor, "12345", "RateLimited")

    db._migrate_user_uniqueness()

    rows = _notifications_for_user(db, survivor)
    assert [row[0] for row in rows] == ["nid-sur"], "ties keep the survivor"


def test_precedence_order_is_sent_sending_pending_ratelimited_failed_cancelled(db):
    """A "Pending" duplicate must outrank a "Cancelled" survivor's row,
    and "Failed" must lose to a "Pending" duplicate's row."""
    old_a, survivor = _seed_duplicate_users(db, labels=("old-a", "newest"))
    _insert_user_notification(db, "nid-dup-pending", "job-p", old_a, "12345", "Pending")
    _insert_user_notification(db, "nid-sur-cancelled", "job-p", survivor, "12345", "Cancelled")
    db._migrate_user_uniqueness()

    rows = _notifications_for_user(db, survivor)
    assert [row[4] for row in rows] == ["Pending"], (
        "Pending (3) outranks Cancelled (0)"
    )


def test_migration_is_idempotent_and_reestablishes_unique_index(db):
    old, survivor = _seed_duplicate_users(db)
    _insert_user_notification(db, "nid-dup", "job-x", old, "12345", "Pending")
    _insert_user_notification(db, "nid-sur", "job-y", survivor, "12345", "Sent")

    db._migrate_user_uniqueness()
    db._migrate_user_uniqueness()  # second run must be a clean no-op

    assert db._conn.execute(
        "SELECT COUNT(*) FROM users WHERE \"Telegram User ID\" = '12345'"
    ).fetchone()[0] == 1
    assert db._conn.execute(
        'SELECT COUNT(*) FROM sqlite_master '
        "WHERE type = 'index' AND name = 'idx_users_telegram_user_id'"
    ).fetchone()[0] == 1


def test_dup_with_no_queued_notifications_still_dedupes(db):
    old, survivor = _seed_duplicate_users(db)
    db._migrate_user_uniqueness()
    assert db._conn.execute(
        "SELECT COUNT(*) FROM users WHERE \"Telegram User ID\" = '12345'"
    ).fetchone()[0] == 1
    assert db._conn.execute(
        'SELECT "User ID" FROM users WHERE "Telegram User ID" = ?', ("12345",)
    ).fetchone()[0] == survivor


# ----------------------------------------------------------------------
# Narrowed OperationalError handling
# ----------------------------------------------------------------------

def test_legacy_migration_swallows_only_missing_table(db):
    """A real operational failure ("database is locked") must propagate
    from the legacy notification-states migration -- not be mistaken for
    "nothing to migrate"."""
    _conn_proxy = _LockedOnPrefix(db._conn, 'SELECT rowid, "Notification Status" FROM jobs')
    db._conn = _conn_proxy
    try:
        with pytest.raises(sqlite3.OperationalError):
            db._migrate_legacy_notification_states()
    finally:
        db._conn = _conn_proxy._real


def test_user_categories_migration_swallows_only_missing_table(db):
    _conn_proxy = _LockedOnPrefix(db._conn, 'SELECT "User ID", "Category ID" FROM user_categories')
    db._conn = _conn_proxy
    try:
        with pytest.raises(sqlite3.OperationalError):
            db._migrate_user_categories_into_users()
    finally:
        db._conn = _conn_proxy._real


def test_both_legacy_migrations_are_noops_on_fresh_db(db):
    """A fresh database (no legacy tables, no legacy statuses) must run
    both migrations as clean no-ops."""
    db._migrate_legacy_notification_states()  # no rows to normalize
    db._migrate_user_categories_into_users()  # no user_categories table


# ----------------------------------------------------------------------
# FIX 2 Regression: User migration active state merge
# ----------------------------------------------------------------------
#
# The survivor (newest row by rowid) is the authoritative record. Its
# explicit Is Active state must NOT be overridden by an older duplicate's
# active state -- that would silently reactivate a user whose newer state
# is inactive/stopped (subscription lifecycle semantics). Only preferences
# (Categories, Sources) are merged; active state and destination type
# remain the survivor's.

def _seed_duplicate_users_with_active(
    db,
    telegram_id="12345",
    old_active="1",
    survivor_active="0",
    labels=("old", "newest"),
):
    """Insert two users rows sharing one Telegram User ID with specific
    Is Active values. Returns (old_user_id, survivor_user_id)."""
    db._conn.execute("DROP INDEX IF EXISTS idx_users_telegram_user_id")
    user_ids = []
    active_values = [old_active, survivor_active]
    for label, active in zip(labels, active_values):
        user_id = f"user-{label}"
        db._conn.execute(
            'INSERT INTO users ("User ID", "Telegram User ID", "Username", '
            '"First Name", "Destination Type", "Is Active", '
            '"Created At", "Updated At") '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                telegram_id,
                label,
                label,
                "user",
                active,
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        user_ids.append(user_id)
    return user_ids


def _get_user_active(db, user_id):
    row = db._conn.execute(
        'SELECT "Is Active" FROM users WHERE "User ID" = ?', (user_id,)
    ).fetchone()
    return row[0] if row else None


def test_survivor_inactive_not_reactivated_by_old_active_duplicate(db):
    """Old duplicate: Is Active = 1, Newest survivor: Is Active = 0
    -> Survivor MUST remain Is Active = 0. The survivor's explicit
    inactive state is authoritative and must not be overridden by an
    older duplicate's active state."""
    old, survivor = _seed_duplicate_users_with_active(
        db, old_active="1", survivor_active="0"
    )

    db._migrate_user_uniqueness()

    assert _get_user_active(db, survivor) == "0", (
        "survivor's inactive state must be preserved; old active duplicate "
        "must not reactivate a stopped user"
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM users WHERE \"Telegram User ID\" = '12345'"
    ).fetchone()[0] == 1


def test_survivor_active_not_deactivated_by_old_inactive_duplicate(db):
    """Old duplicate: Is Active = 0, Newest survivor: Is Active = 1
    -> Survivor MUST remain Is Active = 1. The inverse case: an old
    inactive duplicate must not deactivate an actively subscribed user."""
    old, survivor = _seed_duplicate_users_with_active(
        db, old_active="0", survivor_active="1"
    )

    db._migrate_user_uniqueness()

    assert _get_user_active(db, survivor) == "1", (
        "survivor's active state must be preserved; old inactive duplicate "
        "must not deactivate a subscribed user"
    )
    assert db._conn.execute(
        "SELECT COUNT(*) FROM users WHERE \"Telegram User ID\" = '12345'"
    ).fetchone()[0] == 1


def test_survivor_active_state_preserved_when_both_active(db):
    """Both old and survivor active -> survivor stays active."""
    old, survivor = _seed_duplicate_users_with_active(
        db, old_active="1", survivor_active="1"
    )

    db._migrate_user_uniqueness()

    assert _get_user_active(db, survivor) == "1"


def test_survivor_active_state_preserved_when_both_inactive(db):
    """Both old and survivor inactive -> survivor stays inactive."""
    old, survivor = _seed_duplicate_users_with_active(
        db, old_active="0", survivor_active="0"
    )

    db._migrate_user_uniqueness()

    assert _get_user_active(db, survivor) == "0"


def test_categories_sources_merged_but_active_preserved(db):
    """Categories and Sources from old duplicate are merged into survivor,
    but active state remains survivor's."""
    db._conn.execute("DROP INDEX IF EXISTS idx_users_telegram_user_id")
    # Old duplicate has different categories/sources and is active
    db._conn.execute(
        'INSERT INTO users ("User ID", "Telegram User ID", "Username", '
        '"First Name", "Destination Type", "Is Active", "Categories", "Sources", '
        '"Created At", "Updated At") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "user-old",
            "12345",
            "old",
            "old",
            "user",
            "1",
            "cat_a,cat_b",
            "src_x",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )
    # Survivor is inactive with different categories/sources
    db._conn.execute(
        'INSERT INTO users ("User ID", "Telegram User ID", "Username", '
        '"First Name", "Destination Type", "Is Active", "Categories", "Sources", '
        '"Created At", "Updated At") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "user-newest",
            "12345",
            "newest",
            "newest",
            "user",
            "0",
            "cat_c",
            "src_y",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00",
        ),
    )

    db._migrate_user_uniqueness()

    # Survivor's active state preserved
    assert _get_user_active(db, "user-newest") == "0"
    # Categories merged (union)
    cats = db._conn.execute(
        'SELECT "Categories" FROM users WHERE "User ID" = ?', ("user-newest",)
    ).fetchone()[0]
    assert set(cats.split(",")) == {"cat_a", "cat_b", "cat_c"}
    # Sources merged (union)
    sources = db._conn.execute(
        'SELECT "Sources" FROM users WHERE "User ID" = ?', ("user-newest",)
    ).fetchone()[0]
    assert set(sources.split(",")) == {"src_x", "src_y"}
    # Destination type remains survivor's
    dst = db._conn.execute(
        'SELECT "Destination Type" FROM users WHERE "User ID" = ?', ("user-newest",)
    ).fetchone()[0]
    assert dst == "user"