"""Offline adapter tests. No PostgreSQL server or driver is needed."""
import ast
from pathlib import Path
import sqlite3
import sys
import traceback
from types import SimpleNamespace

import pytest

from bridge.database import (
    Cursor, DatabaseError, Row, _qmarks, _schema_statements, open_database,
)


SECRET = 'driver failure with postgres://private:secret@private/db and parameters'
URL = 'postgresql://user:p%40ss%25word@db.example/bridge?sslmode=require'


class FakeCursor:
    def __init__(self, rows=(), names=(), rowcount=0):
        self.description = [SimpleNamespace(name=name) for name in names] or None
        self.rows = rows
        self.rowcount = rowcount
        self.closed = False

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.events.append('begin')
        if self.connection.fail_enter:
            raise RuntimeError(SECRET)
        return self

    def __exit__(self, typ, value, tb):
        self.connection.events.append('rollback' if typ else 'commit')
        if self.connection.fail_exit:
            raise RuntimeError(SECRET)
        return False


class FakeConnection:
    def __init__(self):
        self.closed = self.broken = False
        self.calls = []
        self.events = []
        self.cursors = []
        self.fail_execute = self.fail_enter = self.fail_exit = False
        self.fail_fetch = self.fail_method = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail_execute:
            self.broken = True
            raise RuntimeError(SECRET)
        cursor = FakeCursor([(7, 'hello')], ['seq', 'payload'], 1) if sql.startswith('SELECT') else FakeCursor(rowcount=2)
        if self.fail_fetch:
            def fail():
                raise RuntimeError(SECRET)
            cursor.fetchall = fail
        self.cursors.append(cursor)
        return cursor

    def transaction(self):
        return FakeTransaction(self)

    def commit(self):
        if self.fail_method:
            raise RuntimeError(SECRET)
        self.events.append('explicit commit')

    def rollback(self):
        if self.fail_method:
            raise RuntimeError(SECRET)
        self.events.append('explicit rollback')

    def close(self):
        if self.fail_method:
            raise RuntimeError(SECRET)
        self.closed = True


@pytest.fixture
def driver(monkeypatch):
    driver = SimpleNamespace(calls=[], connections=[], fail=False)

    def connect(url, **kwargs):
        driver.calls.append((url, kwargs))
        if driver.fail:
            raise RuntimeError(SECRET)
        connection = FakeConnection()
        driver.connections.append(connection)
        return connection

    driver.connect = connect
    monkeypatch.setitem(sys.modules, 'psycopg', driver)
    return driver


def open_pg(url=URL):
    return open_database(SimpleNamespace(database_url=url))


def safe_failure(call, message=None):
    with pytest.raises(DatabaseError) as error:
        call()
    rendered = ''.join(traceback.format_exception(error.type, error.value, error.tb))
    assert SECRET not in rendered
    assert 'p%40ss' not in rendered
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    if message:
        assert str(error.value) == message


@pytest.mark.parametrize('url', ['', None])
def test_native_sqlite(tmp_path, url):
    db = open_database(SimpleNamespace(data=tmp_path, database_url=url))
    assert isinstance(db, sqlite3.Connection)
    assert db.row_factory is sqlite3.Row
    db.executescript('PRAGMA journal_mode=WAL; CREATE TABLE items(seq INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT);')
    with db:
        db.execute('INSERT INTO items(value) VALUES (?)', ('a',))
    row = db.execute('SELECT * FROM items').fetchone()
    assert dict(row) == {'seq': 1, 'value': 'a'}
    assert (tmp_path / 'bridge.sqlite3').exists()
    db.close()


def test_missing_url_setting_is_sqlite(tmp_path):
    db = open_database(SimpleNamespace(data=tmp_path))
    assert isinstance(db, sqlite3.Connection)
    db.close()


@pytest.mark.parametrize('url', [
    'mysql://host/db', 'sqlite:///db', 'https://host/db', 'postgresql://',
    'postgres://host', 'postgres://host/', 'postgres:///db',
    'postgres://host:abc/db', 'postgres://host:99999/db',
    'postgres://host/db#secret', 'postgres://host/my db',
    ' postgresql://host/db', 'postgres://host/db\n',
])
def test_invalid_url_never_connects_or_falls_back(driver, url):
    safe_failure(lambda: open_pg(url), 'Invalid PostgreSQL database URL')
    assert not driver.calls


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql'])
def test_connect_options_and_encoded_password(driver, scheme):
    url = f'{scheme}://user:p%40ss%25word@host/bridge?sslmode=verify-full&sslrootcert=%2Fcert&options=-c%20search_path%3Dbridge'
    db = open_pg(url)
    passed_url, kwargs = driver.calls[0]
    assert passed_url == url  # Preserve SSL and encoded connection parameters.
    assert kwargs == dict(autocommit=True, connect_timeout=5,
                          application_name='qq_bot_bridge',
                          options='-c search_path=bridge -c statement_timeout=10000 -c lock_timeout=5000')
    assert not driver.connections[0].calls  # No implicit advisory locks.
    db.close()


