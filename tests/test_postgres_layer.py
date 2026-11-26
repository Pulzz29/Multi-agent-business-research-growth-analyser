"""
Database access layer (app/database/db.py) and schema migrations.

PostgreSQL tests run against the test database only (scratch schemas that
are dropped afterwards); they are skipped when it is not reachable.
"""
import os
import uuid

import pytest

from app.database import db

postgres_only = pytest.mark.skipif(os.environ.get("DB_BACKEND") != "postgres",
                                   reason="PostgreSQL test database not available")


@pytest.fixture
def scratch_schema():
    name = "scratch_" + uuid.uuid4().hex[:10]
    admin = db.direct_connect(schema="public", autocommit=True)
    admin.execute(f'CREATE SCHEMA "{name}"')
    admin.close()
    try:
        yield name
    finally:
        admin = db.direct_connect(schema="public", autocommit=True)
        admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        admin.close()


# --------------------------------------------------------------- pure units

@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = %s AND b = %s"),
    ("SELECT '?' AS q, x FROM t WHERE y = ?", "SELECT '?' AS q, x FROM t WHERE y = %s"),
    ("SELECT x FROM t WHERE y LIKE 'a%' AND z = ?", "SELECT x FROM t WHERE y LIKE 'a%%' AND z = %s"),
    ('SELECT "odd?name" FROM t WHERE a = ?', 'SELECT "odd?name" FROM t WHERE a = %s'),
    ("SELECT 1 -- what? 5%\nWHERE a = ?", "SELECT 1 -- what? 5%%\nWHERE a = %s"),
])
def test_placeholder_translation(sql, expected):
    assert db.to_postgres_sql(sql) == expected


def test_row_supports_key_index_dict_and_unpacking():
    row = db.Row((1, "AAPL"), {"id": 0, "ticker": 1})
    assert row["ticker"] == "AAPL" and row[0] == 1 and dict(row) == {"id": 1, "ticker": "AAPL"}
    first, second = row
    assert (first, second) == (1, "AAPL") and row.keys() == ["id", "ticker"] and len(row) == 2


def test_settings_require_connection_values(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "")
    with pytest.raises(ValidationError) as info:
        Settings()
    assert "DB_HOST" in str(info.value)
    monkeypatch.setenv("DB_SSLMODE", "sometimes")
    with pytest.raises(ValidationError):
        Settings()


def test_conninfo_is_configurable_and_secret_free(monkeypatch):
    from psycopg.conninfo import conninfo_to_dict

    from app.core.config import get_settings

    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:s3cret@db.example.internal:6543/insight?sslmode=require")
    monkeypatch.setenv("DB_SCHEMA", "tenant_a")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "5000")
    get_settings.cache_clear()
    try:
        info = conninfo_to_dict(db.postgres_conninfo())
        assert (info["host"], info["port"], info["dbname"], info["sslmode"]) == (
            "db.example.internal", "6543", "insight", "require")
        assert "search_path=tenant_a" in info["options"] and "statement_timeout=5000" in info["options"]
        described = db.describe_target()
        assert described["host"] == "db.example.internal" and "s3cret" not in repr(described)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ---------------------------------------------------------- live PostgreSQL

@postgres_only
def test_connectivity_and_utc_session():
    conn = db.direct_connect()
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert conn.execute("SHOW timezone").fetchone()[0] == "UTC"
        assert int(conn.raw.info.server_version) >= 120000
    finally:
        conn.close()


