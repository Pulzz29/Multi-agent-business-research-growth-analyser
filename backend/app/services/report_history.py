"""
Read-only research history.

A research run can be evidenced by three stores, written by
services/research_jobs.py:

* the research_execution_runs table (authoritative job lifecycle),
* data/research_job_journal/{run_id}.json (lifecycle mirror),
* reports/research_runs/{run_id}.json (the completed report itself).

The database can lose rows when the runtime copy is rebuilt from an
older snapshot (see migration_reports), while the report files remain.
This module merges the three so no saved report becomes unreachable.
It never writes and never modifies a stored report.
"""

import json
import threading
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.database.sqlite_manager import connect

SUPPORTED_TICKERS = frozenset({"AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA"})

_META_LOCK = threading.Lock()
_META_CACHE = {}


class ReportHistoryError(LookupError):
    pass


class ReportUnavailable(RuntimeError):
    pass


def project_root():
    return Path(get_settings().project_root).resolve()


def reports_directory(root=None):
    return (Path(root or project_root()) / "reports" / "research_runs").resolve()


def summaries_directory(root=None):
    return reports_directory(root) / "summaries"


def canonical_run_id(value):
    try:
        canonical = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None
    return canonical if canonical == str(value) else None


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _len(value):
    return len(value) if isinstance(value, list) else 0


def report_metadata(path):
    """Summarise a stored report file; cached by (mtime, size). None if invalid."""
    path = Path(path)
    run_id = canonical_run_id(path.stem)
    if run_id is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)

    with _META_LOCK:
        cached = _META_CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]

    doc = _read_json(path)
    meta = None
    if (
        isinstance(doc, dict)
        and doc.get("run_id") == run_id
        and doc.get("ticker") in SUPPORTED_TICKERS
        and doc.get("status") == "completed"
    ):
        financial = doc.get("financial") or {}
        news = doc.get("news") or {}
        strategic = doc.get("strategic") or {}
        claims = (financial.get("verified_claims") or {}).get("verified_claims")
        meta = {
            "run_id": run_id,
            "ticker": doc["ticker"],
            "generated_at_utc": doc.get("generated_at_utc"),
            "total_elapsed_seconds": doc.get("total_elapsed_seconds"),
            "agent_statuses": doc.get("agent_statuses"),
            "agent_timings_seconds": doc.get("agent_timings_seconds"),
            "counts": {
                "financial_claims": _len(claims),
                "news_articles": _len(news.get("approved_primary_articles")),
                "contextual_articles": _len(news.get("contextual_articles")),
                "strategic_priorities": _len(strategic.get("research_priorities")),
                "forecast_metrics": len(financial.get("forecasts") or {}),
            },
        }

    with _META_LOCK:
        _META_CACHE[path] = (key, meta)
    return meta


def read_summary(run_id, root=None):
    path = summaries_directory(root) / f"{run_id}.json"
    doc = _read_json(path) if path.is_file() else None
    if isinstance(doc, dict) and doc.get("run_id") == run_id:
        return doc
    return None


def _db_rows(ticker=None):
    query = "SELECT * FROM research_execution_runs"
    params = ()
    if ticker:
        query += " WHERE ticker = ?"
        params = (ticker,)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    out = {}
    for row in rows:
        record = dict(row)
        try:
            record["agent_statuses"] = json.loads(record.pop("agent_statuses_json") or "{}")
        except ValueError:
            record["agent_statuses"] = {}
        out[record["run_id"]] = record
    return out


def _journal_rows(root, ticker=None):
    out = {}
    for path in (Path(root) / "data" / "research_job_journal").glob("*.json"):
        run_id = canonical_run_id(path.stem)
        doc = _read_json(path) if run_id else None
        if not isinstance(doc, dict) or doc.get("run_id") != run_id:
            continue
        if doc.get("ticker") not in SUPPORTED_TICKERS or (ticker and doc["ticker"] != ticker):
            continue
        out[run_id] = doc
    return out


