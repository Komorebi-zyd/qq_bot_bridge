import sqlite3

import pytest

from bridge.ownership import DatabaseOwner, DatabaseOwnershipLost


class Result:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return {"owned": self.value}


class FakePostgres:
    dialect = "postgresql"

    def __init__(self, locks=None):
        self.generation = 1
        self.locks = locks if locks is not None else set()
        self.queries = []
        self.fail = None

    def execute(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise self.fail
        if "pg_try_advisory_lock" in sql:
            if self.locks:
                return Result(False)
            self.locks.add("bridge")
            return Result(True)
        if "pg_advisory_unlock" in sql:
            self.locks.discard("bridge")
            return Result(True)
        assert sql == "SELECT 1"
        return Result(True)


def test_sqlite_is_an_active_noop():
    owner = DatabaseOwner(sqlite3.connect(":memory:"))

    assert owner.active is True
    assert owner.try_acquire() is True
    assert owner.check() is True
    owner.release()
    assert owner.active is True


def test_postgres_acquires_once_and_does_not_count_lock_twice():
    db = FakePostgres()
    owner = DatabaseOwner(db)

    assert owner.active is False
    assert owner.try_acquire() is True
    assert owner.try_acquire() is True
    assert owner.active is True
    assert len(db.queries) == 1
    assert "pg_try_advisory_lock" in db.queries[0]


def test_second_owner_is_standby_until_first_releases():
    locks = set()
    first_db, second_db = FakePostgres(locks), FakePostgres(locks)
    first, second = DatabaseOwner(first_db), DatabaseOwner(second_db)

    assert first.try_acquire() is True
    assert second.try_acquire() is False
    assert second.active is False
    assert len(second_db.queries) == 1

    first.release()
    assert second.try_acquire() is True


def test_check_revokes_and_raises_when_generation_changes_before_probe():
    db = FakePostgres()
    owner = DatabaseOwner(db)
    assert owner.try_acquire()
    db.generation += 1

    with pytest.raises(DatabaseOwnershipLost, match="^Database ownership lost$"):
        owner.check()
    assert owner.active is False
    assert len(db.queries) == 1  # no probe through a replacement session


def test_check_detects_generation_change_caused_by_probe():
    class ReconnectingFake(FakePostgres):
        def execute(self, sql):
            result = super().execute(sql)
            if sql == "SELECT 1":
                self.generation += 1
            return result

    db = ReconnectingFake()
    owner = DatabaseOwner(db)
    assert owner.try_acquire()

    with pytest.raises(DatabaseOwnershipLost):
        owner.check()
    assert owner.active is False


def test_check_revokes_but_does_not_mask_database_error():
    db = FakePostgres()
    owner = DatabaseOwner(db)
    assert owner.try_acquire()
    db.fail = RuntimeError("driver failure")

    with pytest.raises(RuntimeError, match="driver failure"):
        owner.check()
    assert owner.active is False


def test_release_is_best_effort_and_never_unlocks_new_generation():
    db = FakePostgres()
    owner = DatabaseOwner(db)
    assert owner.try_acquire()
    db.generation += 1

    owner.release()
    assert owner.active is False
    assert len(db.queries) == 1


def test_release_suppresses_unlock_failure():
    db = FakePostgres()
    owner = DatabaseOwner(db)
    assert owner.try_acquire()
    db.fail = RuntimeError("network gone")

    owner.release()
    assert owner.active is False
