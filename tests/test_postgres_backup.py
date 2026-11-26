"""
PostgreSQL backup, restore verification and retention (test database only).

The backup runs pg_dump against the test database, restores the dump into a
new isolated database, compares every table's row count, and drops it.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("DB_BACKEND") != "postgres",
                                reason="PostgreSQL test database not available")


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "pg_backups"))
    return tmp_path / "pg_backups"


def test_backup_is_restored_and_verified_in_an_isolated_database(backup_dir):
    from app.database import db
    from app.services import db_backup

    result = db_backup.backup(verify=True)
    dump = backup_dir / result["file"]
    assert dump.is_file() and dump.stat().st_size == result["size_bytes"] > 0
    assert result["restore_verified"] is True
    verification = result["verification"]
    assert verification["matched"] and not verification["differences"]
    assert verification["tables_compared"] >= 20 and verification["rows_compared"] > 1000
    # The scratch database is gone again.
    admin = db.direct_connect(dbname="postgres", schema="public")
    try:
        assert admin.execute("SELECT 1 FROM pg_database WHERE datname = ?",
                             (verification["scratch_database"],)).fetchone() is None
    finally:
        admin.close()
    status = db_backup.latest_backup_status()
    assert status["status"] == "succeeded" and status["restore_verified"] is True


def test_verification_detects_a_dump_that_does_not_match(backup_dir):
    from app.services import db_backup

    result = db_backup.backup(verify=False)
    dump = backup_dir / result["file"]
    tampered = {"seed_live.companies": 999_999_999}
    outcome = db_backup.verify_restore(dump, tampered)
    assert outcome["matched"] is False and "seed_live.companies" in outcome["differences"]


def test_retention_keeps_newest_and_deletes_only_old_dumps(backup_dir, monkeypatch):
    from app.services import db_backup

    monkeypatch.setenv("BACKUP_RETENTION_COUNT", "3")
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "10")
    backup_dir.mkdir(parents=True)
    now = datetime(2026, 11, 25, tzinfo=timezone.utc)
    names = [f"insightiq_{(now - timedelta(days=d)).strftime('%Y%m%dT%H%M%SZ')}.dump" for d in (0, 1, 2, 3, 20)]
    for name in names:
        (backup_dir / name).write_bytes(b"x")
    (backup_dir / "notes.txt").write_text("keep me")
    (backup_dir / "other.dump").write_bytes(b"keep")
    outcome = db_backup.apply_retention(now=now)
    left = sorted(p.name for p in backup_dir.iterdir())
    assert sorted(outcome["deleted"]) == sorted(names[3:])
    assert left == sorted(names[:3] + ["notes.txt", "other.dump"])


def test_newest_backup_is_never_deleted(backup_dir, monkeypatch):
    from app.services import db_backup

    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")
    backup_dir.mkdir(parents=True)
    old = backup_dir / "insightiq_20200101T000000Z.dump"
    old.write_bytes(b"x")
    db_backup.apply_retention(now=datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert old.is_file()


def test_retention_never_deletes_the_newest_verified_backup(backup_dir, monkeypatch):
    from app.services import db_backup

    result = db_backup.backup(verify=True)  # recorded as restore-verified in backup_runs
    # Two newer, unverified dumps appear afterwards; retention keeps only one.
    for name in ("insightiq_29990101T000000Z.dump", "insightiq_29990102T000000Z.dump"):
        (backup_dir / name).write_bytes(b"x")
    monkeypatch.setenv("BACKUP_RETENTION_COUNT", "1")
    outcome = db_backup.apply_retention()
    assert outcome["protected_verified"] == result["file"]
    assert (backup_dir / result["file"]).is_file()                      # only verified backup kept
    assert (backup_dir / "insightiq_29990102T000000Z.dump").is_file()   # newest kept
    assert outcome["deleted"] == ["insightiq_29990101T000000Z.dump"]


def test_retained_dumps_are_re_verified(backup_dir):
    from app.services import db_backup

    result = db_backup.backup(verify=False)
    checks = {c["file"]: c for c in db_backup.verify_files()}
    assert checks[result["file"]]["sha256_matches"] is True and checks[result["file"]]["readable"] is True
    (backup_dir / result["file"]).write_bytes(b"corrupted")
    checks = {c["file"]: c for c in db_backup.verify_files()}
    assert checks[result["file"]]["sha256_matches"] is False and checks[result["file"]]["readable"] is False


def test_sqlite_export_is_consistent_and_verified(tmp_path):
    import sqlite3

    from app.database import db
    from app.services import db_backup

    target = tmp_path / "insightiq_export_20261126T000000Z.sqlite3"
    export = db_backup.export_sqlite(target)
    assert export["integrity"] == "ok" and export["foreign_key_violations"] == 0
    assert export["counts_match"] and export["checksums_match"] and export["checksum_tables"] == 21
    con = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
    pg = db.direct_connect()
    try:
        for table in ("sec_facts", "news_articles", "research_execution_runs", "stored_documents"):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == \
                pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], table
        # Schema of the original tables is the original SQLite schema (rollback-compatible).
        ddl = con.execute("SELECT sql FROM sqlite_master WHERE name = 'sec_facts'").fetchone()[0]
        assert "AUTOINCREMENT" in ddl
    finally:
        con.close()
        pg.close()
    with pytest.raises(db_backup.BackupError):
        db_backup.export_sqlite(target)  # never overwrites
