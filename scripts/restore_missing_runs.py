"""Restore research_execution_runs rows that the runtime database lost.
SQLITE-ONLY TOOL: works on the SQLite database file (DB_BACKEND=sqlite, rollback and
file recovery). With PostgreSQL use scripts/setup_postgres.py, pg_dump/pg_restore and
scripts/migrate_sqlite_to_postgres.py instead (docs/POSTGRES_MIGRATION.md).


When the runtime database is rebuilt from an older snapshot, rows for research
runs completed afterwards can disappear even though their report files remain
in reports/research_runs/. This script finds such runs in a verified backup and
re-inserts the original rows unchanged.

Default is a DRY RUN: it only prints what it would insert.

    .venv\\Scripts\\python.exe scripts\\restore_missing_runs.py
    .venv\\Scripts\\python.exe scripts\\restore_missing_runs.py --apply   (backend must be stopped)

--apply:
  1. creates a verified backup of the current runtime database,
  2. INSERT OR IGNORE of the missing rows exactly as stored in the backup,
  3. creates another verified backup and refreshes data/insightiq.sqlite3,
     so a restart (which restores from the newest backup) keeps the rows.
It never updates or deletes anything. No network calls.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

COLUMNS = (
    "run_id", "ticker", "status", "created_at_utc", "updated_at_utc",
    "started_at_utc", "finished_at_utc", "agent_statuses_json",
    "report_path", "error_type", "error_code",
)


def ro_connect(path, immutable=False):
    uri = Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def valid_report(run_id, ticker):
    path = PROJECT_ROOT / "reports" / "research_runs" / f"{run_id}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return doc.get("run_id") == run_id and doc.get("ticker") == ticker and doc.get("status") == "completed"


def find_missing(live_path):
    with ro_connect(live_path) as live:
        present = {r["run_id"] for r in live.execute("SELECT run_id FROM research_execution_runs")}

    candidates = {}
    for backup in sorted((PROJECT_ROOT / "data" / "sqlite_backups").glob("insightiq_*.sqlite3")):
        try:
            con = ro_connect(backup, immutable=True)
            rows = con.execute(
                f"SELECT {', '.join(COLUMNS)} FROM research_execution_runs"
            ).fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for row in rows:
            record = dict(row)
            if record["run_id"] in present or record["run_id"] in candidates:
                continue
            if record["status"] != "completed" or not valid_report(record["run_id"], record["ticker"]):
                continue
            candidates[record["run_id"]] = (backup.name, record)
    return candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    args = parser.parse_args()

    from run_backend import load_environment

    load_environment(Path(args.env_file).resolve())

    from app.database import sqlite_manager

    live_path = sqlite_manager.working_database_path()
    missing = find_missing(live_path)

    print(f"Runtime database: {live_path}")
    if not missing:
        print("No missing research runs with a valid saved report. Nothing to do.")
        return

    for run_id, (backup_name, record) in missing.items():
        print(f"\nMissing run {run_id}  (source backup: {backup_name})")
        for column in COLUMNS:
            print(f"  {column:20} {record[column]}")

    if not args.apply:
        print("\nDRY RUN: nothing was written. Re-run with --apply (backend stopped) to insert these rows.")
        return

    from app.services.sqlite_recovery import create_verified_backup

    before_backup, _ = create_verified_backup(PROJECT_ROOT)
    print(f"\nPre-restore backup: {before_backup.name}")

    with sqlite3.connect(str(live_path), timeout=30) as con:
        before = con.execute("SELECT COUNT(*) FROM research_execution_runs").fetchone()[0]
        for _, record in missing.values():
            con.execute(
                f"INSERT OR IGNORE INTO research_execution_runs ({', '.join(COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                tuple(record[c] for c in COLUMNS),
            )
        after = con.execute("SELECT COUNT(*) FROM research_execution_runs").fetchone()[0]
    con.close()

    after_backup, _ = create_verified_backup(PROJECT_ROOT)
    sqlite_manager.sync_snapshot_to_drive()
    print(f"research_execution_runs: {before} -> {after}")
    print(f"Post-restore backup: {after_backup.name}; data/insightiq.sqlite3 refreshed.")


if __name__ == "__main__":
    main()
