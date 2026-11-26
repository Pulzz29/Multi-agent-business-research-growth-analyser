"""
Durable report files in Azure Blob Storage (private container, managed identity).

Enabled when AZURE_STORAGE_ACCOUNT_URL is set (otherwise PDFs are built on
request exactly as before, so local use is unchanged).

* Every blob name is unique and immutable: research PDFs are keyed by company,
  research run and executive-summary version; sector files by comparison and
  format. Uploads use "if none match *", so an existing blob is never
  overwritten.
* A file is recorded as saved (``report_files``) only after its bytes were read
  back and the SHA-256 matched. Uploads are retried (bounded); a failure leaves
  a "failed" record plus an error event, and the next download retries.
* Downloads go through the authenticated backend; no public or SAS links and no
  storage credentials reach the frontend.
* PDFs are rendered deterministically from the stored report content (no AI or
  provider calls) with the report's original generation time.
"""

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone

from app.database import db

logger = logging.getLogger("insightiq.report_blob")
MAX_ATTEMPTS = 3
_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()
_container_factory = None  # tests inject a fake container client


def enabled():
    return bool(os.getenv("AZURE_STORAGE_ACCOUNT_URL", "").strip()) or _container_factory is not None


def reports_container():
    return os.getenv("AZURE_STORAGE_REPORTS_CONTAINER", "reports")


def set_container_factory(factory):
    """Test hook: ``factory(container_name)`` returns a container-client-like object."""
    global _container_factory
    _container_factory = factory
    _CLIENTS.clear()


def container_client(name):
    if _container_factory is not None:
        return _container_factory(name)
    with _CLIENT_LOCK:
        if name not in _CLIENTS:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
            service = BlobServiceClient(os.environ["AZURE_STORAGE_ACCOUNT_URL"].strip(), credential=credential)
            _CLIENTS[name] = service.get_container_client(name)
        return _CLIENTS[name]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _row(blob_name):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM report_files WHERE blob_name = ?", (blob_name,)).fetchone()
    return dict(row) if row else None


def _record(meta, status, sha=None, size=None, attempts=0, error=None):
    now = _now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO report_files(blob_name, container, kind, ticker, run_id, comparison_id, summary_version, "
            "file_name, content_type, generated_at_utc, sha256, size_bytes, status, attempts, last_error, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (blob_name) DO UPDATE SET status = EXCLUDED.status, sha256 = EXCLUDED.sha256, "
            "size_bytes = EXCLUDED.size_bytes, attempts = report_files.attempts + EXCLUDED.attempts, "
            "last_error = EXCLUDED.last_error, updated_at = EXCLUDED.updated_at",
            (meta["blob_name"], meta["container"], meta["kind"], meta.get("ticker"), meta.get("run_id"),
             meta.get("comparison_id"), meta.get("summary_version"), meta["file_name"], meta["content_type"],
             meta.get("generated_at_utc"), sha, size, status, attempts, (error or "")[:400] or None, now, now))


def _download(container, blob_name):
    return container.get_blob_client(blob_name).download_blob().readall()


def store(meta, content):
    """Upload immutably, read back and verify. Returns True when durably saved."""
    from app.services import errors

    digest = _sha(content)
    container = container_client(meta["container"])
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            from azure.core.exceptions import ResourceExistsError

            try:
                container.get_blob_client(meta["blob_name"]).upload_blob(
                    content, overwrite=False, metadata={
                        "sha256": digest, "kind": meta["kind"], "ticker": meta.get("ticker") or "",
                        "run_id": meta.get("run_id") or "", "comparison_id": meta.get("comparison_id") or "",
                        "generated_at_utc": meta.get("generated_at_utc") or ""},
                    content_settings=_content_settings(meta["content_type"]))
            except ResourceExistsError:
                pass  # immutable: an existing blob is verified, never replaced
            stored = _download(container, meta["blob_name"])
            if _sha(stored) != digest:
                raise RuntimeError("stored blob checksum does not match the generated file")
            _record(meta, "saved", digest, len(content), attempts=attempt)
            return True
        except Exception as exc:  # bounded retries, then a durable failure record
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(2 ** attempt, 8))
    _record(meta, "failed", digest, len(content), attempts=MAX_ATTEMPTS, error=errors.sanitize(last_error, 400))
    errors.record("report_blob:upload", RuntimeError(last_error), severity="error", component="blob_storage",
                  run_id=meta.get("run_id"), ticker=meta.get("ticker"), retries=MAX_ATTEMPTS, final_status="failed")
    return False


