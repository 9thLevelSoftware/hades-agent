"""Tests for conservative session leases and abandoned-session reconciliation.

The session_leases table tracks "an agent process claims ownership of this
session row" with a time-bounded TTL. Reclamation is conservative: only
EXPIRED leases are ended, unleased open sessions are untouched. Reconciliation
runs synchronously (no background heartbeat thread) — callers invoke
``reconcile_expired_session_leases(now=...)`` on demand.

Methods under test (SessionDB):
- claim_session_lease(session_id, owner_id, ttl_seconds) -> bool
- touch_session_lease(session_id, owner_id, ttl_seconds) -> bool
- release_session_lease(session_id, owner_id) -> bool
- reconcile_expired_session_leases(now=None) -> int

Fake-clock pattern (monkeypatch hermes_state.time.time) — no real sleeps.
"""

from __future__ import annotations

import time

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "leases.db"
    session_db = SessionDB(db_path=db_path)
    yield session_db
    try:
        session_db.close()
    except Exception:
        pass


@pytest.fixture()
def clock(monkeypatch):
    """Pinned clock — tests advance ``now`` via ``set_time``. No real sleeps."""

    state = {"now": 1_000_000.0}

    def _fake_time():
        return state["now"]

    monkeypatch.setattr(hermes_state.time, "time", _fake_time)

    def set_time(value: float) -> None:
        state["now"] = float(value)

    return set_time


# =========================================================================
# Schema presence
# =========================================================================


class TestSessionLeasesSchema:
    """The session_leases table must be created from SCHEMA_SQL with the
    right shape: PK on session_id, FK cascade to sessions(id), and the
    columns required by the lease methods.

    ponytail: declarative schema check — one PRAGMA round-trip, no fixture
    gymnastics. If a future migration changes the columns the tests fail
    loudly instead of silently losing fields.
    """

    def test_table_exists_in_schema(self, db):
        rows = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_leases'"
        ).fetchall()
        assert rows, "session_leases table missing from SCHEMA_SQL"

    def test_table_columns(self, db):
        cols = {
            row[1] for row in db._conn.execute("PRAGMA table_info(session_leases)")
        }
        assert {"session_id", "owner_id", "acquired_at", "expires_at"} <= cols

    def test_foreign_key_cascade_on_session_delete(self, db):
        db.create_session("s1", "cli")
        db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0)
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s1'"
        ).fetchone() is not None

        db._conn.execute("DELETE FROM sessions WHERE id='s1'")
        db._conn.commit()

        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s1'"
        ).fetchone() is None


# =========================================================================
# claim / touch / release ownership semantics
# =========================================================================


