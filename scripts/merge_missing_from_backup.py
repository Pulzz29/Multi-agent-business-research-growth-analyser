"""
Additive recovery: copy rows that exist in a verified backup but are missing
from the current runtime database. Nothing existing is updated or deleted.

Rows are matched on natural keys (never on raw integer IDs, which can be
reissued after a restore); copied rows with an INTEGER id receive new IDs.

Usage:
  .venv\\Scripts\\python.exe scripts\\merge_missing_from_backup.py BACKUP.sqlite3            (dry run)
  .venv\\Scripts\\python.exe scripts\\merge_missing_from_backup.py BACKUP.sqlite3 --apply    (apply)

--apply first creates a fresh verified backup of the current database, then
merges in one transaction, then records a verified recovery point. A JSON
report is written to docs/validation_evidence/.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# table -> (natural key columns, has autoincrement id)
TABLES = {
    "companies": (("ticker",), False),
    "market_companies": (("symbol",), False),
    "market_quotes": (("symbol", "provider", "fetched_at"), True),
    "market_statements": (("symbol", "provider", "period_type", "period_end", "metric", "value_text"), True),
    "news_articles": (("provider", "provider_article_id", "company_ticker"), True),
    "source_refreshes": (("company_ticker", "source", "started_at"), True),
    "research_execution_runs": (("run_id",), False),
    "research_observability_events": (("run_id", "span_id", "event_type", "timestamp_utc"), True),
    "provider_usage": (("provider", "usage_day"), False),
}


def columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def plan(current, backup):
    out = {}
    for table, (key, has_id) in TABLES.items():
        cols_b, cols_c = columns(backup, table), columns(current, table)
        if not cols_b or not cols_c:
            out[table] = {"skipped": "table missing"}
            continue
        shared = [c for c in cols_b if c in cols_c and not (has_id and c == "id")]
        existing = {tuple(r) for r in current.execute(f"SELECT {', '.join(key)} FROM {table}")}
        rows = backup.execute(f"SELECT {', '.join(shared)} FROM {table}").fetchall()
        index = [shared.index(k) for k in key]
        missing = [r for r in rows if tuple(r[i] for i in index) not in existing]
        out[table] = {"columns": shared, "missing": missing, "key": key}
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if not args:
        sys.exit(__doc__)
    backup_path = Path(args[0]).resolve()

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))
    config = json.loads((PROJECT_ROOT / "data" / "server_startup_config.json").read_text(encoding="utf-8"))
    current_path = Path(config["runtime_database_path"]).resolve()

    backup = sqlite3.connect(backup_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit("Backup failed its integrity check; nothing was changed.")
    current = sqlite3.connect(str(current_path), timeout=30)
    current.execute("PRAGMA foreign_keys = ON")

    merge_plan = plan(current, backup)
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "dry_run",
        "backup": backup_path.name,
        "current_database": "runtime database (path from data/server_startup_config.json)",
        "tables": {},
    }
    for table, item in merge_plan.items():
        missing = item.get("missing", [])
        summary = {"rows_missing_in_current": len(missing), "natural_key": list(item.get("key", ()))}
        if table == "research_execution_runs":
            summary["runs"] = [dict(zip(item["columns"], r)) for r in missing]
            for run in summary["runs"]:
                run.pop("agent_statuses_json", None)
        if table == "news_articles":
            summary["articles"] = [
                {k: dict(zip(item["columns"], r)).get(k) for k in ("company_ticker", "provider", "title")}
                for r in missing
            ]
        report["tables"][table] = summary
        print(f"{table:32s} missing in current: {len(missing)}")

    if apply:
        from app.services.sqlite_recovery import create_verified_backup

        before_backup, before_manifest = create_verified_backup(PROJECT_ROOT)
        report["pre_merge_backup"] = Path(before_backup).name
        inserted = {}
        with current:
            current.execute("BEGIN IMMEDIATE")
            # Parents first so foreign keys hold.
            for table in TABLES:
                item = merge_plan[table]
                missing = item.get("missing", [])
                if not missing:
                    inserted[table] = 0
                    continue
                placeholders = ", ".join("?" for _ in item["columns"])
                cursor = current.executemany(
                    f"INSERT OR IGNORE INTO {table} ({', '.join(item['columns'])}) VALUES ({placeholders})",
                    missing,
                )
                inserted[table] = cursor.rowcount
        report["inserted"] = inserted
        if current.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            report["integrity_after_merge"] = "FAILED"
        else:
            report["integrity_after_merge"] = "ok"
        current.close()
        from app.database.sqlite_manager import create_recovery_point, sync_snapshot_to_drive

        report["snapshot"] = bool(sync_snapshot_to_drive(create_timestamped_backup=False).get("integrity_ok"))
        report["recovery_point_verified"] = bool(create_recovery_point("merge_from_backup").get("verified"))
        print("inserted:", inserted, "| integrity:", report["integrity_after_merge"])

    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    out = PROJECT_ROOT / "docs" / "validation_evidence" / f"db_merge_{report['mode']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"report: {out.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
