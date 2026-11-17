"""Process ownership for a bridge database connection.

PostgreSQL advisory locks belong to a *session*, so this object deliberately
uses the connection it was given.  It never opens (or asks the adapter to
open) another connection: losing that session also loses the lease.
"""
from __future__ import annotations

import sqlite3
from typing import Any


# This is a single, signed PostgreSQL bigint (rather than a pair of int4s).
# It is deliberately constant: all bridge processes contend for one lease.
ADVISORY_LOCK_KEY = 714326598142907381


class DatabaseOwnershipLost(RuntimeError):
    """The PostgreSQL connection which owned the bridge lease changed."""

    def __init__(self) -> None:
        # Keep this message static.  In particular, never put a database URL
        # or a driver exception (which can contain one) in it.
        super().__init__("Database ownership lost")


_MISSING = object()


class DatabaseOwner:
    """Own the PostgreSQL bridge lease on an existing database connection.

    SQLite is single-process for this purpose and is intentionally a no-op.
    For PostgreSQL, callers must call :meth:`try_acquire` before doing schema
    setup or recovery, and use :meth:`check` as their write-side guard.
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self._sqlite = isinstance(db, sqlite3.Connection)
        self._postgres = getattr(db, "dialect", None) == "postgresql"
        if not (self._sqlite or self._postgres):
            raise TypeError("DatabaseOwner requires SQLite or a PostgreSQL adapter")

        # SQLite needs no lease and remains active even after release().
        self.active = self._sqlite
        self._generation: Any = _MISSING
        if hasattr(db, "set_owner_guard"):
            db.set_owner_guard(lambda: self.active and self._same_generation())

    def _current_generation(self) -> Any:
        """Return the adapter generation, retaining 'not supported' distinctly."""
        return getattr(self.db, "generation", _MISSING)

    def _same_generation(self) -> bool:
        return self._generation is _MISSING or self._current_generation() == self._generation

    @staticmethod
    def _value(cursor: Any) -> bool:
        """Read ``owned`` from sqlite, the bridge cursor facade, or a DB-API cursor."""
        row = cursor.fetchone()
        if row is None:
            return False
        try:
            return bool(row["owned"])
        except (KeyError, IndexError, TypeError):
            return bool(row[0])

    def try_acquire(self) -> bool:
        """Try once to acquire the lease; never take a counted lock twice.

        Database execution errors are deliberately allowed to propagate.  A
        caller can therefore distinguish an unavailable database from a live
        standby which simply received ``False``.
        """
        if self._sqlite:
            return True
        if self.active:
            return True

        # Do not catch this: adapters are responsible for their credential-free
        # database errors, and silently treating one as standby is unsafe.
        cursor = self.db.execute(
            "SELECT pg_try_advisory_lock(%d) AS owned" % ADVISORY_LOCK_KEY
        )
        if not self._value(cursor):
            return False

        # Capture only after acquisition.  This identifies the exact session
        # that owns a session-scoped advisory lock.
        self._generation = self._current_generation()
        self.active = True
        if hasattr(self.db, "pin_session"):
            self.db.pin_session()
        return True

    def check(self) -> bool:
        """Verify a live lease without reconnecting or reacquiring it.

        ``False`` means this instance was never the owner (or was released).
        A changed adapter generation is an ownership loss and raises the
        deliberately static exception.  Driver errors propagate, but the
        local lease is revoked first so a later operation cannot proceed.
        """
        if self._sqlite:
            return True
        if not self.active:
            return False
        if not self._same_generation():
            self.active = False
            raise DatabaseOwnershipLost()

        try:
            self.db.execute("SELECT 1")
        except Exception:
            # A failed probe may coincide with a driver reconnect.  Never
            # retain permission to write after it.
            self.active = False
            raise

        if not self._same_generation():
            self.active = False
            raise DatabaseOwnershipLost()
        return True

    def release(self) -> None:
        """Best-effort release on the original PostgreSQL session.

        Unlock failures are intentionally ignored; connection close is the
        caller's final cleanup.  Never send an unlock through a replacement
        generation, since it could affect a different lease/session.
        """
        if self._sqlite or not self.active:
            return
        if not self._same_generation():
            self.active = False
            return
        try:
            self.db.execute("SELECT pg_advisory_unlock(%d) AS owned" % ADVISORY_LOCK_KEY)
        except Exception:
            pass
        finally:
            self.active = False