@postgres_only
def test_migrations_create_the_full_schema_once(scratch_schema):
    from app.database.migrations import migration_files, run_migrations

    conn = db.direct_connect(schema=scratch_schema)
    try:
        applied = run_migrations(conn)
        assert applied == [p.stem for p in migration_files()]
        assert run_migrations(conn) == []  # idempotent
        tables = set(db.table_names(conn))
        expected = {
            "companies", "sec_facts", "news_articles", "source_refreshes", "research_jobs",
            "research_reports", "forecast_cache", "ai_runs", "ai_trace_events", "observability_alerts",
            "schema_migrations", "market_companies", "market_quotes", "market_statements",
            "provider_usage", "research_execution_migrations", "research_execution_runs",
            "observability_schema_migrations", "research_observability_events",
            "reliability_monitor_evaluations", "sec_company_validation", "company_data_cache",
            "db_schema_migrations",
        }
        assert expected <= tables
        uniques = conn.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace = current_schema()::regnamespace "
            "AND contype IN ('u', 'f', 'c', 'p')").fetchall()
        assert len(uniques) >= 40  # keys, unique, foreign-key and CHECK constraints
        types = dict(conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'company_data_cache'").fetchall())
        assert types["expires_at"] == "timestamp with time zone"
        # Text sorts byte-wise, as in SQLite, whatever the server locale.
        collations = {r[0] for r in conn.execute(
            "SELECT DISTINCT collation_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND data_type = 'text'").fetchall()}
        assert collations == {"C"}
        assert [r[0] for r in conn.execute(
            "SELECT v FROM (VALUES ('b'), ('B'), ('a_x'), ('aX')) AS t(v) "
            "ORDER BY v COLLATE \"C\"").fetchall()] == ["B", "aX", "a_x", "b"]
    finally:
        conn.close()


@postgres_only
def test_concurrent_migration_runs_apply_each_version_once(scratch_schema):
    import threading

    from app.database.migrations import run_migrations

    results, errors = [], []

    def run():
        conn = db.direct_connect(schema=scratch_schema)
        try:
            results.append(run_migrations(conn))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    from app.database.migrations import migration_files

    # Each migration applied by exactly one runner.
    assert sum(len(r) for r in results) == len(migration_files())


@postgres_only
def test_constraints_and_transactions(scratch_schema):
    from app.database.migrations import run_migrations

    conn = db.direct_connect(schema=scratch_schema)
    try:
        run_migrations(conn)
        conn.execute("INSERT INTO companies VALUES (?, ?, ?, ?)", ("AAA", "A", "1", "2026-01-01T00:00:00+00:00"))
        conn.commit()
        with pytest.raises(db.IntegrityError):  # duplicate primary key
            conn.execute("INSERT INTO companies VALUES (?, ?, ?, ?)", ("AAA", "A", "1", "x"))
        conn.rollback()
        with pytest.raises(db.IntegrityError):  # foreign key
            conn.execute("INSERT INTO market_companies(symbol, market, name, created_at, updated_at) "
                         "VALUES ('ZZZ', 'US', 'Z', 'x', 'x')")
        conn.rollback()
        with pytest.raises(db.IntegrityError):  # CHECK
            conn.execute("INSERT INTO market_companies(symbol, market, name, created_at, updated_at) "
                         "VALUES ('AAA', 'XX', 'A', 'x', 'x')")
        conn.rollback()
        # ON CONFLICT DO NOTHING is idempotent; RETURNING gives identity ids.
        conn.execute("INSERT INTO companies VALUES ('AAA', 'A', '1', 'x') ON CONFLICT DO NOTHING")
        first = conn.execute("INSERT INTO source_refreshes(company_ticker, source, started_at, status) "
                             "VALUES ('AAA', 'SEC', 'x', 'running') RETURNING id").fetchone()[0]
        second = conn.execute("INSERT INTO source_refreshes(company_ticker, source, started_at, status) "
                              "VALUES ('AAA', 'SEC', 'x', 'running') RETURNING id").fetchone()[0]
        assert second == first + 1
        # Savepoints keep the outer transaction usable after an error.
        with pytest.raises(db.IntegrityError):
            with conn.savepoint():
                conn.execute("INSERT INTO companies VALUES ('AAA', 'A', '1', 'x')")
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1
        conn.commit()
    finally:
        conn.close()


@postgres_only
def test_pool_returns_connections_and_rolls_back_failures(scratch_schema, monkeypatch):
    from app.core.config import get_settings
    from app.database.migrations import run_migrations

    monkeypatch.setenv("DB_SCHEMA", scratch_schema)
    monkeypatch.setenv("DB_POOL_MAX_SIZE", "2")
    get_settings.cache_clear()
    db.close_pools()
    try:
        run_migrations()
        for _ in range(10):  # more uses than pool slots: connections are returned
            with db.connect() as conn:
                conn.execute("SELECT 1")
        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO companies VALUES ('BBB', 'B', '1', 'x')")
                raise RuntimeError("boom")
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM companies WHERE ticker='BBB'").fetchone()[0] == 0
        with pytest.raises(RuntimeError, match="after its 'with' block"):
            conn.execute("SELECT 1")
    finally:
        db.close_pools()
        monkeypatch.undo()
        get_settings.cache_clear()


def test_remote_database_requires_trusted_tls(monkeypatch, tmp_path):
    from pydantic import ValidationError

    from app.core.config import Settings

    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "insightiq-db.example.internal")
    monkeypatch.setenv("DB_SSLMODE", "prefer")
    with pytest.raises(ValidationError, match="remote database needs TLS"):
        Settings()
    monkeypatch.setenv("DB_SSLMODE", "verify-full")
    monkeypatch.setenv("DB_SSLROOTCERT", str(tmp_path / "missing-ca.pem"))
    with pytest.raises(ValidationError, match="CA certificate"):
        Settings()
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("DB_SSLROOTCERT", str(ca))
    assert Settings().db_sslmode == "verify-full"
    monkeypatch.setenv("DB_SSLROOTCERT", "system")  # libpq: operating-system trust store
    assert Settings().db_sslrootcert == "system"
    # A local server may keep sslmode=prefer.
    monkeypatch.setenv("DB_HOST", "localhost")
    monkeypatch.setenv("DB_SSLMODE", "prefer")
    monkeypatch.delenv("DB_SSLROOTCERT")
    assert Settings().db_sslmode == "prefer"
    # DATABASE_URL is checked the same way.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.example.internal/app?sslmode=disable")
    with pytest.raises(ValidationError, match="remote database needs TLS"):
        Settings()
