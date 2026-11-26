"""
Store a PDF in Azure Blob Storage for every saved research report and sector
comparison (idempotent; immutable blobs; no AI or provider calls).

PDFs are rendered deterministically from the structured content in PostgreSQL
with each report's original generation time. Already-saved files are verified,
not re-uploaded. Needs AZURE_STORAGE_ACCOUNT_URL and an identity with Storage
Blob Data Contributor on the account.

    python scripts/backfill_report_pdfs.py [--env-file PATH]
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
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))
    from app.database import db
    from app.database.migrations import run_migrations
    from app.services import report_blob, report_history, sector_comparison

    if not report_blob.enabled():
        print("AZURE_STORAGE_ACCOUNT_URL is not set; nothing to do.")
        return 2
    run_migrations()
    summary = {"research": {"saved": 0, "failed": 0}, "sector": {"saved": 0, "failed": 0}}
    for report in report_history.saved_report_catalog():
        report_blob.research_pdf(report["ticker"], report["run_id"])
        meta = report_blob.research_meta(report["ticker"], report["run_id"])
        row = report_blob._row(meta["blob_name"])
        summary["research"]["saved" if row and row["status"] == "saved" else "failed"] += 1
    for snap in sector_comparison.list_snapshots(limit=10_000):
        snapshot = sector_comparison.read_snapshot(snap["comparison_id"])
        name = report_blob.sector_filename(snapshot, "pdf")
        report_blob.sector_file(snapshot, "pdf", name)
        row = report_blob._row(report_blob.sector_meta(snapshot, "pdf", name)["blob_name"])
        summary["sector"]["saved" if row and row["status"] == "saved" else "failed"] += 1
    with db.connect() as conn:
        summary["report_files"] = conn.execute(
            "SELECT status, COUNT(*) AS n FROM report_files GROUP BY status").fetchall()
    summary["report_files"] = {r["status"]: r["n"] for r in summary["report_files"]}
    print(json.dumps(summary, indent=2))
    return 0 if not (summary["research"]["failed"] or summary["sector"]["failed"]) else 1


if __name__ == "__main__":
    sys.exit(main())