class TestSessionLeaseOwnership:
    """claim/touch/release are all owner-checked — a different owner_id
    must NOT silently take over the lease."""

    def test_claim_when_unleased_succeeds(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        row = db._conn.execute(
            "SELECT owner_id, acquired_at, expires_at FROM session_leases "
            "WHERE session_id='s1'"
        ).fetchone()
        assert row["owner_id"] == "owner-a"
        assert row["expires_at"] - row["acquired_at"] == 60.0

    def test_claim_when_owned_by_other_returns_false(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        assert db.claim_session_lease("s1", "owner-b", ttl_seconds=60.0) is False
        # Existing lease must remain untouched.
        row = db._conn.execute(
            "SELECT owner_id FROM session_leases WHERE session_id='s1'"
        ).fetchone()
        assert row["owner_id"] == "owner-a"

    def test_claim_when_expired_reclaims(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        clock(1_000_000.0 + 120.0)  # beyond expires_at
        assert db.claim_session_lease("s1", "owner-b", ttl_seconds=60.0) is True
        row = db._conn.execute(
            "SELECT owner_id FROM session_leases WHERE session_id='s1'"
        ).fetchone()
        assert row["owner_id"] == "owner-b"

    def test_touch_by_owner_extends(self, db, clock):
        db.create_session("s1", "cli")
        clock(1_000_000.0)
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        clock(1_000_000.0 + 30.0)
        assert db.touch_session_lease("s1", "owner-a", ttl_seconds=120.0) is True
        row = db._conn.execute(
            "SELECT expires_at FROM session_leases WHERE session_id='s1'"
        ).fetchone()
        # expires_at is now=1_000_030 + 120 = 1_000_150
        assert row["expires_at"] == pytest.approx(1_000_150.0)

    def test_touch_by_non_owner_fails(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        assert db.touch_session_lease("s1", "owner-b", ttl_seconds=120.0) is False
        row = db._conn.execute(
            "SELECT owner_id, expires_at FROM session_leases WHERE session_id='s1'"
        ).fetchone()
        # Untouched.
        assert row["owner_id"] == "owner-a"
        assert row["expires_at"] == pytest.approx(1_000_060.0)

    def test_touch_when_no_lease_returns_false(self, db, clock):
        db.create_session("s1", "cli")
        assert db.touch_session_lease("s1", "owner-a", ttl_seconds=60.0) is False

    def test_release_by_owner_succeeds_and_deletes(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        assert db.release_session_lease("s1", "owner-a") is True
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s1'"
        ).fetchone() is None

    def test_release_by_non_owner_returns_false(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        assert db.release_session_lease("s1", "owner-b") is False
        # Still leased.
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s1'"
        ).fetchone() is not None

    def test_release_when_no_lease_returns_false(self, db, clock):
        db.create_session("s1", "cli")
        assert db.release_session_lease("s1", "owner-a") is False


# =========================================================================
# Reconciliation
# =========================================================================


class TestReconcileExpiredSessionLeases:
    """Reconciliation ends only EXPIRED lease rows. Unleased open sessions
    are untouched. The function returns the number of leases ended."""

    def test_ends_expired_lease_and_marks_session(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        clock(1_000_000.0 + 120.0)

        n = db.reconcile_expired_session_leases(now=1_000_120.0)
        assert n == 1

        # Lease row gone, session ended with abandoned reason.
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s1'"
        ).fetchone() is None
        session = db.get_session("s1")
        assert session["ended_at"] == pytest.approx(1_000_060.0)  # = expires_at
        assert session["end_reason"] == "abandoned"

    def test_unleased_open_session_untouched(self, db, clock):
        db.create_session("s1", "cli")
        # No claim made.

        n = db.reconcile_expired_session_leases(now=1_000_000.0 + 999.0)
        assert n == 0

        session = db.get_session("s1")
        assert session["ended_at"] is None
        assert session["end_reason"] is None

    def test_active_lease_untouched(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True

        # Still well within TTL — reconciliation must NOT touch this lease.
        n = db.reconcile_expired_session_leases(now=1_000_000.0 + 30.0)
        assert n == 0
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s1'"
        ).fetchone() is not None
        session = db.get_session("s1")
        assert session["ended_at"] is None

    def test_mixed_lease_states(self, db, clock):
        db.create_session("s-expired", "cli")
        db.create_session("s-active", "cli")
        db.create_session("s-unleased", "cli")

        # s-active uses a long TTL so it's still live when we reconcile.
        assert db.claim_session_lease("s-active", "owner-b", ttl_seconds=3600.0) is True
        # s-expired uses the default short TTL — expires at 1_000_060.
        assert db.claim_session_lease("s-expired", "owner-a", ttl_seconds=60.0) is True
        # No lease for s-unleased.

        # Advance past s-expired's TTL but well before s-active's.
        n = db.reconcile_expired_session_leases(now=1_000_070.0)
        assert n == 1

        # s-expired: ended abandoned.
        expired = db.get_session("s-expired")
        assert expired["end_reason"] == "abandoned"

        # s-active: still leased, still open.
        active = db.get_session("s-active")
        assert active["ended_at"] is None
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='s-active'"
        ).fetchone() is not None

        # s-unleased: still open.
        unleased = db.get_session("s-unleased")
        assert unleased["ended_at"] is None
        assert unleased["end_reason"] is None

    def test_already_ended_session_not_re_ended(self, db, clock):
        """If the session is already ended for some other reason
        (compression, agent_close, ...), reconciliation must NOT overwrite
        end_reason — leases reclaim only rows with a live session."""
        db.create_session("s1", "cli")
        db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0)
        db.end_session("s1", "compression")
        prior_ended = db.get_session("s1")["ended_at"]

        n = db.reconcile_expired_session_leases(now=1_000_000.0 + 999.0)
        assert n == 1  # lease was reclaimed
        # Session end_reason must remain 'compression' — abandonment loses.
        session = db.get_session("s1")
        assert session["end_reason"] == "compression"
        assert session["ended_at"] == pytest.approx(prior_ended)

    def test_now_defaults_to_time_time(self, db, clock, monkeypatch):
        db.create_session("s1", "cli")
        db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0)
        clock(1_000_000.0 + 120.0)  # also advances the hermes_state.time patch

        n = db.reconcile_expired_session_leases()  # no `now=` argument
        assert n == 1
        assert db.get_session("s1")["end_reason"] == "abandoned"

    def test_lease_taken_when_session_does_not_exist(self, db, clock):
        """No-op: claim must not crash for unknown sessions; returns False
        so callers can distinguish 'could not lease' from 'leased'."""
        assert db.claim_session_lease("ghost", "owner-a", ttl_seconds=60.0) is False
        assert db._conn.execute(
            "SELECT 1 FROM session_leases WHERE session_id='ghost'"
        ).fetchone() is None


# =========================================================================
# Lifecycle (claim → touch → touch → release)
# =========================================================================


class TestSessionLeaseLifecycle:
    """Mirrors what AIAgent does: first turn claims, subsequent turns touch,
    close releases. The three-call sequence must be a no-op on the session
    row (the close path's ``end_session`` is what marks ended_at — not the
    lease itself)."""

    def test_first_claim_then_touches_then_release(self, db, clock):
        db.create_session("s1", "cli")

        # First turn: claim.
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=120.0) is True
        clock(1_000_030.0)

        # Subsequent turns: touch.
        for i in range(5):
            assert db.touch_session_lease("s1", "owner-a", ttl_seconds=120.0) is True
            clock(1_000_030.0 + (i + 1) * 10.0)

        # Close: release.
        assert db.release_session_lease("s1", "owner-a") is True

        # The session row itself remains open — only end_session marks it.
        session = db.get_session("s1")
        assert session["ended_at"] is None
        assert session["end_reason"] is None

    def test_release_after_touch_re_enables_claim_by_other(self, db, clock):
        db.create_session("s1", "cli")
        assert db.claim_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        assert db.touch_session_lease("s1", "owner-a", ttl_seconds=60.0) is True
        assert db.release_session_lease("s1", "owner-a") is True
        # owner-b can now claim cleanly (not via expiry reclaim).
        assert db.claim_session_lease("s1", "owner-b", ttl_seconds=60.0) is True