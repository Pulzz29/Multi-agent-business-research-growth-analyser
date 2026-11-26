"""
SQLite -> PostgreSQL data migration (app/database/sqlite_to_postgres.py).

Copies the stored snapshot (read-only) into a scratch schema of the test
database: counts and per-row checksums match, re-running adds nothing,
identity sequences continue after the copied ids, and the source file is
not modified.
"""
import hashlib
import os
import uuid
from pathlib import Path

import pytest

from app.database import db

pytestmark = pytest.mark.skipif(os.environ.get("DB_BACKEND") != "postgres",
                                reason="PostgreSQL test database not available")

SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "insightiq.sqlite3"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def migrated():
    from app.database.migrations import run_migrations
    from app.database.sqlite_to_postgres import copy_database

    schema = "migtest_" + uuid.uuid4().hex[:10]
    admin = db.direct_connect(schema="public", autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    admin.close()
    before = sha256(SNAPSHOT)
    conn = db.direct_connect(schema=schema)
    try:
        run_migrations(conn)
        first = copy_database(SNAPSHOT, conn)
        second = copy_database(SNAPSHOT, conn)
        yield {"conn": conn, "first": first, "second": second, "source_hash_before": before}
    finally:
        conn.close()
        admin = db.direct_connect(schema="public", autocommit=True)
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def test_every_row_is_copied_and_checksums_match(migrated):
    from app.database.sqlite_to_postgres import validate

    report = validate(SNAPSHOT, migrated["conn"])
    assert report and all(r["match"] for r in report.values()), {
        t: r for t, r in report.items() if not r["match"]}
    assert report["sec_facts"]["postgres_rows"] > 1000


def test_rerun_inserts_no_duplicates(migrated):
    assert all(r["inserted"] == 0 for r in migrated["second"].values())
    assert sum(r["inserted"] for r in migrated["first"].values()) > 1000


def test_identity_sequences_continue_after_copied_ids(migrated):
    conn = migrated["conn"]
    top = conn.execute("SELECT MAX(id) FROM sec_facts").fetchone()[0]
    conn.execute("INSERT INTO companies VALUES ('ZZTEST', 'Z', '1', 'x') ON CONFLICT DO NOTHING")
    new_id = conn.execute(
        "INSERT INTO source_refreshes(company_ticker, source, started_at, status) "
        "VALUES ('ZZTEST', 'SEC', 'x', 'running') RETURNING id").fetchone()[0]
    old_top = conn.execute("SELECT MAX(id) FROM source_refreshes WHERE company_ticker <> 'ZZTEST'").fetchone()[0]
    assert new_id > (old_top or 0) and top
    conn.rollback()


def test_source_database_is_not_modified(migrated):
    assert sha256(SNAPSHOT) == migrated["source_hash_before"]


def test_relationships_survive(migrated):
    conn = migrated["conn"]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM sec_facts f LEFT JOIN companies c ON c.ticker = f.company_ticker "
        "WHERE c.ticker IS NULL").fetchone()[0]
    assert orphans == 0
