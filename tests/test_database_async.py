"""Credential-free checks of the production Store async execution boundary."""
import asyncio
import sqlite3
import threading
import time

import pytest

from bridge.core import Settings, Store
from bridge.database import DatabaseError


def make_store(tmp_path, remote=True):
    store = Store(Settings(data=tmp_path, token='test-token-at-least-16'))
    # Exercise precisely the PostgreSQL executor path, using a real local DB.
    store.remote = remote
    return store


def test_delayed_whole_operation_keeps_event_loop_responsive(tmp_path):
    store = make_store(tmp_path)

    async def run():
        started = threading.Event()
        release = threading.Event()
        loop_thread = threading.get_ident()

        def delayed(*, value):
            assert threading.get_ident() != loop_thread
            with store.db:
                store.db.execute("INSERT INTO metadata VALUES('async','value',?)", (value,))
                started.set()
                assert release.wait(2)
                return store.db.execute("SELECT value FROM metadata WHERE namespace='async'").fetchone()[0]

        task = asyncio.create_task(store.call(delayed, value='ok'))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(.005)
            assert started.is_set(), 'operation did not start off the event loop'
            # The transaction is still running while independent async work runs.
            await asyncio.sleep(.02)
            assert not task.done()
            release.set()
            assert await task == 'ok'
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await store.close()

    asyncio.run(run())


def test_same_store_serializes_entire_transactions(tmp_path):
    store = make_store(tmp_path)
    events, threads = [], set()

    def transaction(index):
        threads.add(threading.get_ident())
        with store.db:
            events.append(('begin', index))
            store.db.execute("INSERT INTO metadata VALUES('serialized',?,?)", (str(index), str(index)))
            time.sleep(.01)
            assert store.db.execute("SELECT COUNT(*) FROM metadata WHERE namespace='serialized'").fetchone()[0] == index + 1
            events.append(('end', index))

    async def run():
        try:
            await asyncio.gather(*(store.call(transaction, i) for i in range(6)))
            assert events == [event for i in range(6) for event in [('begin', i), ('end', i)]]
            assert len(threads) == 1
        finally:
            await store.close()

    asyncio.run(run())


def test_failed_transaction_rolls_back_before_next_operation(tmp_path):
    store = make_store(tmp_path)

    def failing():
        with store.db:
            store.db.execute("INSERT INTO metadata VALUES('rollback','key','value')")
            raise ValueError('test failure')

    async def run():
        try:
            with pytest.raises(ValueError, match='test failure'):
                await store.call(failing)
            assert await store.call(lambda: store.db.execute("SELECT COUNT(*) FROM metadata WHERE namespace='rollback'").fetchone()[0]) == 0
        finally:
            await store.close()

    asyncio.run(run())


def test_cancelled_operation_drains_before_close(tmp_path):
    store = make_store(tmp_path)
    started, release = threading.Event(), threading.Event()
    finished = []

    def operation():
        with store.db:
            started.set()
            assert release.wait(2)
            store.db.execute("INSERT INTO metadata VALUES('cancel','key','value')")
        finished.append(True)

    async def run():
        task = asyncio.create_task(store.call(operation))
        try:
            while not started.is_set():
                await asyncio.sleep(.005)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
            closing = asyncio.create_task(store.close())
            await asyncio.sleep(.02)
            assert not closing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await closing
            assert finished == [True]
            await store.close()  # Idempotent, including executor shutdown.
            with pytest.raises(DatabaseError, match='closed'):
                await store.call(lambda: None)
            with pytest.raises(sqlite3.ProgrammingError):
                store.db.execute('SELECT 1')
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await store.close()

    asyncio.run(run())


def test_sqlite_call_is_direct_and_supports_keywords(tmp_path):
    store = make_store(tmp_path, remote=False)

    async def run():
        try:
            thread = threading.get_ident()
            assert await store.call(lambda value, *, suffix: (threading.get_ident(), value + suffix), 'a', suffix='b') == (thread, 'ab')
        finally:
            await store.close()

    asyncio.run(run())
