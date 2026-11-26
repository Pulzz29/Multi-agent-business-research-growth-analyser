"""
Database access layer: the one place that opens database connections.

Backends (``DB_BACKEND``):

* ``postgres`` (default): any PostgreSQL-compatible server, configured by
  ``DATABASE_URL`` or ``DB_HOST``/``DB_PORT``/``DB_NAME``/``DB_USER``/
  ``DB_PASSWORD`` (+ ``DB_SSLMODE``, ``DB_SSLROOTCERT``). Connections come
  from a ``psycopg_pool`` pool with connect and statement timeouts.
* ``sqlite``: the original single-file database (``sqlite_manager``), kept
  for rollback and as the source of the SQLite-to-PostgreSQL migration.

Application code uses one SQL style on both backends:

* ``?`` placeholders (translated to ``%s`` for PostgreSQL here);
* standard SQL that both engines accept: ``INSERT ... ON CONFLICT DO
  NOTHING``, ``RETURNING id``, ``CASE`` instead of two-argument ``MAX``;
* the helpers below for the few dialect differences (``json_text``,
  ``table_exists``, ``table_columns``).

``with connect() as conn`` commits on success and rolls back on error, like
``sqlite3``; on PostgreSQL the connection then returns to the pool. Rows
support ``row["column"]``, ``row[0]``, ``dict(row)`` and ``row.keys()``.
Credentials are never logged.
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger("insightiq.db")

_POOLS = {}
_POOLS_GUARD = threading.Lock()


# ------------------------------------------------------------------ errors

def _psycopg():
    import psycopg

    return psycopg


def _error_classes(name):
    classes = [getattr(sqlite3, name)]
    try:
        classes.append(getattr(_psycopg(), name))
    except ImportError:  # psycopg not installed: SQLite-only installation
        pass
    return tuple(classes)


# Use these in ``except`` clauses instead of sqlite3/psycopg classes.
Error = _error_classes("Error")
IntegrityError = _error_classes("IntegrityError")
OperationalError = _error_classes("OperationalError")


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached (connection or pool timeout)."""


# --------------------------------------------------------------- settings

def backend():
    from app.core.config import get_settings

    return get_settings().db_backend


def is_postgres():
    return backend() == "postgres"


