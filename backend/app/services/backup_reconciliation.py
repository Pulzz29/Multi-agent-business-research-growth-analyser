import json
import sqlite3
from pathlib import Path

from app.core.portability import localize_stored_path

from app.database.sqlite_manager import connect
from app.services.research_jobs import read_job
from app.services.research_backup import (
    backup_status_path,
    backup_completed_research_job,
)
from app.services.backup_freshness import (
    audit_backup_jobs,
)


class BackupReconciliationError(RuntimeError):
    pass


def inspect_completed_backup(
    root,
    run_id,
    status_directory=None,
):
    """
    Verify the backup outcome and recovery point for
    one genuinely completed research run.
    """
    root = Path(root).resolve()
    job = read_job(run_id)

    if job["status"] != "completed":
        return {
            "run_id": run_id,
            "state": "not_completed",
            "repair_needed": False,
        }

    path = backup_status_path(
        root,
        run_id,
        directory=status_directory,
    )

    if not path.is_file():
        return {
            "run_id": run_id,
            "state": "missing_backup_status",
            "repair_needed": True,
        }

    try:
        outcome = json.loads(
            path.read_text(encoding="utf-8")
        )

        if not isinstance(outcome, dict):
            raise ValueError("Invalid backup outcome.")

        if (
            outcome.get("run_id") != run_id
            or outcome.get("ticker") != job["ticker"]
            or outcome.get("research_status") != "completed"
        ):
            raise ValueError("Backup outcome identity mismatch.")

        if outcome.get("backup_status") != "verified":
            return {
                "run_id": run_id,
                "state": "backup_not_verified",
                "repair_needed": True,
            }

        backup_path = localize_stored_path(
            outcome["backup_path"], root
        )

        manifest_path = localize_stored_path(
            outcome["manifest_path"], root
        )

        allowed_directory = (
            root / "data" / "sqlite_backups"
        ).resolve()

        # Saved outcome paths must remain in the
        # expected project-specific backup directory.
        if (
            backup_path.parent != allowed_directory
            or manifest_path.parent != allowed_directory
        ):
            raise ValueError(
                "Backup path is outside allowed directory."
            )

        audit = audit_backup_jobs(
            backup_path,
            manifest_path,
            [run_id],
        )

        if not audit["recovery_point_satisfied"]:
            raise ValueError(
                "Required completed job missing from backup."
            )

        if (
            audit["backup_sha256"]
            != outcome["backup_sha256"]
        ):
            raise ValueError(
                "Backup checksum differs from outcome."
            )

        if (
            audit["verified_completed_jobs"][run_id]["ticker"]
            != job["ticker"]
        ):
            raise ValueError(
                "Backup company identity mismatch."
            )

        return {
            "run_id": run_id,
            "state": "verified",
            "repair_needed": False,
            "backup_path": str(backup_path),
        }

    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
    ):
        return {
            "run_id": run_id,
            "state": "invalid_backup_status_or_snapshot",
            "repair_needed": True,
        }


def completed_run_ids():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id
            FROM research_execution_runs
            WHERE status = 'completed'
            ORDER BY created_at_utc, run_id
            """
        ).fetchall()

    return [row["run_id"] for row in rows]


def reconcile_backups(
    root,
    run_ids=None,
    repair=False,
    status_directory=None,
    backup_factory=None,
):
    """
    Inspect completed runs. Optional repair attempts
    create or verify a backup using the existing
    backup coordinator; no AI calls are made.

    Does not modify research job statuses.
    """
    root = Path(root).resolve()
    selected = (
        completed_run_ids()
        if run_ids is None
        else list(run_ids)
    )

    if len(selected) != len(set(selected)):
        raise BackupReconciliationError(
            "Duplicate research run identifier."
        )

    results = []

    for run_id in selected:
        before = inspect_completed_backup(
            root,
            run_id,
            status_directory=status_directory,
        )

        item = {
            "run_id": run_id,
            "initial_state": before["state"],
            "repair_attempted": False,
            "final_state": before["state"],
        }

        if repair and before["repair_needed"]:
            item["repair_attempted"] = True

            try:
                outcome = backup_completed_research_job(
                    root,
                    run_id,
                    backup_factory=backup_factory,
                    status_directory=status_directory,
                )

                after = inspect_completed_backup(
                    root,
                    run_id,
                    status_directory=status_directory,
                )

                item["final_state"] = after["state"]
                item["backup_status"] = outcome[
                    "backup_status"
                ]

            except Exception:
                item["final_state"] = "repair_failed"
                item["backup_status"] = "failed"

        results.append(item)

    return {
        "inspected_count": len(results),
        "verified_count": sum(
            item["final_state"] == "verified"
            for item in results
        ),
        "unverified_count": sum(
            item["final_state"] != "verified"
            for item in results
        ),
        "repair_attempts": sum(
            item["repair_attempted"]
            for item in results
        ),
        "results": results,
    }
