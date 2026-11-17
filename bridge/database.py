"""SQLite by default, or an explicitly configured PostgreSQL connection.

The PostgreSQL adapter accepts portable SQL, not SQLite DML. Only qmark
parameters and the bridge's internal SQLite schema need translation here.
No statements are retried and no singleton/advisory locks are acquired.
"""
from __future__ import annotations

import re
import logging
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit


class DatabaseError(RuntimeError):
    """A deliberately credential-free database failure."""


def _parts(sql):
    """Yield (protected, text) spans, keeping literals/comments untouched."""
    start = i = 0
    n = len(sql)
    while i < n:
        end = None
        if sql.startswith('--', i):
            pos = sql.find('\n', i + 2)
            end = n if pos < 0 else pos + 1
        elif sql.startswith('/*', i):
            j, depth = i + 2, 1
            while j < n and depth:
                if sql.startswith('/*', j):
                    depth += 1
                    j += 2
                elif sql.startswith('*/', j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            end = j
        elif sql[i] in "'\"":
            quote = sql[i]
            escaped = (quote == "'" and i > 0 and sql[i - 1] in 'eE'
                       and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == '_')))
            j = i + 1
            while j < n:
                if escaped and sql[j] == '\\':
                    j += 2
                elif sql[j] == quote:
                    j += 1
                    if j < n and sql[j] == quote:
                        j += 1
                    else:
                        break
                else:
                    j += 1
            end = min(j, n)
        elif sql[i] == '$':
            match = re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', sql[i:])
            if match:
                tag = match.group()
                pos = sql.find(tag, i + len(tag))
                end = n if pos < 0 else pos + len(tag)
        if end is None:
            i += 1
            continue
        if start < i:
            yield False, sql[start:i]
        yield True, sql[i:end]
        i = start = end
    if start < n:
        yield False, sql[start:]


def _qmarks(sql, bound):
    if not bound:
        return sql
    # Psycopg interprets percent signs even inside SQL literals when binding.
    return ''.join(text.replace('%', '%%') if protected else
                   text.replace('%', '%%').replace('?', '%s')
                   for protected, text in _parts(sql))


def _schema_statements(script):
    statements, current = [], ''
    for protected, text in _parts(script):
        if protected:
            current += text
            continue
        chunks = text.split(';')
        for chunk in chunks[:-1]:
            statements.append(current + chunk)
            current = ''
        current += chunks[-1]
    statements.append(current)
    for statement in statements:
        # Comments have no schema semantics (but quoted strings do).
        statement = ''.join(' ' if protected and text.startswith(('--', '/*')) else text
                            for protected, text in _parts(statement)).strip()
        if not statement:
            continue
        if re.match(r'PRAGMA\b', statement, re.I):
            if re.fullmatch(r'PRAGMA\s+(?:journal_mode\s*=\s*WAL|'
                            r'synchronous\s*=\s*FULL|busy_timeout\s*=\s*5000)\s*',
                            statement, re.I):
                continue
            raise DatabaseError('Unsupported SQLite schema directive') from None
        def translate(text):
            text = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
                          'BIGSERIAL PRIMARY KEY', text, flags=re.I)
            text = re.sub(r'\bINTEGER\b', 'BIGINT', text, flags=re.I)
            return re.sub(r'\bREAL\b', 'DOUBLE PRECISION', text, flags=re.I)
        yield ''.join(text if protected else translate(text)
                      for protected, text in _parts(statement))


class Row:
    """Sequence iteration plus name lookup, like sqlite3.Row (including dict())."""
    __slots__ = ('_names', '_values')

    def __init__(self, names, values):
        self._names = tuple(names)
        self._values = tuple(values)

    def keys(self):
        return list(self._names)

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._values)

    def __getitem__(self, key):
        if isinstance(key, str):
            for i, name in enumerate(self._names):
                if name.lower() == key.lower():
                    return self._values[i]
            raise IndexError('No item with that key')
        return self._values[key]


class Cursor:
    """Fully buffered results: no driver cursor or implicit transaction survives."""
    def __init__(self, names=(), rows=(), rowcount=-1):
        self._rows = [Row(names, row) for row in rows]
        self._position = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


