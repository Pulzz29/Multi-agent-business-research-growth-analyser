import os
import json
import shutil
import sqlite3
from app.core.portability import closing_connection
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timezone

from app.core.config import get_settings


_DB_LOCK = threading.RLock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def project_root():
    return get_settings().project_root.resolve()


def drive_database_path():
    return project_root() / "data" / "insightiq.sqlite3"


def working_database_path():
    root = (
        Path(tempfile.gettempdir())
        / "insightiq_sqlite"
        / project_root().name
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "insightiq.sqlite3"


def verify_sqlite_integrity(path):
    path = Path(path)

    if not path.is_file():
        return False

    try:
        with closing_connection(str(path), timeout=15) as conn:
            result = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            return bool(result and result[0] == "ok")
    except sqlite3.Error:
        return False


def restore_database_from_drive():
    source = drive_database_path()
    target = working_database_path()

    with _DB_LOCK:
        if target.exists():
            if not verify_sqlite_integrity(target):
                raise RuntimeError(
                    "Local database exists but failed integrity verification."
                )
            return {"restored": False, "reason": "local_exists"}

        if not source.exists():
            return {"restored": False, "reason": "no_drive_snapshot"}

        if not verify_sqlite_integrity(source):
            raise RuntimeError(
                "Drive database failed integrity verification."
            )

        temporary = target.with_suffix(".restore.tmp")

        try:
            shutil.copy2(source, temporary)

            if not verify_sqlite_integrity(temporary):
                raise RuntimeError(
                    "Restored database failed integrity verification."
                )

            os.replace(temporary, target)

        finally:
            temporary.unlink(missing_ok=True)

        return {
            "restored": True,
            "database_path": str(target),
        }


def sqlite_connect():
    """Raw connection to the SQLite working database (DB_BACKEND=sqlite)."""
    path = working_database_path()

    conn = sqlite3.connect(
        str(path),
        timeout=30,
        isolation_level="DEFERRED",
    )

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")

    return conn


def connect():
    """Connection to the configured database (PostgreSQL or SQLite).

    Kept here so existing ``from app.database.sqlite_manager import connect``
    imports keep working; see app.database.db.
    """
    from app.database import db

    return db.connect()


def transaction():
    from app.database import db

    return db.transaction()


def file_database_enabled():
    """True when the SQLite file is the live database (DB_BACKEND=sqlite).

    On PostgreSQL the file snapshots, verified file backups and file restores
    below do not apply: backups are taken with pg_dump (see
    docs/POSTGRES_MIGRATION.md).
    """
    from app.database import db

    return not db.is_postgres()


MIGRATIONS = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS companies (
            ticker TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            cik TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sec_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_ticker TEXT NOT NULL,
            source_key TEXT NOT NULL UNIQUE,
            taxonomy TEXT NOT NULL,
            concept TEXT NOT NULL,
            unit TEXT NOT NULL,
            value_text TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT NOT NULL,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            form TEXT,
            accession TEXT,
            filed_at TEXT,
            sec_frame TEXT,
            source_url TEXT,
            retrieved_at TEXT NOT NULL,
            FOREIGN KEY(company_ticker)
                REFERENCES companies(ticker)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_sec_facts_company_period
        ON sec_facts(company_ticker, concept, period_end)
        """,
        """
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_ticker TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_article_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            article_url TEXT,
            source_name TEXT,
            published_at TEXT,
            retrieved_at TEXT NOT NULL,
            relevance_score REAL,
            relevance_reason TEXT,
            raw_metadata_json TEXT,
            UNIQUE(provider, provider_article_id, company_ticker),
            FOREIGN KEY(company_ticker)
                REFERENCES companies(ticker)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_news_company_date
        ON news_articles(company_ticker, published_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS source_refreshes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            records_fetched INTEGER NOT NULL DEFAULT 0,
            records_inserted INTEGER NOT NULL DEFAULT 0,
            error_category TEXT,
            error_message TEXT,
            FOREIGN KEY(company_ticker)
                REFERENCES companies(ticker)
        )
        """,
    ],
    2: [
        """
        CREATE TABLE IF NOT EXISTS research_jobs (
            job_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL CHECK(
                status IN (
                    'queued', 'running', 'completed',
                    'partial_failure', 'failed'
                )
            ),
            request_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            error_category TEXT,
            error_message TEXT,
            FOREIGN KEY(ticker)
                REFERENCES companies(ticker)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS research_reports (
            report_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id)
                REFERENCES research_jobs(job_id),
            FOREIGN KEY(ticker)
                REFERENCES companies(ticker)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS forecast_cache (
            cache_key TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            model_config_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(ticker)
                REFERENCES companies(ticker)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_jobs_ticker_created
        ON research_jobs(ticker, created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_reports_ticker_created
        ON research_reports(ticker, created_at)
        """,
    ],
    3: [
        """
        CREATE TABLE IF NOT EXISTS ai_runs (
            run_id TEXT PRIMARY KEY,
            job_id TEXT,
            ticker TEXT,
            trace_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms REAL,
            error_category TEXT,
            FOREIGN KEY(job_id)
                REFERENCES research_jobs(job_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ai_trace_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            trace_id TEXT NOT NULL,
            span_id TEXT,
            parent_span_id TEXT,
            stage TEXT NOT NULL,
            event_name TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_ms REAL,
            provider TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            retry_count INTEGER,
            error_category TEXT,
            metadata_json TEXT,
            FOREIGN KEY(run_id)
                REFERENCES ai_runs(run_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_trace_run
        ON ai_trace_events(run_id, started_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS ix_trace_stage
        ON ai_trace_events(stage, started_at)
        """,
        """
        CREATE TABLE IF NOT EXISTS observability_alerts (
            alert_id TEXT PRIMARY KEY,
            run_id TEXT,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            triggered_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY(run_id)
                REFERENCES ai_runs(run_id)
        )
        """,
    ],
}


def initialize_database():
    if not file_database_enabled():
        from app.database.migrations import run_migrations

        return {"backend": "postgres", "applied_migrations": run_migrations()}

    with _DB_LOCK:
        restore_result = restore_database_from_drive()

        with transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )

            applied = {
                int(row["version"])
                for row in conn.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }

            applied_now = []

            for version in sorted(MIGRATIONS):
                if version in applied:
                    continue

                for sql in MIGRATIONS[version]:
                    conn.execute(sql)

                conn.execute(
                    """
                    INSERT INTO schema_migrations(
                        version, applied_at
                    ) VALUES (?, ?)
                    """,
                    (version, utc_now()),
                )
                applied_now.append(version)

        if not verify_sqlite_integrity(working_database_path()):
            raise RuntimeError(
                "Database failed integrity verification after migration."
            )

        return {
            "database_path": str(working_database_path()),
            "drive_path": str(drive_database_path()),
            "applied_migrations": applied_now,
            "restore": restore_result,
            "integrity_ok": True,
        }


def sync_snapshot_to_drive(create_timestamped_backup=False):
    if not file_database_enabled():
        # PostgreSQL commits are durable on the server; there is no file
        # snapshot to refresh (backups: pg_dump).
        return {"synced": False, "integrity_ok": True, "reason": "postgres_backend"}

    with _DB_LOCK:
        source = working_database_path()
        destination = drive_database_path()

        if not verify_sqlite_integrity(source):
            raise RuntimeError(
                "Working SQLite database failed integrity verification."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)

        temporary = destination.with_name(
            destination.name + ".snapshot.tmp"
        )

        backup_path = None

        try:
            with closing_connection(str(source)) as source_conn:
                with closing_connection(str(temporary)) as target_conn:
                    source_conn.backup(target_conn)

            if not verify_sqlite_integrity(temporary):
                raise RuntimeError(
                    "Generated SQLite snapshot failed integrity verification."
                )

            if create_timestamped_backup and destination.exists():
                backup_directory = destination.parent / "backups"
                backup_directory.mkdir(parents=True, exist_ok=True)

                timestamp = datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%S%fZ"
                )

                backup_path = (
                    backup_directory / f"insightiq_{timestamp}.sqlite3"
                )

                shutil.copy2(destination, backup_path)

                if not verify_sqlite_integrity(backup_path):
                    backup_path.unlink(missing_ok=True)
                    raise RuntimeError(
                        "Previous Drive database backup failed verification."
                    )

            os.replace(temporary, destination)

            if not verify_sqlite_integrity(destination):
                raise RuntimeError(
                    "Final Drive snapshot failed integrity verification."
                )

        finally:
            temporary.unlink(missing_ok=True)

        return {
            "synced": True,
            "integrity_ok": True,
            "drive_path": str(destination),
            "previous_backup_path": (
                str(backup_path) if backup_path else None
            ),
        }


def database_summary():
    from app.database import db

    with connect() as conn:
        migrations = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]

        tables = db.table_names(conn)

    if not file_database_enabled():
        return {"database": db.describe_target(), "migrations": migrations, "tables": tables,
                "integrity_ok": True}
    return {
        "database_path": str(working_database_path()),
        "migrations": migrations,
        "tables": tables,
        "integrity_ok": verify_sqlite_integrity(
            working_database_path()
        ),
    }


# ----------------------------------------------------------
# BUG B1 fix (local migration): ingestion recovery points
# ----------------------------------------------------------
# sync_snapshot_to_drive() refreshes data/insightiq.sqlite3, but
# controlled server startup restores ONLY from the newest verified
# manifest in data/sqlite_backups. Without a recovery point there,
# news/SEC ingested after the last research backup could be lost
# when the runtime working copy is rebuilt. This reuses the existing
# verified-backup mechanism (sqlite_recovery.create_verified_backup).

def create_recovery_point(reason):
    """Create a SHA-256 verified backup + manifest in data/sqlite_backups.

    Never raises: ingestion has already been committed and snapshotted,
    so a backup failure must not turn a successful refresh into a failed
    one (same policy as completed research-job backups).
    """
    import logging

    logger = logging.getLogger(__name__)

    if not file_database_enabled():
        # PostgreSQL: committed data is already durable on the server.
        return {"verified": True, "reason": reason, "backend": "postgres", "backup_file": None}

    try:
        from app.services.sqlite_recovery import create_verified_backup

        backup_path, manifest_path = create_verified_backup(project_root())

        return {
            "verified": True,
            "reason": reason,
            "backup_file": backup_path.name,
            "manifest_file": manifest_path.name,
        }

    except Exception as exc:
        logger.warning(
            "Recovery point creation failed after %s: %s",
            reason,
            type(exc).__name__,
        )

        return {
            "verified": False,
            "reason": reason,
            "error_type": type(exc).__name__,
        }