def list_runs(ticker=None, limit=100):
    if ticker is not None:
        ticker = str(ticker).strip().upper()
        if ticker not in SUPPORTED_TICKERS:
            raise ReportHistoryError("Unsupported company ticker.")

    root = project_root()
    db = _db_rows(ticker)
    journal = _journal_rows(root, ticker)

    reports = {}
    skipped = 0
    for path in reports_directory(root).glob("*.json"):
        meta = report_metadata(path)
        if meta is None:
            if canonical_run_id(path.stem):
                skipped += 1
            continue
        if ticker and meta["ticker"] != ticker:
            continue
        reports[meta["run_id"]] = meta

    runs = []
    for run_id in set(db) | set(journal) | set(reports):
        row = db.get(run_id)
        mirror = journal.get(run_id)
        report = reports.get(run_id)
        base = row or mirror or {}
        run_ticker = base.get("ticker") or (report or {}).get("ticker")

        # A report whose company disagrees with its job record is never shown.
        if report and run_ticker and report["ticker"] != run_ticker:
            report = None

        sources = [name for name, present in (
            ("database", row), ("journal", mirror), ("report_file", report)) if present]

        status = base.get("status") or ("completed" if report else "unknown")
        summary = read_summary(run_id, root) if report else None
        runs.append({
            "run_id": run_id,
            "ticker": run_ticker,
            "status": status,
            "created_at_utc": base.get("created_at_utc"),
            "started_at_utc": base.get("started_at_utc"),
            "finished_at_utc": base.get("finished_at_utc"),
            "updated_at_utc": base.get("updated_at_utc"),
            "agent_statuses": base.get("agent_statuses") or (report or {}).get("agent_statuses"),
            "agent_timings_seconds": (report or {}).get("agent_timings_seconds"),
            "total_elapsed_seconds": (report or {}).get("total_elapsed_seconds"),
            "error_type": base.get("error_type"),
            "error_code": base.get("error_code"),
            "report_available": bool(report) and status == "completed",
            "report_generated_at_utc": (report or {}).get("generated_at_utc"),
            "counts": (report or {}).get("counts"),
            "record_sources": sources,
            "in_database": bool(row),
            "summary_status": (summary or {}).get("status"),
        })

    def sort_key(run):
        return run["created_at_utc"] or run["report_generated_at_utc"] or ""

    runs.sort(key=sort_key, reverse=True)

    latest = next((r["run_id"] for r in runs if r["report_available"]), None)
    for run in runs:
        run["is_latest_completed"] = run["run_id"] == latest

    return {
        "ticker": ticker,
        "run_count": len(runs),
        "completed_report_count": sum(1 for r in runs if r["report_available"]),
        "latest_completed_run_id": latest,
        "skipped_invalid_report_files": skipped,
        "runs": runs[:limit],
    }


def latest_completed(ticker):
    history = list_runs(ticker, limit=1000)
    for run in history["runs"]:
        if run["report_available"]:
            return {
                "run_id": run["run_id"],
                "generated_at_utc": run["report_generated_at_utc"],
                "created_at_utc": run["created_at_utc"],
                "counts": run["counts"],
            }
    return None


def read_report(ticker, run_id):
    """Return the original saved report, unchanged, plus its summary sidecar."""
    ticker = str(ticker or "").strip().upper()
    if ticker not in SUPPORTED_TICKERS:
        raise ReportHistoryError("Unsupported company ticker.")
    run_id = canonical_run_id(run_id)
    if run_id is None:
        raise ValueError("Invalid research run ID.")

    root = project_root()
    directory = reports_directory(root)
    path = (directory / f"{run_id}.json").resolve()
    if not path.is_relative_to(directory):
        raise ValueError("Invalid research run ID.")

    row = _db_rows(ticker).get(run_id)
    if row is not None and row.get("status") != "completed":
        raise ReportUnavailable("Research report is not completed.")

    if not path.is_file():
        raise ReportHistoryError("Research report not found.")

    doc = _read_json(path)
    if (
        not isinstance(doc, dict)
        or doc.get("run_id") != run_id
        or doc.get("status") != "completed"
    ):
        raise ReportUnavailable("Stored report failed its identity check.")
    if doc.get("ticker") != ticker:
        raise ReportHistoryError("Research report not found.")

    return {
        "report": doc,
        "summary": read_summary(run_id, root),
        "record": {
            "in_database": row is not None,
            "created_at_utc": (row or {}).get("created_at_utc"),
            "finished_at_utc": (row or {}).get("finished_at_utc"),
        },
    }