def postgres_conninfo(dbname=None, schema=None):
    """libpq connection string from DATABASE_URL or the DB_* settings."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from app.core.config import get_settings

    s = get_settings()
    url = s.database_url.get_secret_value().strip() if s.database_url else ""
    params = conninfo_to_dict(url) if url else {
        "host": s.db_host,
        "port": s.db_port,
        "dbname": s.db_name,
        "user": s.db_user,
        **({"password": s.db_password.get_secret_value()} if s.db_password else {}),
    }
    params.setdefault("sslmode", s.db_sslmode)
    if s.db_sslrootcert:
        params.setdefault("sslrootcert", s.db_sslrootcert)
    params.setdefault("connect_timeout", s.db_connect_timeout_seconds)
    params.setdefault("application_name", "insightiq")
    if dbname:
        params["dbname"] = dbname
    schema = schema or s.db_schema
    options = [f"-c search_path={schema}", "-c timezone=UTC"]
    if s.db_statement_timeout_ms:
        options.append(f"-c statement_timeout={int(s.db_statement_timeout_ms)}")
    params["options"] = " ".join(options)
    return make_conninfo(**{k: v for k, v in params.items() if v not in (None, "")})


def describe_target():
    """Host/port/database/schema of the configured database (no secrets)."""
    from app.core.config import get_settings

    s = get_settings()
    if s.db_backend == "sqlite":
        from app.database.sqlite_manager import working_database_path

        return {"backend": "sqlite", "path": str(working_database_path())}
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(postgres_conninfo())
    return {
        "backend": "postgres", "host": info.get("host"), "port": info.get("port"),
        "database": info.get("dbname"), "schema": s.db_schema, "sslmode": info.get("sslmode"),
    }


# -------------------------------------------------------------------- rows

class Row:
    """Read-only row: ``row["col"]``, ``row[0]``, ``dict(row)``, iteration."""

    __slots__ = ("_values", "_index")

    def __init__(self, values, index):
        self._values = tuple(values)
        self._index = index

    def keys(self):
        return list(self._index)

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values and self.keys() == other.keys()
        return NotImplemented

    def __hash__(self):
        return hash(self._values)

    def __repr__(self):
        return f"Row({dict(zip(self._index, self._values))!r})"


def _row_factory(cursor):
    description = cursor.description
    if not description:
        return tuple
    index = {column.name: i for i, column in enumerate(description)}
    return lambda values: Row(values, index)


# -------------------------------------------------------------- translation

def to_postgres_sql(sql):
    """``?`` placeholders -> ``%s``; literal ``%`` -> ``%%``. Quoted text,
    quoted identifiers and comments are left alone except for ``%``."""
    out = []
    i = 0
    n = len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if ch == "%":
            out.append("%%")
        elif quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            out.append(sql[i:end].replace("%", "%%"))
            i = end
            continue
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _adapt_param(value, postgres):
    if isinstance(value, bool):
        return int(value)  # boolean flags are stored as 0/1 integers on both engines
    if not postgres and isinstance(value, datetime):
        # Fixed-width UTC text so stored times compare correctly as strings.
        from datetime import timezone

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value


def _adapt_params(params, postgres):
    if params is None:
        return None
    if isinstance(params, dict):
        return {k: _adapt_param(v, postgres) for k, v in params.items()}
    return tuple(_adapt_param(v, postgres) for v in params)


# ----------------------------------------------------------------- cursors

class Cursor:
    """sqlite3-like cursor: fetch methods return None/[] after DML."""

    def __init__(self, raw, postgres, lastrowid=None):
        self._raw = raw
        self._postgres = postgres
        self.lastrowid = lastrowid

    @property
    def rowcount(self):
        return self._raw.rowcount

    @property
    def description(self):
        return self._raw.description

    def _has_rows(self):
        return not self._postgres or self._raw.description is not None

    def fetchone(self):
        return self._raw.fetchone() if self._has_rows() else None

    def fetchall(self):
        return self._raw.fetchall() if self._has_rows() else []

    def fetchmany(self, size=None):
        if not self._has_rows():
            return []
        return self._raw.fetchmany(size) if size else self._raw.fetchmany()

    def __iter__(self):
        return iter(self.fetchall())

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


def _trace_statement(sql, started, ok):
    """Report a statement to the active trace (no parameters are recorded)."""
    try:
        from app.services import telemetry

        telemetry.db_statement(sql, (time.perf_counter() - started) * 1000.0, ok)
    except Exception:
        pass


class Connection:
    """One connection with sqlite3-compatible methods for both backends."""

    def __init__(self, raw, postgres, release=None):
        self._raw = raw
        self.postgres = postgres
        self._release = release
        self._changes = 0
        self.closed = False

    @property
    def dialect(self):
        return "postgres" if self.postgres else "sqlite"

    def _check(self):
        if self.closed:
            raise RuntimeError("Database connection used after its 'with' block ended.")

    def execute(self, sql, params=None):
        self._check()
        if self.postgres:
            cursor = self._raw.cursor()
            started = time.perf_counter()
            ok = False
            try:
                if params:
                    cursor.execute(to_postgres_sql(sql), _adapt_params(params, True))
                else:
                    cursor.execute(sql)
                ok = True
            finally:
                _trace_statement(sql, started, ok)
            if cursor.rowcount and cursor.rowcount > 0:
                self._changes += cursor.rowcount
            return Cursor(cursor, True)
        cursor = self._raw.execute(sql, _adapt_params(params, False) or ())
        return Cursor(cursor, False, getattr(cursor, "lastrowid", None))

    def executemany(self, sql, seq_of_params):
        self._check()
        rows = [_adapt_params(p, self.postgres) for p in seq_of_params]
        if self.postgres:
            cursor = self._raw.cursor()
            total = 0
            if rows:
                cursor.executemany(to_postgres_sql(sql), rows)
                total = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            self._changes += total
            return Cursor(cursor, True)
        return Cursor(self._raw.executemany(sql, rows), False)

    @property
    def total_changes(self):
        """Rows changed through this connection (like sqlite3's attribute)."""
        return self._changes if self.postgres else self._raw.total_changes

    def commit(self):
        self._check()
        self._raw.commit()

    def rollback(self):
        if not self.closed:
            self._raw.rollback()

    @contextmanager
    def savepoint(self, name="sp"):
        """Nested unit of work: an error rolls back only this block."""
        self.execute(f"SAVEPOINT {name}")
        try:
            yield self
        except BaseException:
            self.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.execute(f"RELEASE SAVEPOINT {name}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self._release:
            self._release(self._raw)
        else:
            self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            if self.postgres:
                self.close()  # back to the pool
        return False

    @property
    def raw(self):
        """The driver connection (backend-specific code only)."""
        return self._raw


# -------------------------------------------------------------------- pools

def _pool(conninfo):
    with _POOLS_GUARD:
        pool = _POOLS.get(conninfo)
        if pool is None:
            from psycopg_pool import ConnectionPool

            from app.core.config import get_settings

            s = get_settings()
            pool = ConnectionPool(
                conninfo,
                min_size=s.db_pool_min_size,
                max_size=s.db_pool_max_size,
                timeout=s.db_pool_timeout_seconds,
                max_lifetime=s.db_pool_max_lifetime_seconds,
                max_idle=s.db_pool_max_idle_seconds,
                check=ConnectionPool.check_connection,  # drop dead connections (failover, idle timeouts)
                kwargs={"row_factory": _row_factory, "autocommit": False},
                name="insightiq",
                open=False,
            )
            pool.open(wait=False)
            _POOLS[conninfo] = pool
        return pool


def close_pools():
    """Close every pool (tests, shutdown, or after a settings change)."""
    with _POOLS_GUARD:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:
            pass


import atexit as _atexit

_atexit.register(close_pools)  # scripts and workers exit without pool-thread warnings


def connect():
    """A connection for the configured backend; use as ``with connect() as conn``."""
    if not is_postgres():
        from app.database.sqlite_manager import sqlite_connect

        return Connection(sqlite_connect(), False)
    import psycopg
    from psycopg_pool import PoolTimeout

    pool = _pool(postgres_conninfo())
    try:
        raw = pool.getconn()
    except (PoolTimeout, psycopg.OperationalError) as exc:
        logger.error("database_unavailable %s", type(exc).__name__)
        raise DatabaseUnavailable("The database is unavailable.") from exc
    if raw.info.transaction_status != 0:  # leftover state from a previous user
        raw.rollback()
    return Connection(raw, True, release=pool.putconn)


@contextmanager
def transaction():
    """Commit on success, roll back on error, always release the connection."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def direct_connect(dbname=None, schema=None, autocommit=False):
    """An unpooled PostgreSQL connection (setup, migrations, tests)."""
    import psycopg

    raw = psycopg.connect(postgres_conninfo(dbname=dbname, schema=schema),
                          row_factory=_row_factory, autocommit=autocommit)
    return Connection(raw, True)


# ----------------------------------------------------------- dialect helpers

def json_text(conn, column, key):
    """SQL expression for the text value of top-level JSON ``key`` in ``column``
    (NULL when the column is not valid JSON)."""
    if not key.replace("_", "").isalnum():
        raise ValueError("JSON key must be alphanumeric.")
    if conn.postgres:
        if conn.raw.info.server_version >= 160000:
            valid = f"{column} IS JSON OBJECT"
        else:  # PostgreSQL < 16: InsightIQ writes these columns as JSON objects
            valid = f"left(ltrim({column}), 1) = '{{'"
        return f"(CASE WHEN {valid} THEN ({column})::jsonb ->> '{key}' END)"
    return f"(CASE WHEN json_valid({column}) THEN json_extract({column}, '$.{key}') END)"


def table_exists(conn, name):
    if conn.postgres:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?", (name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,),
        ).fetchone()
    return row is not None


def table_names(conn):
    if conn.postgres:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r[0] for r in rows]


def table_columns(conn, name):
    if conn.postgres:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
            (name,),
        ).fetchall()
        return [r[0] for r in rows]
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")').fetchall()]


def ping():
    """True when the configured database answers a trivial query."""
    with connect() as conn:
        return conn.execute("SELECT 1").fetchone()[0] == 1
