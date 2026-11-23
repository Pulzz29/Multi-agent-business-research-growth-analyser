import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.core.portability import closing_connection, exclusive_file_lock

from app.services.sqlite_recovery import (
    file_sha256,
    verify_database,
)


class StartupRecoveryError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def exclusive_startup_lock(lock_path):
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a+b") as handle:
        with exclusive_file_lock(handle):
            yield


def verified_backup(backup_path, manifest_path):
    backup_path = Path(backup_path).resolve()
    manifest_path = Path(manifest_path).resolve()

    if not backup_path.is_file():
        raise StartupRecoveryError(
            "Recovery backup is unavailable."
        )

    if not manifest_path.is_file():
        raise StartupRecoveryError(
            "Recovery manifest is unavailable."
        )

    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        raise StartupRecoveryError(
            "Recovery manifest is invalid."
        )

    if manifest.get("backup_format_version") != 1:
        raise StartupRecoveryError(
            "Unsupported recovery backup version."
        )

    if manifest.get("backup_file") != backup_path.name:
        raise StartupRecoveryError(
            "Recovery backup filename mismatch."
        )

    if manifest.get("byte_size") != backup_path.stat().st_size:
        raise StartupRecoveryError(
            "Recovery backup size mismatch."
        )

    if manifest.get("sha256") != file_sha256(backup_path):
        raise StartupRecoveryError(
            "Recovery backup checksum mismatch."
        )

    try:
        # Read-only connection prevents accidental writes
        # to the persistent Drive backup.
        connection = sqlite3.connect(
            backup_path.as_uri() + "?mode=ro",
            uri=True,
        )

        try:
            counts = verify_database(connection)
        finally:
            connection.close()

    except sqlite3.Error as exc:
        raise StartupRecoveryError(
            "Recovery backup failed SQLite validation."
        ) from exc

    if counts != manifest.get("table_counts"):
        raise StartupRecoveryError(
            "Recovery backup counts do not match manifest."
        )

    return manifest


def reconcile_interrupted_jobs(database_path):
    """
    Run ONLY while starting a controlled, stopped
    single-worker service.

    Do not invoke against a database with active
    research workers.
    """
    with closing_connection(
        str(database_path),
        timeout=30.0,
    ) as conn:
        conn.row_factory = sqlite3.Row

        conn.execute("BEGIN IMMEDIATE")

        rows = conn.execute(
            """
            SELECT run_id, status, agent_statuses_json
            FROM research_execution_runs
            WHERE status IN ('queued', 'running')
            """
        ).fetchall()

        changed = []

        for row in rows:
            statuses = json.loads(
                row["agent_statuses_json"]
            )

            if set(statuses) != {
                "financial", "news", "strategic"
            }:
                raise StartupRecoveryError(
                    "Invalid stored research agent statuses."
                )

            # Only agents that actually reported running
            # are marked failed; pending agents remain
            # pending because they never executed.
            reconciled = {
                name: (
                    "failed"
                    if status == "running"
                    else status
                )
                for name, status in statuses.items()
            }

            now = utc_now()

            updated = conn.execute(
                """
                UPDATE research_execution_runs
                SET status = 'failed',
                    updated_at_utc = ?,
                    finished_at_utc = ?,
                    agent_statuses_json = ?,
                    error_type = 'InterruptedExecution',
                    error_code = 'startup_interrupted'
                WHERE run_id = ?
                  AND status = ?
                """,
                (
                    now,
                    now,
                    json.dumps(
                        reconciled,
                        sort_keys=True,
                    ),
                    row["run_id"],
                    row["status"],
                ),
            )

            if updated.rowcount != 1:
                raise StartupRecoveryError(
                    "Concurrent research status change."
                )

            changed.append({
                "run_id": row["run_id"],
                "previous_status": row["status"],
                "recovered_status": "failed",
                "agent_statuses": reconciled,
            })

        conn.commit()

    return changed


def prepare_runtime_database(
    database_path,
    backup_path,
    manifest_path,
    reconcile_jobs=True,
):
    """
    Controlled startup entrypoint.

    Must run BEFORE creating workers or opening
    long-lived SQLite connections.

    Never overwrites an existing database file.
    """
    database_path = Path(database_path).resolve()
    backup_path = Path(backup_path).resolve()

    if database_path == backup_path:
        raise StartupRecoveryError(
            "Runtime and backup paths must differ."
        )

    database_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    lock_path = database_path.with_suffix(
        ".startup.lock"
    )

    restored = False

    with exclusive_startup_lock(lock_path):
        if not database_path.exists():
            manifest = verified_backup(
                backup_path,
                manifest_path,
            )

            temporary = database_path.with_name(
                database_path.name
                + ".restore-"
                + uuid.uuid4().hex
            )

            try:
                with closing_connection(
                    backup_path.as_uri() + "?mode=ro",
                    uri=True,
                ) as source:
                    with closing_connection(
                        str(temporary)
                    ) as destination:
                        source.backup(destination)

                with closing_connection(
                    str(temporary)
                ) as check:
                    restored_counts = verify_database(
                        check
                    )

                if (
                    restored_counts
                    != manifest["table_counts"]
                ):
                    raise StartupRecoveryError(
                        "Restored database verification failed."
                    )

                # The target must still be missing.
                # The lock coordinates cooperating
                # startup processes only.
                if database_path.exists():
                    raise StartupRecoveryError(
                        "Runtime database appeared during restore."
                    )

                os.replace(
                    temporary,
                    database_path,
                )

                restored = True

            finally:
                if temporary.exists():
                    temporary.unlink()

        if not database_path.is_file():
            raise StartupRecoveryError(
                "Runtime database unavailable."
            )

        with closing_connection(
            str(database_path)
        ) as check:
            counts = verify_database(check)

        recovered_jobs = (
            reconcile_interrupted_jobs(database_path)
            if reconcile_jobs
            else []
        )

        with closing_connection(
            str(database_path)
        ) as check:
            final_counts = verify_database(check)

    return {
        "database_path": str(database_path),
        "database_restored": restored,
        "integrity": "ok",
        "initial_counts": counts,
        "final_counts": final_counts,
        "interrupted_jobs_reconciled": recovered_jobs,
    }