def _content_settings(content_type):
    from azure.storage.blob import ContentSettings

    return ContentSettings(content_type=content_type)


def fetch_or_store(meta, build):
    """Serve the saved blob when it exists and verifies; otherwise build the file,
    store it durably and serve it. ``build()`` returns the file bytes."""
    if not enabled():
        return build()
    row = _row(meta["blob_name"])
    if row and row["status"] == "saved":
        try:
            data = _download(container_client(meta["container"]), meta["blob_name"])
            if _sha(data) == row["sha256"]:
                return data
            logger.warning("report_blob_checksum_mismatch %s", meta["blob_name"])
        except Exception as exc:
            logger.warning("report_blob_download_failed %s %s", meta["blob_name"], type(exc).__name__)
    content = build()
    store(meta, content)  # failure is recorded; the user still receives the generated file
    return content


# ------------------------------------------------------------------ report kinds

def research_meta(ticker, run_id):
    from app.services import report_history
    from app.services.company_report_pdf import company_name, safe_filename

    report = report_history.read_report(ticker, run_id)["report"]
    summary = report_history.read_summary(run_id) or {}
    version = summary.get("generated_at_utc") if summary.get("status") == "completed" else None
    version_key = hashlib.sha256((version or "no-summary").encode()).hexdigest()[:16]
    return {
        "container": reports_container(), "kind": "research_pdf", "ticker": ticker, "run_id": run_id,
        "summary_version": version, "generated_at_utc": report.get("generated_at_utc"),
        "blob_name": f"research/{ticker}/{run_id}/{version_key}.pdf",
        "file_name": safe_filename(company_name(ticker), report.get("generated_at_utc")),
        "content_type": "application/pdf",
    }


def research_pdf(ticker, run_id):
    """(bytes, filename) for a saved research report, from Blob when available."""
    from app.services import company_report_pdf

    meta = research_meta(ticker, run_id)
    content = fetch_or_store(meta, lambda: company_report_pdf.build(ticker, run_id)[0])
    return content, meta["file_name"]


def sector_meta(snapshot, fmt, filename):
    media = {"pdf": "application/pdf",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
    return {
        "container": reports_container(), "kind": f"sector_{fmt}", "ticker": None, "run_id": None,
        "comparison_id": snapshot["comparison_id"], "generated_at_utc": snapshot.get("generated_at_utc"),
        "blob_name": f"sector/{snapshot['comparison_id']}.{fmt}", "file_name": filename, "content_type": media,
    }


def sector_file(snapshot, fmt, filename):
    from app.services import sector_report

    meta = sector_meta(snapshot, fmt, filename)
    return fetch_or_store(meta, lambda: sector_report.render(snapshot, fmt)[0]), meta["content_type"]


# ------------------------------------------------------------------ background saving

def schedule(fn, *args):
    """Save a newly generated report file in the background (never blocks or fails the caller)."""
    if not enabled():
        return None

    def run():
        try:
            fn(*args)
        except Exception as exc:
            logger.warning("report_blob_background_failed %s", type(exc).__name__)

    thread = threading.Thread(target=run, name="report-blob", daemon=True)
    thread.start()
    return thread


def schedule_research(ticker, run_id):
    return schedule(research_pdf, ticker, run_id)


def schedule_sector(comparison_id):
    def save():
        from app.services import sector_comparison

        snapshot = sector_comparison.read_snapshot(comparison_id)
        sector_file(snapshot, "pdf", sector_filename(snapshot, "pdf"))

    return schedule(save)


def sector_filename(snapshot, extension):
    from app.services import sector_comparison

    custom = "_custom" if sector_comparison.snapshot_type(snapshot) == "custom" else ""
    return (f"InsightIQ_{snapshot['market']}_{snapshot['sector_key']}{custom}_"
            f"{str(snapshot['generated_at_utc'])[:10]}.{extension}")


# ------------------------------------------------------------------ generic uploads (backups)

def upload_file(container_name, blob_name, path, metadata=None):
    """Upload a local file immutably and verify it by SHA-256. Returns the digest."""
    data = open(path, "rb").read()
    digest = _sha(data)
    container = container_client(container_name)
    from azure.core.exceptions import ResourceExistsError

    try:
        container.get_blob_client(blob_name).upload_blob(data, overwrite=False,
                                                         metadata={"sha256": digest, **(metadata or {})})
    except ResourceExistsError:
        pass
    if _sha(_download(container, blob_name)) != digest:
        raise RuntimeError("uploaded backup failed checksum verification")
    return digest
