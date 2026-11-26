"""
Scheduled native PostgreSQL backups inside the application (App Service).

Enabled when BACKUP_INTERVAL_HOURS > 0 on PostgreSQL. Every interval one
instance (PostgreSQL advisory lock) runs the existing ``db_backup.backup``:
pg_dump of a consistent snapshot, restore into an isolated temporary database
to verify counts, local retention, then upload of the dump (and an optional
SQLite export, BACKUP_EXPORT_SQLITE=true) to the private Blob container
AZURE_STORAGE_BACKUP_CONTAINER with SHA-256 verification. Failures are
recorded in ``error_events``; the running application is never affected.
Azure Flexible Server point-in-time restore remains an independent safety net.
"""

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("insightiq.backup_scheduler")
_LOCK_KEY = 784_221_903  # advisory lock id for "insightiq scheduled backup"
_started = False


def interval_hours():
    try:
        return float(os.getenv("BACKUP_INTERVAL_HOURS", "0") or 0)
    except ValueError:
        return 0.0


def run_once():
    """One scheduled backup cycle. Returns a result dict (never raises)."""
    from app.database import db
    from app.services import db_backup, errors, report_blob

    conn = db.direct_connect()
    try:
        if not conn.execute("SELECT pg_try_advisory_lock(?)", (_LOCK_KEY,)).fetchone()[0]:
            return {"skipped": "another instance is running the backup"}
        result = db_backup.backup(verify=True)
        uploaded = []
        if report_blob.enabled():
            container = os.getenv("AZURE_STORAGE_BACKUP_CONTAINER", "db-backups")
            files = [Path(result["path"])]
            if os.getenv("BACKUP_EXPORT_SQLITE", "").lower() in ("1", "true", "yes"):
                export = db_backup.export_sqlite(Path(result["path"]).with_suffix(".sqlite3"))
                files.append(Path(export["file"]))
            for path in files:
                digest = report_blob.upload_file(container, f"postgres/{path.name}", path,
                                                 {"restore_verified": str(result["restore_verified"]).lower()})
                uploaded.append({"blob": f"postgres/{path.name}", "sha256": digest})
        result["uploaded"] = uploaded
        logger.info("scheduled_backup ok file=%s verified=%s uploaded=%s", result["file"],
                    result["restore_verified"], len(uploaded))
        return result
    except Exception as exc:
        errors.record("backup:scheduled", exc, severity="critical", component="db_backup", final_status="failed")
        return {"failed": type(exc).__name__}
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(?)", (_LOCK_KEY,))
            conn.close()
        except Exception:
            pass


def start():
    """Start the background schedule once per process (no-op when disabled)."""
    global _started
    hours = interval_hours()
    if _started or hours <= 0:
        return False
    from app.database import db

    if not db.is_postgres():
        return False
    _started = True

    def loop():
        time.sleep(float(os.getenv("BACKUP_FIRST_DELAY_SECONDS", "900")))  # let the app settle after start
        while True:
            run_once()
            time.sleep(hours * 3600)

    threading.Thread(target=loop, name="backup-scheduler", daemon=True).start()
    return True
