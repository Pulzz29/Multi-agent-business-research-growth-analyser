"""Read-only InsightIQ data baseline: SQLite integrity, row counts, report and cache hashes.
SQLITE-ONLY TOOL: works on the SQLite database file (DB_BACKEND=sqlite, rollback and
file recovery). With PostgreSQL use scripts/setup_postgres.py, pg_dump/pg_restore and
scripts/migrate_sqlite_to_postgres.py instead (docs/POSTGRES_MIGRATION.md).


Usage: python scripts/db_baseline.py <project_root> <output_json>
Opens every database with mode=ro; never writes to the data directory.
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2])


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_db(path):
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
        return {"integrity": integrity, "tables": counts}
    finally:
        con.close()


result = {"databases": {}, "files": {}}
main_db = root / "data" / "insightiq.sqlite3"
for db in sorted((root / "data").rglob("*.sqlite3")):
    rel = db.relative_to(root).as_posix()
    entry = {"bytes": db.stat().st_size, "sha256": sha256(db)}
    try:
        entry.update(inspect_db(db))
    except sqlite3.Error as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
    result["databases"][rel] = entry

# Detail for the primary database
con = sqlite3.connect(main_db.resolve().as_uri() + "?mode=ro", uri=True)
con.row_factory = sqlite3.Row
detail = {}
try:
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    if "research_execution_runs" in tables:
        cols = [r[1] for r in con.execute("PRAGMA table_info(research_execution_runs)")]
        detail["research_execution_runs_columns"] = cols
        status_col = "status" if "status" in cols else None
        if status_col:
            detail["research_runs_by_status"] = {
                r[0]: r[1] for r in con.execute(
                    f"SELECT {status_col}, COUNT(*) FROM research_execution_runs GROUP BY {status_col}")}
        keep = [c for c in ("run_id", "ticker", "status", "created_at_utc", "completed_at_utc", "report_path") if c in cols]
        detail["research_runs"] = [dict(r) for r in con.execute(
            f"SELECT {', '.join(keep)} FROM research_execution_runs ORDER BY rowid")] if keep else []
    if "news_articles" in tables:
        cols = [r[1] for r in con.execute("PRAGMA table_info(news_articles)")]
        detail["news_articles_columns"] = cols
        if "company_ticker" in cols:
            detail["news_by_ticker"] = {r[0]: r[1] for r in con.execute(
                "SELECT company_ticker, COUNT(*) FROM news_articles GROUP BY company_ticker ORDER BY company_ticker")}
    if "sec_facts" in tables:
        cols = [r[1] for r in con.execute("PRAGMA table_info(sec_facts)")]
        detail["sec_facts_columns"] = cols
        if "company_ticker" in cols:
            detail["sec_facts_by_ticker"] = {r[0]: r[1] for r in con.execute(
                "SELECT company_ticker, COUNT(*) FROM sec_facts GROUP BY company_ticker ORDER BY company_ticker")}
finally:
    con.close()
result["primary_detail"] = detail

for folder in ("reports", "data/forecast_cache_v1", "data/forecast_cache_v2", "data/research_job_journal", "data/job_backup_status"):
    base = root / folder
    if base.is_dir():
        for p in sorted(base.rglob("*")):
            if p.is_file():
                result["files"][p.relative_to(root).as_posix()] = {"bytes": p.stat().st_size, "sha256": sha256(p)}

out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
m = result["databases"].get("data/insightiq.sqlite3", {})
print("primary integrity:", m.get("integrity"))
print("primary counts:", json.dumps(m.get("tables"), indent=1))
print("databases inspected:", len(result["databases"]),
      "| non-ok:", [k for k, v in result["databases"].items() if v.get("integrity") != "ok"])
print("files hashed:", len(result["files"]))
print(json.dumps({k: v for k, v in detail.items() if not k.endswith("_columns")}, indent=1, default=str)[:4000])