def test_connect_error_is_sanitized_without_fallback(driver):
    driver.fail = True
    safe_failure(open_pg, 'Could not connect to PostgreSQL database')
    assert len(driver.calls) == 1


def test_missing_optional_driver(monkeypatch):
    monkeypatch.setitem(sys.modules, 'psycopg', None)
    safe_failure(open_pg, 'Could not connect to PostgreSQL database')


@pytest.mark.parametrize(('sql', 'expected'), [
    ('SELECT ?, ?', 'SELECT %s, %s'),
    ('''SELECT '?', "?", ?, 'it''s ?' ''', '''SELECT '?', "?", %s, 'it''s ?' '''),
    ('SELECT "a""?", ? -- ?\n/* ? /* ? */ ? */', 'SELECT "a""?", %s -- ?\n/* ? /* ? */ ? */'),
    ("SELECT $$?%$$, $tag$?$tag$, ?", "SELECT $$?%%$$, $tag$?$tag$, %s"),
    ("SELECT E'it\\'s ?', ?", "SELECT E'it\\'s ?', %s"),
    ("SELECT ? % 2, '50%', '%%', '%s'", "SELECT %s %% 2, '50%%', '%%%%', '%%s'"),
])
def test_qmarks_respect_sql_quoting(sql, expected):
    assert _qmarks(sql, True) == expected
    assert _qmarks(sql, False) == sql


def test_bound_and_unbound_execution(driver):
    db = open_pg()
    db.execute("SELECT '50%', ?", ('private value',))
    db.execute("SELECT '50%' ")
    db.execute('SELECT 1', ())
    assert driver.connections[0].calls == [
        ("SELECT '50%%', %s", ('private value',)), ("SELECT '50%' ", None), ('SELECT 1', ())]
    assert not driver.connections[0].events  # SELECT never opens implicit transactions.


def test_buffered_cursor_and_row_parity(driver):
    db = open_pg()
    cursor = db.execute('SELECT seq,payload FROM messages')
    assert driver.connections[0].cursors[-1].closed
    db.close()
    row = cursor.fetchone()
    assert dict(row) == {'seq': 7, 'payload': 'hello'}
    assert tuple(row) == (7, 'hello')
    assert row[0] == row['seq'] == row['SEQ'] == 7
    assert row[-1] == 'hello'
    assert row[:] == (7, 'hello')
    assert row.keys() == ['seq', 'payload']
    with pytest.raises(IndexError):
        _ = row['missing']
    assert cursor.fetchone() is None
    assert cursor.fetchall() == []
    assert cursor.rowcount == 1
    assert list(Cursor(['n'], [(1,), (2,)]))[1][0] == 2
    cursor = Cursor(['n'], [(1,), (2,)])
    assert cursor.fetchone()[0] == 1
    assert [tuple(row) for row in cursor.fetchall()] == [(2,)]
    assert Row(['same', 'same'], [1, 2])['same'] == 1


def test_dml_is_not_rewritten(driver):
    db = open_pg()
    sql = 'INSERT INTO metadata(namespace,key,value) VALUES (?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value'
    result = db.execute(sql, ('a', 'b', 'c'))
    assert driver.connections[0].calls[-1] == (sql.replace('?', '%s'), ('a', 'b', 'c'))
    assert result.rowcount == 2
    assert result.fetchall() == []
    # Unsupported SQLite DML is intentionally not magically repaired.
    sql = 'INSERT OR IGNORE INTO things VALUES (?)'
    db.execute(sql, (1,))
    assert driver.connections[0].calls[-1][0] == sql.replace('?', '%s')


def test_actual_core_schema(driver):
    # Read only the schema literals; importing core is unnecessary for adapter tests.
    core = Path(__file__).resolve().parents[1] / 'bridge' / 'core.py'
    tree = ast.parse(core.read_text(encoding='utf-8'))
    scripts = [node.args[0].value for node in ast.walk(tree)
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
               and node.func.attr == 'executescript' and node.args
               and isinstance(node.args[0], ast.Constant)]
    assert len(scripts) >= 2
    db = open_pg()
    for script in scripts:
        db.executescript(script)
    sql = '\n'.join(call[0] for call in driver.connections[0].calls)
    assert 'PRAGMA' not in sql
    assert 'AUTOINCREMENT' not in sql
    assert 'INTEGER' not in sql
    assert 'REAL' not in sql
    assert 'seq BIGSERIAL PRIMARY KEY' in sql
    assert 'history_seq BIGSERIAL PRIMARY KEY' in sql
    assert 'reply_to BIGINT' in sql
    assert 'expires DOUBLE PRECISION' in sql
    assert 'CREATE INDEX IF NOT EXISTS' in sql


def test_schema_quoting_and_semicolons():
    script = '''-- start; INTEGER
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=FULL;
    PRAGMA busy_timeout=5000;
    CREATE TABLE "INTEGER"(n INTEGER, r REAL, txt TEXT DEFAULT 'INTEGER;REAL');
    /* comment; */ CREATE INDEX idx ON "INTEGER"(n);'''
    assert list(_schema_statements(script)) == [
        '''CREATE TABLE "INTEGER"(n BIGINT, r DOUBLE PRECISION, txt TEXT DEFAULT 'INTEGER;REAL')''',
        'CREATE INDEX idx ON "INTEGER"(n)']


