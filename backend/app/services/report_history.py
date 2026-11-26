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

# Companies on the validated SEC pipeline (the original six plus every US
# universe company whose SEC filings reconcile); see sec_coverage.py.
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_TICKERS  # noqa: E402


def is_known_ticker(ticker):
    """The six SEC-validated companies or any registered market company."""
    if ticker in SUPPORTED_TICKERS:
        return True
    try:
        from app.services.market_registry import is_research_ticker

        return is_research_ticker(ticker)
    except Exception:
        return False

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
    from app.services import document_store

    try:
        text = document_store.read_text(path)
        return json.loads(text) if text is not None else None
    except (OSError, ValueError, TypeError, document_store.DocumentIntegrityError):
        return None


def _len(value):
    return len(value) if isinstance(value, list) else 0


def report_metadata(path):
    """Summarise a stored report file; cached by (mtime, size). None if invalid."""
    path = Path(path)
    run_id = canonical_run_id(path.stem)
    if run_id is None:
        return None
    from app.services import document_store

    key = document_store.version(path)
    if key is None:
        return None

    with _META_LOCK:
        cached = _META_CACHE.get(path)
        if cached and cached[0] == key:
            return cached[1]

    doc = _read_json(path)
    meta = None
    if (
        isinstance(doc, dict)
        and doc.get("run_id") == run_id
        and is_known_ticker(doc.get("ticker"))
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
    doc = _read_json(path)
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
    from app.services import document_store

    for path in document_store.list_paths(Path(root) / "data" / "research_job_journal", root=root):
        run_id = canonical_run_id(path.stem)
        doc = _read_json(path) if run_id else None
        if not isinstance(doc, dict) or doc.get("run_id") != run_id:
            continue
        if not is_known_ticker(doc.get("ticker")) or (ticker and doc["ticker"] != ticker):
            continue
        out[run_id] = doc
    return out


def list_runs(ticker=None, limit=100):
    if ticker is not None:
        ticker = str(ticker).strip().upper()
        if not is_known_ticker(ticker):
            raise ReportHistoryError("Unsupported company ticker.")

    root = project_root()
    db = _db_rows(ticker)
    journal = _journal_rows(root, ticker)

    reports = {}
    skipped = 0
    from app.services import document_store

    for path in document_store.list_paths(reports_directory(root), root=root):
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
    if not is_known_ticker(ticker):
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

    doc = _read_json(path)
    if doc is None:
        raise ReportHistoryError("Research report not found.")

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
        "news_evidence_resolution": news_evidence_resolution(doc),
    }


def resolve_news_evidence(conn, ticker, article):
    """
    Locate a saved report's cited article in the current database.

    Database IDs are not stable across a restore (IDs can be reissued), so an
    ID is trusted only when it still points to the same company and URL;
    otherwise the article is looked up by company and source URL. A citation
    is never resolved to another company's article.
    """
    url = article.get("source_url")
    db_id = article.get("database_id")
    if db_id is not None:
        row = conn.execute(
            "SELECT id, company_ticker, article_url FROM news_articles WHERE id = ?", (int(db_id),)
        ).fetchone()
        if row is not None and row["company_ticker"] == ticker and row["article_url"] == url:
            return {"status": "resolved", "database_id": row["id"]}
    if url:
        row = conn.execute(
            "SELECT id FROM news_articles WHERE company_ticker = ? AND article_url = ? ORDER BY id LIMIT 1",
            (ticker, url),
        ).fetchone()
        if row is not None:
            return {"status": "resolved_by_source_url", "database_id": row["id"]}
    return {"status": "unresolvable", "database_id": None}


def news_evidence_resolution(doc):
    """{evidence_id: resolution} for every news article cited by a saved report."""
    ticker = doc.get("ticker")
    news = doc.get("news") or {}
    articles = {}
    for article in (news.get("selected_articles") or []) + (news.get("approved_primary_articles") or []):
        if article.get("evidence_id"):
            articles[article["evidence_id"]] = article
    if not articles:
        return {}
    try:
        from app.database.sqlite_manager import connect

        with connect() as conn:
            return {eid: resolve_news_evidence(conn, ticker, a) for eid, a in articles.items()}
    except Exception:
        return {eid: {"status": "unknown", "database_id": None} for eid in articles}


def companies_with_reports():
    """Distinct companies (US and India) with at least one completed run whose
    report is saved, from the same merged history as list_runs. Each entry:
    ticker, name, market, saved report count and latest report time."""
    history = list_runs(limit=1_000_000)
    by_ticker = {}
    for run in history["runs"]:
        if not run["report_available"] or not run["ticker"]:
            continue
        entry = by_ticker.setdefault(run["ticker"], {"ticker": run["ticker"], "report_count": 0,
                                                     "latest_report_at_utc": None})
        entry["report_count"] += 1
        stamp = run["report_generated_at_utc"] or run["created_at_utc"]
        if stamp and (entry["latest_report_at_utc"] is None or stamp > entry["latest_report_at_utc"]):
            entry["latest_report_at_utc"] = stamp
    if not by_ticker:
        return []
    tickers = sorted(by_ticker)
    marks = ",".join("?" for _ in tickers)
    with connect() as conn:
        market_rows = {r["symbol"]: dict(r) for r in conn.execute(
            f"SELECT symbol, name, market FROM market_companies WHERE symbol IN ({marks})", tickers).fetchall()}
        names = {r["ticker"]: r["name"] for r in conn.execute(
            f"SELECT ticker, name FROM companies WHERE ticker IN ({marks})", tickers).fetchall()}
    out = []
    for ticker in tickers:
        stored = market_rows.get(ticker) or {}
        market = stored.get("market") or ("IN" if ticker.endswith((".NS", ".BO")) else "US")
        out.append({**by_ticker[ticker], "name": stored.get("name") or names.get(ticker) or ticker,
                    "market": market, "sec_validated": ticker in SUPPORTED_TICKERS})
    return out
