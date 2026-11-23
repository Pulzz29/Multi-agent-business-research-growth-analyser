import json
import sqlite3
import uuid
from pathlib import Path

from app.services.startup_recovery import (
    verified_backup,
    StartupRecoveryError,
)


class BackupFreshnessError(RuntimeError):
    pass


def audit_backup_jobs(
    backup_path,
    manifest_path,
    required_completed_run_ids,
):
    """
    Verify backup integrity and the presence of
    explicitly required completed research jobs.

    Valid SQLite integrity does not prove that
    recent work is included in the snapshot.
    """
    backup_path = Path(backup_path).resolve()
    manifest_path = Path(manifest_path).resolve()

    try:
        verified_backup(
            backup_path,
            manifest_path,
        )
    except StartupRecoveryError as exc:
        raise BackupFreshnessError(
            "Backup integrity verification failed."
        ) from exc

    required = list(required_completed_run_ids)

    for run_id in required:
        try:
            if str(uuid.UUID(run_id)) != run_id:
                raise ValueError("Noncanonical UUID")
        except (TypeError, ValueError, AttributeError):
            raise BackupFreshnessError(
                "Invalid required research run ID."
            )

    if len(required) != len(set(required)):
        raise BackupFreshnessError(
            "Duplicate required research run ID."
        )

    connection = sqlite3.connect(
        backup_path.as_uri() + "?mode=ro",
        uri=True,
    )

    try:
        connection.row_factory = sqlite3.Row

        found = {}
        missing = []
        incomplete = []

        for run_id in required:
            row = connection.execute(
                """
                SELECT run_id, ticker, status
                FROM research_execution_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

            if row is None:
                missing.append(run_id)
                continue

            if row["status"] != "completed":
                incomplete.append(run_id)
                continue

            found[run_id] = {
                "ticker": row["ticker"],
                "status": row["status"],
            }

        manifest = json.loads(
            manifest_path.read_text(
                encoding="utf-8"
            )
        )

        return {
            "backup_file": backup_path.name,
            "backup_sha256": manifest["sha256"],
            "created_at_utc": manifest[
                "created_at_utc"
            ],
            "required_job_count": len(required),
            "verified_completed_jobs": found,
            "missing_jobs": missing,
            "incomplete_jobs": incomplete,
            "recovery_point_satisfied": (
                not missing and not incomplete
            ),
            "table_counts": manifest["table_counts"],
        }

    finally:
        connection.close()


def require_recovery_point(audit):
    """
    Fail closed when the verified backup cannot
    recover the requested completed research jobs.
    """
    if not audit["recovery_point_satisfied"]:
        raise BackupFreshnessError(
            "Backup does not contain every required "
            "completed research job."
        )

    return True
