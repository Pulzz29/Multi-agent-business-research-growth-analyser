import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.portability import localize_stored_path

from app.services.research_jobs import read_job
from app.services.sqlite_recovery import create_verified_backup
from app.services.backup_freshness import (
    audit_backup_jobs,
    require_recovery_point,
)


class ResearchBackupError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def backup_status_path(root, run_id, directory=None):
    if str(uuid.UUID(str(run_id))) != str(run_id):
        raise ResearchBackupError(
            "Invalid research run identifier."
        )

    base = (
        Path(directory)
        if directory is not None
        else Path(root) / "data" / "job_backup_status"
    )

    return base / (run_id + ".json")


def save_backup_status(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_name(
        path.name + ".tmp-" + uuid.uuid4().hex
    )

    try:
        temporary.write_text(
            json.dumps(
                record,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )

        os.replace(temporary, path)

    finally:
        if temporary.exists():
            temporary.unlink()


def backup_completed_research_job(
    root,
    run_id,
    backup_factory=None,
    audit_factory=None,
    status_directory=None,
):
    """
    Produce a verified recovery point for a completed
    research job, without changing that job's status.

    The optional factories enable isolated failure tests.
    They do not generate financial or news data.
    """
    root = Path(root).resolve()

    job = read_job(run_id)

    if job["status"] != "completed":
        raise ResearchBackupError(
            "Only completed research jobs may be backed up."
        )

    if not job.get("report_path"):
        raise ResearchBackupError(
            "Completed research report is unavailable."
        )

    report_path = localize_stored_path(job["report_path"], root)

    expected_path = (
        root / "reports" / "research_runs"
        / (run_id + ".json")
    ).resolve()

    if (
        report_path != expected_path
        or not report_path.is_file()
    ):
        raise ResearchBackupError(
            "Completed research report path mismatch."
        )

    document = json.loads(
        report_path.read_text(encoding="utf-8")
    )

    if (
        document.get("run_id") != run_id
        or document.get("ticker") != job["ticker"]
        or document.get("status") != "completed"
    ):
        raise ResearchBackupError(
            "Completed research report identity mismatch."
        )

    status_path = backup_status_path(
        root,
        run_id,
        directory=status_directory,
    )

    record = {
        "run_id": run_id,
        "ticker": job["ticker"],
        "research_status": "completed",
        "backup_status": "pending",
        "checked_at_utc": utc_now(),
        "backup_path": None,
        "manifest_path": None,
        "backup_sha256": None,
        "error_code": None,
    }

    save_backup_status(status_path, record)

    make_backup = (
        backup_factory
        if backup_factory is not None
        else create_verified_backup
    )

    audit_backup = (
        audit_factory
        if audit_factory is not None
        else audit_backup_jobs
    )

    try:
        backup_path, manifest_path = (
            make_backup(root)
        )

        audit = audit_backup(
            backup_path,
            manifest_path,
            [run_id],
        )

        require_recovery_point(audit)

        if (
            run_id not in audit[
                "verified_completed_jobs"
            ]
        ):
            raise ResearchBackupError(
                "Required job not present in backup."
            )

        if audit[
            "verified_completed_jobs"
        ][run_id]["ticker"] != job["ticker"]:
            raise ResearchBackupError(
                "Backup company mismatch."
            )

        record.update({
            "backup_status": "verified",
            "checked_at_utc": utc_now(),
            "backup_path": str(backup_path),
            "manifest_path": str(manifest_path),
            "backup_sha256": audit[
                "backup_sha256"
            ],
        })

    except Exception:
        # Persist a safe failure marker; never save
        # provider exception text, file internals,
        # API keys or business data.
        record.update({
            "backup_status": "failed",
            "checked_at_utc": utc_now(),
            "error_code": "research_backup_failed",
        })

    save_backup_status(status_path, record)

    return record


def get_backup_status(root, run_id):
    path = backup_status_path(root, run_id)

    if not path.is_file():
        return {
            "run_id": run_id,
            "backup_status": "unknown",
            "error_code": "backup_status_unavailable",
        }

    record = json.loads(
        path.read_text(encoding="utf-8")
    )

    if record.get("run_id") != run_id:
        raise ResearchBackupError(
            "Backup status identity mismatch."
        )

    return record
