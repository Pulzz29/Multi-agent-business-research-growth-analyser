import hashlib
import json
import os
import sqlite3
from app.core.portability import closing_connection
import uuid
from datetime import datetime, timezone
from pathlib import Path


class SQLiteRecoveryError(RuntimeError):
    pass


REQUIRED_TABLES = {
    "sec_facts",
    "news_articles",
    "research_execution_runs",
    "schema_migrations",
}


def utc_timestamp():
    return datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%S%fZ")


def file_sha256(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def database_tables(conn):
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
    }


def verify_database(conn):
    integrity = conn.execute(
        "PRAGMA integrity_check"
    ).fetchone()

    if integrity is None or integrity[0] != "ok":
        raise SQLiteRecoveryError(
            "SQLite integrity verification failed."
        )

    tables = database_tables(conn)

    if not REQUIRED_TABLES.issubset(tables):
        raise SQLiteRecoveryError(
            "SQLite backup lacks required tables."
        )

    return {
        "sec_facts": conn.execute(
            "SELECT COUNT(*) FROM sec_facts"
        ).fetchone()[0],
        "news_articles": conn.execute(
            "SELECT COUNT(*) FROM news_articles"
        ).fetchone()[0],
        "research_execution_runs": conn.execute(
            "SELECT COUNT(*) FROM research_execution_runs"
        ).fetchone()[0],
        "completed_research_runs": conn.execute(
            """
            SELECT COUNT(*)
            FROM research_execution_runs
            WHERE status = 'completed'
            """
        ).fetchone()[0],
    }


def discover_active_database():
    # Reuse the project-managed connection rather
    # than guessing a temporary SQLite filename.
    from app.database.sqlite_manager import connect

    with connect() as conn:
        databases = conn.execute(
            "PRAGMA database_list"
        ).fetchall()

        file_names = [
            row[2]
            for row in databases
            if row[1] == "main"
        ]

        if len(file_names) != 1 or not file_names[0]:
            raise SQLiteRecoveryError(
                "Active SQLite database path unavailable."
            )

        source_path = Path(file_names[0]).resolve()

        if not source_path.is_file():
            raise SQLiteRecoveryError(
                "Active SQLite database file unavailable."
            )

        verify_database(conn)

    return source_path


def create_verified_backup(root):
    root = Path(root).resolve()

    backup_directory = (
        root / "data" / "sqlite_backups"
    )

    backup_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_path = discover_active_database()

    identifier = (
        utc_timestamp()
        + "_"
        + uuid.uuid4().hex[:10]
    )

    backup_path = (
        backup_directory
        / f"insightiq_{identifier}.sqlite3"
    )

    temporary_path = backup_path.with_name(
        backup_path.name + ".tmp"
    )

    manifest_path = backup_path.with_suffix(
        ".manifest.json"
    )

    try:
        with closing_connection(
            str(source_path),
            timeout=30.0,
        ) as source:
            source.execute("PRAGMA busy_timeout=30000")

            with closing_connection(
                str(temporary_path),
                timeout=30.0,
            ) as destination:
                # SQLite's backup mechanism incorporates
                # committed WAL changes consistently.
                source.backup(destination)

        with closing_connection(
            str(temporary_path),
            timeout=30.0,
        ) as verification:
            counts = verify_database(verification)

        backup_hash = file_sha256(
            temporary_path
        )

        os.replace(
            temporary_path,
            backup_path,
        )

        manifest = {
            "backup_format_version": 1,
            "created_at_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "backup_file": backup_path.name,
            "sha256": backup_hash,
            "byte_size": backup_path.stat().st_size,
            "table_counts": counts,
            "integrity_check": "ok",
            "source_database_path": str(source_path),
            "restore_tested": False,
        }

        manifest_temp = manifest_path.with_name(
            manifest_path.name + ".tmp"
        )

        manifest_temp.write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )

        os.replace(
            manifest_temp,
            manifest_path,
        )

    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return backup_path, manifest_path


def restore_to_isolated_path(
    backup_path,
    manifest_path,
    destination_path,
):
    backup_path = Path(backup_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    destination_path = Path(destination_path).resolve()

    if not backup_path.is_file():
        raise SQLiteRecoveryError(
            "Backup database does not exist."
        )

    if not manifest_path.is_file():
        raise SQLiteRecoveryError(
            "Backup manifest does not exist."
        )

    manifest = json.loads(
        manifest_path.read_text(
            encoding="utf-8"
        )
    )

    if manifest.get("backup_format_version") != 1:
        raise SQLiteRecoveryError(
            "Unsupported backup manifest version."
        )

    if manifest.get("backup_file") != backup_path.name:
        raise SQLiteRecoveryError(
            "Backup filename does not match manifest."
        )

    if (
        manifest.get("sha256")
        != file_sha256(backup_path)
    ):
        raise SQLiteRecoveryError(
            "Backup SHA-256 checksum mismatch."
        )

    active_path = discover_active_database()

    if destination_path == active_path:
        raise SQLiteRecoveryError(
            "Refusing to overwrite the active database."
        )

    if destination_path.exists():
        raise SQLiteRecoveryError(
            "Restore destination already exists."
        )

    destination_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Backup API is used for restoration as well;
    # the source and destination are distinct files.
    with closing_connection(
        str(backup_path),
        timeout=30.0,
    ) as backup:
        with closing_connection(
            str(destination_path),
            timeout=30.0,
        ) as restored:
            backup.backup(restored)

    with closing_connection(
        str(destination_path),
        timeout=30.0,
    ) as verification:
        counts = verify_database(verification)

    if counts != manifest["table_counts"]:
        raise SQLiteRecoveryError(
            "Restored database counts differ from manifest."
        )

    return counts