def _connection_arguments(url):
    try:
        if not isinstance(url, str) or any(char.isspace() for char in url):
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme not in ('postgres', 'postgresql') or not parsed.hostname
                or not parsed.path.startswith('/') or not unquote(parsed.path[1:]).strip()
                or parsed.fragment):
            raise ValueError
        # Accessing port also validates malformed/non-numeric ports.
        if parsed.port is not None and not 0 < parsed.port < 65536:
            raise ValueError
        options = ' '.join(value for key, value in parse_qsl(parsed.query)
                           if key == 'options')
    except Exception:
        raise DatabaseError('Invalid PostgreSQL database URL') from None
    return dict(autocommit=True, connect_timeout=5, application_name='qq_bot_bridge',
                options=(options + ' -c statement_timeout=10000 -c lock_timeout=5000').strip())


class PostgresConnection:
    """Small sqlite-style connection facade; explicit contexts use savepoints."""
    dialect = 'postgresql'

    def __init__(self, url):
        self.generation = 0
        self._pinned = False
        self._owner_guard = None
        self._arguments = _connection_arguments(url)
        self._url = url
        self._transactions = []
        self._closed = False
        self._conn = self._connect()

    def _connect(self):
        try:
            import psycopg
            logging.getLogger('psycopg').disabled = True
            for name, logger in list(logging.Logger.manager.loggerDict.items()):
                if isinstance(logger, logging.Logger) and name.startswith('psycopg.'):
                    logger.disabled = True
            connection = psycopg.connect(self._url, **self._arguments)
            self.generation += 1
            return connection
        except Exception:
            raise DatabaseError('Could not connect to PostgreSQL database') from None

    def _ready(self):
        if self._closed:
            raise DatabaseError('Database connection is closed') from None
        if self._conn.closed or self._conn.broken:
            if self._pinned:
                raise DatabaseError('PostgreSQL ownership session lost') from None
            if self._transactions:
                raise DatabaseError('PostgreSQL connection lost during transaction') from None
            # Only reconnect before an operation. Never replay an attempted statement.
            self._conn = self._connect()

    def set_owner_guard(self, guard):
        self._owner_guard = guard

    def pin_session(self):
        self._pinned = True

    def unpin_session(self):
        self._pinned = False

    def reset_session(self):
        # Closing releases any advisory lock even after a failed ownership probe.
        # The next operation establishes a fresh session before acquisition.
        self._pinned = False
        self._transactions.clear()
        try:
            self._conn.close()
        except Exception:
            pass

    def execute(self, sql, parameters=None):
        ownership_query = bool(re.fullmatch(r'SELECT pg_(?:try_advisory_lock|advisory_unlock)\(\d+\) AS owned', sql))
        if not ownership_query and self._owner_guard is not None and not self._owner_guard():
            raise DatabaseError('Database session is not the active bridge owner') from None
        self._ready()
        try:
            cursor = self._conn.execute(_qmarks(sql, parameters is not None), parameters)
            try:
                names = [column.name for column in cursor.description] if cursor.description else []
                rows = cursor.fetchall() if cursor.description else []
                return Cursor(names, rows, cursor.rowcount)
            finally:
                cursor.close()
        except Exception:
            raise DatabaseError('PostgreSQL statement failed') from None

    def executescript(self, script):
        result = Cursor()
        for statement in _schema_statements(script):
            result = self.execute(statement)
        return result

    def __enter__(self):
        self._ready()
        try:
            transaction = self._conn.transaction()
            transaction.__enter__()
            self._transactions.append(transaction)
        except Exception:
            raise DatabaseError('Could not begin PostgreSQL transaction') from None
        return self

    def __exit__(self, exc_type, exc, traceback):
        transaction = self._transactions.pop()
        try:
            return transaction.__exit__(exc_type, exc, traceback)
        except Exception:
            raise DatabaseError('Could not finish PostgreSQL transaction') from None

    def commit(self):
        try:
            self._conn.commit()
        except Exception:
            raise DatabaseError('Could not commit PostgreSQL transaction') from None

    def rollback(self):
        try:
            self._conn.rollback()
        except Exception:
            raise DatabaseError('Could not roll back PostgreSQL transaction') from None

    def close(self):
        self._closed = True
        try:
            self._conn.close()
        except Exception:
            raise DatabaseError('Could not close PostgreSQL database') from None


def open_database(cfg):
    """Use cfg.database_url when set, otherwise cfg.data / 'bridge.sqlite3'.

    SQLite remains a native connection with sqlite3.Row and cross-thread access.
    The caller creates cfg.data. PostgreSQL driver import is deliberately lazy.
    """
    url = getattr(cfg, 'database_url', '')
    if url:
        return PostgresConnection(url)
    connection = sqlite3.connect(Path(cfg.data) / 'bridge.sqlite3', check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection
