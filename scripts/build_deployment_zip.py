"""Build a clean, portable InsightIQ deployment ZIP (no credentials, no environments, no caches).

* The database is snapshotted from the LIVE runtime working copy with SQLite's backup API
  (read-only source connection), so the running application is neither stopped nor modified.
* Every packaged file is scanned for credential values from .env / .env.test (values are read
  in-process and never printed) and for known provider key patterns; any hit aborts the build.
* deployment_manifest.json records SHA-256 for every file, database counts and research runs.

Usage: .venv\\Scripts\\python.exe scripts\\build_deployment_zip.py <output_dir>
"""
import fnmatch
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
PACKAGE_NAME = "InsightIQ"

INCLUDE = [
    "README.md", "README_INSIGHTIQ.md", "DEPLOYMENT.md", ".env.example", ".gitignore",
    "pytest.ini", "requirements.lock.txt",
    ".vscode/*.json",
    "backend/requirements.txt", "backend/app/**/*.py",
    "frontend/index.html", "frontend/package.json", "frontend/package-lock.json",
    "frontend/vite.config.js", "frontend/.gitignore", "frontend/src/**/*",
    "scripts/*.py", "scripts/*.ps1",
    "tests/*.py",
    "data/server_startup_config.json",
    "data/sqlite_backups/*.sqlite3", "data/sqlite_backups/*.manifest.json",
    "data/backups/*.sqlite3",
    "data/forecast_cache_v1/*.json", "data/forecast_cache_v2/*.json", "data/research_job_journal/*.json", "data/job_backup_status/*.json",
    "reports/*.json", "reports/research_runs/*.json", "reports/news_refresh_frontend_backup/**/*",
    "migration_reports/*.md", "migration_reports/artifacts/baseline_original.json",
    "migration_reports/artifacts/*.diff",
]
EXCLUDE = [
    ".env", ".env.*", "!.env.example", "*/__pycache__/*", "*.pyc", "*.bak", "*.before_*",
    "*-wal", "*-shm", "*.tmp", "*.tmp-*", "*.lock", "*.startup.lock",
    "data/insightiq.sqlite3",  # replaced by the backup-API snapshot below
]
SECRET_PATTERNS = [re.compile(rb"gsk_[A-Za-z0-9]{20,}"), re.compile(rb"pub_[A-Za-z0-9]{20,}")]


def excluded(rel):
    if rel == ".env.example":
        return False
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p) for p in EXCLUDE if not p.startswith("!"))


def collect():
    files = set()
    for pattern in INCLUDE:
        for path in ROOT.glob(pattern):
            if path.is_file():
                rel = path.relative_to(ROOT).as_posix()
                if not excluded(rel):
                    files.add(rel)
    return sorted(files)


def secret_values():
    from dotenv import dotenv_values

    values = set()
    for name in (".env", ".env.test"):
        path = ROOT / name
        if path.is_file():
            for key, value in dotenv_values(path).items():
                value = (value or "").strip()
                if key not in ("API_ACCESS_KEY", "AZURE_OPENAI_API_KEY", "NEWSDATA_API_KEY", "FINNHUB_API_KEY", "FMP_API_KEY", "SEC_USER_AGENT"):
                    continue
                if len(value) < 8 or "placeholder" in value or "example.invalid" in value:
                    continue  # empty or the deliberately fake offline-test values
                values.add(value.encode("utf-8"))
    return values


def scan(data, secrets):
    return any(s in data for s in secrets) or any(p.search(data) for p in SECRET_PATTERNS)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def table_counts(con):
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def snapshot_database(target):
    from app.database.sqlite_manager import working_database_path  # needs settings env

    source = working_database_path().resolve()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, \
            closing(sqlite3.connect(str(target))) as dst:
        src.backup(dst)
        source_counts = table_counts(src)
    with closing(sqlite3.connect(str(target))) as con:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        counts = table_counts(con)
        runs = [dict(zip(("run_id", "ticker", "status", "created_at_utc"), r)) for r in con.execute(
            "SELECT run_id, ticker, status, created_at_utc FROM research_execution_runs ORDER BY created_at_utc")]
    assert integrity == "ok", integrity
    assert counts == source_counts, (counts, source_counts)
    return counts, runs


def main():
    out_dir = Path(sys.argv[1]).resolve()
    assert not out_dir.is_relative_to(ROOT), "Output must be outside the project folder"
    out_dir.mkdir(parents=True, exist_ok=True)

    import os
    from dotenv import load_dotenv

    # Settings validation needs values; the offline profile is enough to locate the runtime DB.
    load_dotenv(ROOT / ".env.test", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(ROOT))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = out_dir / f"InsightIQ_deploy_{stamp}.zip"
    secrets = secret_values()
    files = collect()

    with tempfile.TemporaryDirectory(prefix="iq_pkg_") as tmp:
        db_snapshot = Path(tmp) / "insightiq.sqlite3"
        counts, runs = snapshot_database(db_snapshot)

        entries = [(rel, (ROOT / rel).read_bytes()) for rel in files]
        entries.append(("data/insightiq.sqlite3", db_snapshot.read_bytes()))

        hits = [rel for rel, data in entries if scan(data, secrets)]
        if hits:
            sys.exit(f"ABORTED: credential material found in {len(hits)} file(s): {hits}")

        manifest = {
            "package": PACKAGE_NAME,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_project_root": str(ROOT),
            "database_snapshot": {"method": "sqlite3 backup API from runtime working copy (read-only)",
                                  "integrity": "ok", "table_counts": counts, "research_runs": runs},
            "completed_reports": sorted(r["run_id"] for r in runs if r["status"] == "completed"),
            "excluded": [".env", ".env.test", ".venv", "frontend/node_modules", "frontend/dist", "logs",
                         "migration_inputs", "__pycache__", "*.bak", "-wal/-shm/tmp files", ".claude",
                         "Colab build manifests"],
            "files": {rel: {"bytes": len(data), "sha256": sha256(data)} for rel, data in entries},
        }
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        assert not scan(manifest_bytes, secrets)

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for rel, data in entries:
                zf.writestr(f"{PACKAGE_NAME}/{rel}", data)
            zf.writestr(f"{PACKAGE_NAME}/deployment_manifest.json", manifest_bytes)
            for empty in ("logs", "observability", "releases"):
                zf.writestr(f"{PACKAGE_NAME}/{empty}/.keep", b"")

    print(json.dumps({"zip": str(zip_path), "files": len(entries) + 1, "zip_bytes": zip_path.stat().st_size,
                      "db_counts": {k: counts[k] for k in ("sec_facts", "news_articles", "research_execution_runs", "companies")},
                      "completed_reports": len(manifest["completed_reports"])}, indent=1))


if __name__ == "__main__":
    main()
