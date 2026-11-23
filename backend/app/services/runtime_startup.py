from pathlib import Path

from app.services.server_bootstrap import (
    bootstrap_api_database,
)

from app.services.backup_reconciliation import (
    reconcile_backups,
)


class RuntimeStartupError(RuntimeError):
    pass


def prepare_server_runtime(
    runtime_database_path,
    backup_directory,
    project_root,
    bootstrap_fn=None,
    reconcile_fn=None,
):
    """
    Controlled server startup sequence:

    1. Restore and verify runtime SQLite if missing.
    2. Reconcile interrupted queued/running jobs.
    3. Verify or repair completed-job backup records.
    4. Permit API startup only if every recovery point
       passes verification.

    Must run before app.main is imported and before
    background workers are created.

    Only one server owner may perform startup recovery.
    """
    root = Path(project_root).resolve()

    bootstrap = (
        bootstrap_fn
        if bootstrap_fn is not None
        else bootstrap_api_database
    )

    reconciler = (
        reconcile_fn
        if reconcile_fn is not None
        else reconcile_backups
    )

    try:
        database_result = bootstrap(
            runtime_database_path,
            backup_directory,
            enforce_source_identity=True,
        )

    except Exception as exc:
        raise RuntimeStartupError(
            "Database startup recovery failed."
        ) from exc

    if database_result.get("integrity") != "ok":
        raise RuntimeStartupError(
            "Database startup integrity was not verified."
        )

    try:
        backup_result = reconciler(
            root,
            repair=True,
        )

    except Exception as exc:
        raise RuntimeStartupError(
            "Completed-job backup reconciliation failed."
        ) from exc

    required_fields = (
        "inspected_count",
        "verified_count",
        "unverified_count",
        "repair_attempts",
    )

    if any(
        field not in backup_result
        for field in required_fields
    ):
        raise RuntimeStartupError(
            "Backup reconciliation result is incomplete."
        )

    if (
        backup_result["unverified_count"] != 0
        or backup_result["verified_count"]
           != backup_result["inspected_count"]
    ):
        raise RuntimeStartupError(
            "Completed research recovery points "
            "are not fully verified."
        )

    return {
        "status": "ready_for_api_import",
        "database_restored": database_result[
            "database_restored"
        ],
        "database_integrity": "ok",
        "interrupted_jobs_reconciled": len(
            database_result.get(
                "interrupted_jobs_reconciled",
                [],
            )
        ),
        "completed_jobs_inspected": backup_result[
            "inspected_count"
        ],
        "verified_recovery_points": backup_result[
            "verified_count"
        ],
        "backup_repair_attempts": backup_result[
            "repair_attempts"
        ],
        "unverified_recovery_points": 0,
    }


def read_backup_health(project_root):
    """
    Read-only health assessment.

    Does not perform backups or repair research jobs.
    """
    root = Path(project_root).resolve()

    try:
        audit = reconcile_backups(
            root,
            repair=False,
        )

    except Exception as exc:
        raise RuntimeStartupError(
            "Backup health is unavailable."
        ) from exc

    inspected = audit["inspected_count"]
    verified = audit["verified_count"]
    unverified = audit["unverified_count"]

    return {
        "status": (
            "healthy"
            if unverified == 0
            else "degraded"
        ),
        "completed_jobs_checked": inspected,
        "verified_backup_count": verified,
        "unverified_backup_count": unverified,
        "recovery_points_complete": (
            unverified == 0
            and verified == inspected
        ),
    }