@pytest.mark.parametrize('pragma', ['PRAGMA foreign_keys=ON;', 'PRAGMA journal_mode=DELETE;',
                                    'PRAGMA busy_timeout=100;', 'PRAGMA synchronous=OFF;'])
def test_only_known_pragmas_are_ignored(driver, pragma):
    db = open_pg()
    safe_failure(lambda: db.executescript(pragma), 'Unsupported SQLite schema directive')
    assert not driver.connections[0].calls


def test_context_commit_rollback_nesting_keeps_connection_open(driver):
    db = open_pg()
    connection = driver.connections[0]
    with db as entered:
        assert entered is db
        db.execute('SELECT 1')
        with db:
            db.execute('SELECT 2')
        try:
            with db:
                raise ValueError('application failure')
        except ValueError:
            pass
    assert connection.events == ['begin', 'begin', 'commit', 'begin', 'rollback', 'commit']
    assert not connection.closed
    with pytest.raises(ValueError, match='application failure'):
        with db:
            raise ValueError('application failure')
    assert connection.events[-1] == 'rollback'
    assert not connection.closed
    db.commit()
    db.rollback()
    assert connection.events[-2:] == ['explicit commit', 'explicit rollback']


@pytest.mark.parametrize('flag', ['fail_enter', 'fail_exit'])
def test_context_driver_errors_sanitized(driver, flag):
    db = open_pg()
    setattr(driver.connections[0], flag, True)
    def run():
        with db:
            pass
    safe_failure(run)
    assert db._transactions == []


def test_context_rollback_failure_sanitizes_original_exception(driver):
    db = open_pg()
    driver.connections[0].fail_exit = True
    def run():
        with db:
            raise RuntimeError(SECRET)
    safe_failure(run, 'Could not finish PostgreSQL transaction')


@pytest.mark.parametrize('flag', ['closed', 'broken'])
def test_reconnect_only_between_operations(driver, flag):
    db = open_pg()
    db.execute('SELECT 1')
    old = driver.connections[0]
    setattr(old, flag, True)
    db.execute('INSERT INTO things VALUES (?)', (1,))
    assert len(driver.calls) == 2
    assert old.calls == [('SELECT 1', None)]
    assert driver.connections[1].calls == [('INSERT INTO things VALUES (%s)', (1,))]


def test_no_reconnect_in_transaction(driver):
    db = open_pg()
    with db:
        connection = driver.connections[0]
        db.execute('SELECT 1')
        connection.broken = True
        safe_failure(lambda: db.execute('SELECT 2'), 'PostgreSQL connection lost during transaction')
    assert len(driver.calls) == 1
    assert len(connection.calls) == 1


def test_write_failure_is_never_replayed(driver):
    db = open_pg()
    old = driver.connections[0]
    old.fail_execute = True
    safe_failure(lambda: db.execute('INSERT INTO things VALUES (?)', ('secret',)),
                 'PostgreSQL statement failed')
    assert len(driver.calls) == 1
    assert old.calls == [('INSERT INTO things VALUES (%s)', ('secret',))]
    # A later independent operation may reconnect, not replay the failed write.
    db.execute('SELECT 1')
    assert len(driver.calls) == 2
    assert driver.connections[1].calls == [('SELECT 1', None)]


def test_fetch_failure_is_sanitized_and_cursor_closed(driver):
    db = open_pg()
    connection = driver.connections[0]
    connection.fail_fetch = True
    safe_failure(lambda: db.execute('SELECT 1'))
    assert connection.cursors[0].closed
    assert len(connection.calls) == 1


@pytest.mark.parametrize('method', ['commit', 'rollback', 'close'])
def test_connection_methods_sanitize_errors(driver, method):
    db = open_pg()
    driver.connections[0].fail_method = True
    safe_failure(getattr(db, method))
    assert len(driver.calls) == 1


def test_explicit_close_never_reopens(driver):
    db = open_pg()
    db.close()
    safe_failure(lambda: db.execute('SELECT 1'), 'Database connection is closed')
    assert len(driver.calls) == 1


def test_ownership_pins_session_and_never_reconnects_while_active(driver):
    db=open_pg()
    assert db.dialect=='postgresql' and db.generation==1
    db.pin_session()
    driver.connections[0].closed=True
    with pytest.raises(DatabaseError,match='ownership session lost'):
        db.execute('UPDATE sends SET status=?',('sent',))
    assert len(driver.connections)==1
    db.reset_session()
    db.execute('SELECT 1')
    assert len(driver.connections)==2 and db.generation==2


def test_settings_never_repr_database_url_or_bot_token():
    from bridge.core import Settings
    cfg=Settings(database_url='postgres://user:secret-private@db.example/bridge',token='secret-private-token')
    assert 'secret-private' not in repr(cfg) and 'db.example' not in repr(cfg)
