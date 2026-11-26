"""
Back up the InsightIQ PostgreSQL database, verify the backup by restoring it
into an isolated temporary database, and apply retention.

Settings (environment / .env): BACKUP_DIR (default data\\pg_backups),
BACKUP_RETENTION_COUNT (14), BACKUP_RETENTION_DAYS (30), PG_BIN_DIR.

Usage: .venv\\Scripts\\python.exe scripts\\backup_postgres.py [--no-verify] [--env-file PATH]
Restore: pg_restore -h HOST -U USER -d <new empty database> <dump file>
"""

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--check-files", action="store_true",
                        help="only re-verify the retained dumps (recorded SHA-256 and readability)")
    parser.add_argument("--export-sqlite", action="store_true",
                        help="also write a verified offline SQLite export (or set BACKUP_EXPORT_SQLITE=true)")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.database.migrations import run_migrations
    from app.services import db_backup

    run_migrations()
    if args.check_files:
        results = db_backup.verify_files()
        for item in results:
            print(item)
        return 1 if any(r["sha256_matches"] is False or not r["readable"] for r in results) else 0
    try:
        result = db_backup.backup(verify=not args.no_verify)
    except db_backup.BackupError as exc:
        print("Backup failed:", exc)
        return 1
    if args.export_sqlite or os.getenv("BACKUP_EXPORT_SQLITE", "").strip().lower() in ("1", "true", "yes"):
        from datetime import datetime, timezone

        name = f"insightiq_export_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.sqlite3"
        try:
            export = db_backup.export_sqlite(db_backup.settings()["directory"] / name)
            print("SQLite export (offline option; not a substitute for pg_dump):", json.dumps(export))
            print("Export retention:", db_backup.apply_export_retention())
        except Exception as exc:
            print("SQLite export failed:", exc)
            return 1
    print(json.dumps({k: v for k, v in result.items() if k != "verification"}, indent=2))
    verification = result.get("verification")
    if verification:
        print("Restore verification:", "PASSED" if verification["matched"] else "FAILED",
              f"({verification['tables_compared']} tables, {verification['rows_compared']} rows)")
        for table, diff in verification["differences"].items():
            print("  ", table, diff)
    return 0 if (args.no_verify or result["restore_verified"]) else 1


if __name__ == "__main__":
    sys.exit(main())
