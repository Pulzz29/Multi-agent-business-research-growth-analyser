"""Independently verify an InsightIQ deployment ZIP.

Checks: ZIP CRC integrity; no forbidden entries (.env, .venv, node_modules, caches, logs, -wal/-shm);
every file matches deployment_manifest.json SHA-256; no credential values (read from this project's
.env/.env.test in-process, never printed) or key patterns anywhere; SQLite integrity and counts;
every completed run has a readable, identity-matching report; recovery-point manifests match their
backup files; forecast cache present.

Usage: .venv\\Scripts\\python.exe scripts\\verify_deployment_zip.py <zip> [--extract-to DIR]
"""
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = [r"(^|/)\.env$", r"(^|/)\.env\.(?!example$)", r"(^|/)\.venv/", r"node_modules/", r"__pycache__/",
             r"\.pyc$", r"\.bak$", r"-wal$", r"-shm$", r"(^|/)logs/(?!\.keep$)", r"migration_inputs/", r"(^|/)frontend/dist/",
             r"(^|/)\.claude/"]
PATTERNS = [re.compile(rb"gsk_[A-Za-z0-9]{20,}"), re.compile(rb"pub_[A-Za-z0-9]{20,}")]


def secrets():
    from dotenv import dotenv_values

    out = set()
    for name in (".env", ".env.test"):
        for key, value in dotenv_values(ROOT / name).items():
            value = (value or "").strip()
            if key in ("API_ACCESS_KEY", "AZURE_OPENAI_API_KEY", "NEWSDATA_API_KEY", "FINNHUB_API_KEY", "FMP_API_KEY", "SEC_USER_AGENT") and len(value) >= 8 \
                    and "placeholder" not in value and "example.invalid" not in value:
                out.add(value.encode())
    return out


def main():
    zip_path = Path(sys.argv[1]).resolve()
    extract_to = Path(sys.argv[sys.argv.index("--extract-to") + 1]) if "--extract-to" in sys.argv else Path(tempfile.mkdtemp(prefix="iq_verify_"))
    results = {}

    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        results["zip_crc_ok"] = bad is None
        names = zf.namelist()
        results["entries"] = len(names)
        results["forbidden_entries"] = [n for n in names if any(re.search(p, n) for p in FORBIDDEN)]
        for n in names:
            assert not n.startswith("/") and ".." not in Path(n).parts, n
        zf.extractall(extract_to)

    root = extract_to / "InsightIQ"
    manifest = json.loads((root / "deployment_manifest.json").read_text(encoding="utf-8"))
    mismatched = [rel for rel, meta in manifest["files"].items()
                  if hashlib.sha256((root / rel).read_bytes()).hexdigest() != meta["sha256"]]
    results["manifest_files"] = len(manifest["files"])
    results["manifest_hash_mismatches"] = mismatched

    sec = secrets()
    hits = []
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if any(s in data for s in sec) or any(p.search(data) for p in PATTERNS):
                hits.append(path.relative_to(root).as_posix())
    results["credential_hits"] = hits
    results["credential_values_checked"] = len(sec)

    db = root / "data" / "insightiq.sqlite3"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        results["db_integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        results["db_counts"] = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                for t in ("sec_facts", "news_articles", "research_execution_runs", "companies")}
        runs = con.execute("SELECT run_id, ticker, status FROM research_execution_runs").fetchall()
    results["db_counts_match_manifest"] = all(
        results["db_counts"][k] == manifest["database_snapshot"]["table_counts"][k] for k in results["db_counts"])

    report_problems = []
    completed = [r for r in runs if r[2] == "completed"]
    for run_id, ticker, _ in completed:
        path = root / "reports" / "research_runs" / f"{run_id}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc["run_id"] != run_id or doc["ticker"] != ticker or doc["status"] != "completed":
                report_problems.append(run_id)
        except Exception:
            report_problems.append(run_id)
    results["completed_runs"] = len(completed)
    results["report_problems"] = report_problems

    backup_problems = []
    for m in (root / "data" / "sqlite_backups").glob("*.manifest.json"):
        meta = json.loads(m.read_text(encoding="utf-8"))
        f = m.parent / meta["backup_file"]
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != meta["sha256"]:
            backup_problems.append(m.name)
    results["recovery_point_manifests"] = len(list((root / "data" / "sqlite_backups").glob("*.manifest.json")))
    results["recovery_point_problems"] = backup_problems
    results["forecast_cache_files"] = len(list((root / "data").glob("forecast_cache_v*/*.json")))
    results["extracted_to"] = str(extract_to)

    ok = (results["zip_crc_ok"] and not results["forbidden_entries"] and not mismatched and not hits
          and results["db_integrity"] == "ok" and results["db_counts_match_manifest"] and not report_problems
          and not backup_problems and results["forecast_cache_files"] == 12)
    results["VERDICT"] = "PASS" if ok else "FAIL"
    print(json.dumps(results, indent=1))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
