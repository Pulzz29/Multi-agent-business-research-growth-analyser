"""
Copy InsightIQ's SQLite data into PostgreSQL (repeatable, never duplicates).

1. Chooses the source: --source PATH, or the SQLite database with the newest
   recorded activity among the runtime working copy and data/insightiq.sqlite3.
2. Backs it up first: a consistent, integrity-checked copy in
   data/backups/pre_postgres_<UTC>.sqlite3. The source file is opened
   read-only and is never modified or deleted.
3. Applies the PostgreSQL schema migrations.
4. Copies every table in foreign-key order (INSERT ... ON CONFLICT DO
   NOTHING), then moves identity sequences past the copied ids.
5. Validates: row counts and a SHA-256 checksum of every row per table, and
   writes docs/validation_evidence/postgres_migration_<UTC>.json.

Exit code 0 only when every table matches.

Usage: .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py [--source PATH] [--env-file PATH]
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def choose_source():
    from app.database.sqlite_manager import drive_database_path, working_database_path
    from app.services.runtime_location import latest_activity

    candidates = [p for p in (working_database_path(), drive_database_path()) if p.is_file()]
    if not candidates:
        sys.exit("No SQLite database found to migrate.")
    # Newest recorded activity wins; the runtime working copy wins a tie.
    return max(candidates, key=lambda p: (latest_activity(p) or "", p == working_database_path()))


def backup_source(source, stamp):
    target_dir = PROJECT_ROOT / "data" / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"pre_postgres_{stamp}.sqlite3"
    src = sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        src.close()
        dst.close()
    if not ok:
        sys.exit(f"Backup {target} failed its integrity check; nothing was migrated.")
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--source", help="SQLite file to migrate (default: newest InsightIQ database)")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.database import db
    from app.database.migrations import run_migrations
    from app.database.sqlite_to_postgres import copy_database, validate

    if not db.is_postgres():
        sys.exit("DB_BACKEND must be postgres to migrate into PostgreSQL.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source = Path(args.source).resolve() if args.source else choose_source()
    target = db.describe_target()
    print("Source:", source)
    print(f"Target: {target['host']}:{target['port']}/{target['database']} schema={target['schema']}")

    backup = backup_source(source, stamp)
    print("Backup (verified):", backup)

    applied = run_migrations()
    print("Migrations applied now:", applied or "none")

    conn = db.direct_connect()
    try:
        results = copy_database(
            backup, conn,
            progress=lambda t, r: print(f"  {t}: {r['source_rows']} rows, {r['inserted']} new"),
        )
        report = validate(backup, conn)
    finally:
        conn.close()

    mismatched = [t for t, r in report.items() if not r["match"]]
    evidence = {
        "check": "sqlite_to_postgres_migration",
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "verified_backup": str(backup),
        "target": target,
        "migrations_applied_now": applied,
        "copy": results,
        "validation": report,
        "all_tables_match": not mismatched,
    }
    out = PROJECT_ROOT / "docs" / "validation_evidence" / f"postgres_migration_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    print()
    for table, r in report.items():
        flag = "OK " if r["match"] else "MISMATCH"
        print(f"{flag} {table}: sqlite={r['sqlite_rows']} postgres={r['postgres_rows']}")
    print("Evidence:", out.relative_to(PROJECT_ROOT))
    if mismatched:
        print("Tables that do not match:", ", ".join(mismatched))
        return 1
    print("All tables match (row counts and checksums).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
