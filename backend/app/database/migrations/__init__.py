"""
Versioned schema migrations for PostgreSQL.

Each ``NNNN_name.sql`` file in this folder runs once, in order, inside one
transaction, and is recorded in ``db_schema_migrations``. A transaction-level
advisory lock makes concurrent startups (several instances) apply each
migration exactly once. On SQLite (rollback backend) the original file keeps
its own schema; ``ensure_sqlite_extras`` adds the cache table there.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("insightiq.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent
_LOCK_ID = 74_211_903  # arbitrary, constant advisory-lock key for migrations


def migration_files():
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _statements(sql):
    """Split a migration file into statements on ``;``, keeping ``$$ ... $$``
    blocks (DO / function bodies) whole. Comment lines are dropped."""
    text = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements, current, in_dollar = [], [], False
    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            in_dollar = not in_dollar
            current.append("$$")
            i += 2
            continue
        ch = text[i]
        if ch == ";" and not in_dollar:
            statements.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    statements.append("".join(current).strip())
    return [s for s in statements if s]


def run_migrations(conn=None):
    """Apply pending migrations; returns the versions applied now."""
    from app.database import db

    own = conn is None
    if own:
        if db.is_postgres():
            import psycopg

            try:
                conn = db.direct_connect()  # unpooled: a bad login fails at once, clearly
            except psycopg.OperationalError as exc:
                reason = str(exc).strip().splitlines()[-1][:200] if str(exc).strip() else type(exc).__name__
                raise db.DatabaseUnavailable(f"PostgreSQL connection failed: {reason}") from exc
        else:
            conn = db.connect()
    if not conn.postgres:
        ensure_sqlite_extras(conn)
        if own:
            conn.commit()
            conn.close()
        return []
    applied_now = []
    try:
        conn.execute("SELECT pg_advisory_xact_lock(?)", (_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS db_schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM db_schema_migrations").fetchall()}
        for path in migration_files():
            version = path.stem
            if version in applied:
                continue
            for statement in _statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO db_schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, datetime.now(timezone.utc)),
            )
            applied_now.append(version)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()
    if applied_now:
        logger.info("db_migrations_applied %s", applied_now)
    return applied_now


SQLITE_CACHE_TABLE = """
CREATE TABLE IF NOT EXISTS company_data_cache (
    cache_kind TEXT NOT NULL CHECK (cache_kind IN ('company', 'search')),
    cache_key TEXT NOT NULL,
    market TEXT NOT NULL,
    symbol TEXT,
    status TEXT NOT NULL CHECK (status IN ('ready', 'pending')),
    payload_json TEXT,
    fetched_at TEXT,
    expires_at TEXT,
    refresh_lease_until TEXT,
    refresh_owner TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cache_kind, cache_key)
)
"""


def ensure_sqlite_extras(conn):
    """Tables added after the SQLite era, created on the SQLite backend too."""
    conn.execute(SQLITE_CACHE_TABLE)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_company_data_cache_expires ON company_data_cache(expires_at)")
