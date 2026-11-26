import hmac
import logging
import json
import os
import uuid
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from app.core.config import get_settings
from app.core.portability import localize_stored_path
from app.database.sqlite_manager import connect
from app.services.research_jobs import (
    ResearchJobError,
    read_job,
)


# Companies on the validated SEC pipeline (live set; see sec_coverage.py).
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_COMPANIES  # noqa: E402

security_scheme = HTTPBearer(
    auto_error=False,
)

app = FastAPI(
    title="InsightIQ Research API",
    version="0.1.0",
    description=(
        "Authenticated research-job and report API."
    ),
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

from app.core.security import HardeningMiddleware as _HardeningMiddleware, install_redaction as _install_redaction

_install_redaction()
app.add_middleware(_HardeningMiddleware)


def _database_error_types():
    from app.database import db

    return (db.DatabaseUnavailable, *db.OperationalError)


@app.exception_handler(Exception)
async def _database_unavailable_handler(request, exc):
    """A database outage is reported as 503 with a plain message; any other
    unexpected error keeps FastAPI's standard 500 response."""
    from fastapi.responses import JSONResponse

    if isinstance(exc, _database_error_types()):
        import logging

        logging.getLogger("insightiq.db").error("request_failed database_unavailable %s",
                                                type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "The database is unavailable. Try again shortly."})
    from app.services import errors as _errors

    _errors.record(f"api:{request.method} {request.url.path}", exc, severity="error")
    return JSONResponse(status_code=500, content={"detail": _errors.FRIENDLY})


def project_root():
    return Path(
        get_settings().project_root
    ).resolve()


SESSION_COOKIE = "insightiq_session"
CSRF_COOKIE = "insightiq_csrf"
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(
        security_scheme
    ),
):
    """Authentication for every private route.

    Users: a valid server-side session (HttpOnly cookie from /auth/login);
    state-changing requests must also send the CSRF token (X-CSRF-Token).
    Server-to-server (scripts, health checks, automated tests): the
    API_ACCESS_KEY Bearer token, which the web UI no longer uses.
    """
    expected = os.getenv("API_ACCESS_KEY")

    if (
        expected
        and credentials is not None
        and credentials.scheme.lower() == "bearer"
        and hmac.compare_digest(credentials.credentials, expected)
    ):
        return True

    token = request.cookies.get(SESSION_COOKIE)
    if token:
        from app.services import auth as _auth

        session = _auth.validate_session(token)
        if session is not None:
            if request.method in UNSAFE_METHODS and not _auth.csrf_matches(
                session, request.headers.get("X-CSRF-Token")
            ):
                raise HTTPException(status_code=403, detail="Security token missing or invalid. Reload the page.")
            request.state.user = {"id": session["id"], "email": session["email"]}
            return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={
            "WWW-Authenticate": "Bearer"
        },
    )


def validate_job_id(run_id):
    try:
        canonical = str(
            uuid.UUID(run_id)
        )
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            status_code=422,
            detail="Invalid research run ID.",
        )

    if run_id != canonical:
        raise HTTPException(
            status_code=422,
            detail="Invalid research run ID.",
        )

    return canonical


def get_persisted_job(run_id):
    run_id = validate_job_id(run_id)

    try:
        return read_job(run_id)
    except ResearchJobError:
        raise HTTPException(
            status_code=404,
            detail="Research job not found.",
        )
    except Exception:
        # Avoid exposing SQLite paths or configuration.
        raise HTTPException(
            status_code=503,
            detail="Research storage unavailable.",
        )


def public_job_record(job):
    return {
        "run_id": job["run_id"],
        "ticker": job["ticker"],
        "status": job["status"],
        "created_at_utc": job["created_at_utc"],
        "updated_at_utc": job["updated_at_utc"],
        "started_at_utc": job["started_at_utc"],
        "finished_at_utc": job["finished_at_utc"],
        "agent_statuses": job["agent_statuses"],
        "report_available": (
            job["status"] == "completed"
            and bool(job["report_path"])
        ),
        "error_code": job["error_code"],
    }


@app.get("/health/live")
def health_live():
    return {
        "status": "alive",
        "service": "insightiq-api",
    }


@app.get("/health/ready")
def health_ready():
    try:
        with connect() as conn:
            conn.execute(
                "SELECT 1"
            ).fetchone()

            from app.database.db import table_names

            tables = set(table_names(conn))

        required_tables = {
            "sec_facts",
            "news_articles",
            "research_execution_runs",
        }

        if not required_tables.issubset(tables):
            raise RuntimeError(
                "Required database tables unavailable."
            )

        root = project_root()

        if not root.is_dir():
            raise RuntimeError(
                "Project storage unavailable."
            )

        from app.database import db as _journal_db

        # SQLite: the journal folder must exist. PostgreSQL: journals are
        # rows in stored_documents.
        if not _journal_db.is_postgres() and not (
            root / "data" / "research_job_journal"
        ).is_dir():
            raise RuntimeError(
                "Drive journal unavailable."
            )

        if not os.getenv("API_ACCESS_KEY"):
            raise RuntimeError(
                "API credential unavailable."
            )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Research service not ready.",
        )

    # Configuration only (no AI request): which model is configured.
    try:
        from app.services.ai_provider import ai_runtime_settings

        ai = ai_runtime_settings()
        ai_status = {"provider": "azure_openai", "model": ai["model"], "deployment": ai["deployment"],
                     "api": ai["api"], "configured": True}
    except Exception:
        ai_status = {"provider": "azure_openai", "model": None, "deployment": None, "api": None,
                     "configured": False}
    from app.services.ai_provider import fallback_settings

    fallback = fallback_settings() or {}
    ai_fallback = {"provider": fallback.get("provider"), "model": fallback.get("model"),
                   "configured": bool(fallback.get("configured"))}

    from app.database import db as _db

    return {
        "status": "ready",
        "service": "insightiq-api",
        "database": "available",
        "database_backend": _db.backend(),
        "research_job_journal": "available",
        "ai_provider": ai_status,
        "ai_fallback": ai_fallback,
    }


@app.get(
    "/api/v1/companies",
    dependencies=[Depends(require_api_key)],
)
def list_companies():
    return {
        "supported_tickers": list(
            SUPPORTED_COMPANIES
        ),
        "count": len(SUPPORTED_COMPANIES),
    }


@app.get(
    "/api/v1/jobs/{run_id}",
    dependencies=[Depends(require_api_key)],
)
def get_job(run_id: str):
    job = get_persisted_job(run_id)

    return public_job_record(job)


@app.get(
    "/api/v1/reports/{run_id}",
    dependencies=[Depends(require_api_key)],
)
def get_report(run_id: str):
    job = get_persisted_job(run_id)

    if job["status"] != "completed":
        raise HTTPException(
            status_code=409,
            detail="Research report is not completed.",
        )

    reports_root = (
        project_root()
        / "reports"
        / "research_runs"
    ).resolve()

    # Never trust an arbitrary absolute report path
    # stored in a database row.
    expected_report = (
        reports_root / (run_id + ".json")
    ).resolve()

    stored_path = job.get("report_path")

    if (
        not stored_path
        or not expected_report.is_relative_to(
            reports_root
        )
        or localize_stored_path(stored_path, project_root())
        != expected_report
    ):
        raise HTTPException(
            status_code=503,
            detail="Research report unavailable.",
        )

    try:
        from app.services import document_store

        document = json.loads(
            document_store.read_text(expected_report) or "null"
        )

        if (
            document.get("run_id") != run_id
            or document.get("ticker") != job["ticker"]
            or document.get("status") != "completed"
        ):
            raise ValueError(
                "Stored report identity mismatch."
            )

    except (OSError, ValueError, TypeError, AttributeError):
        raise HTTPException(
            status_code=503,
            detail="Research report unavailable.",
        )

    return document

# ==========================================================
# Background research submission — in-process worker
# ==========================================================

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from functools import partial

from app.services.research_jobs import (
    active_job_for_ticker,
    create_job,
    transition_job,
    execute_queued_research_job,
)

from app.agents.research_workflow import (
    run_research_workflow,
)


class ResearchSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # One of the six SEC-validated companies, or any market company whose
    # data has been loaded (validated against the registry on submission).
    ticker: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9&.\-]{0,19}$",
    )

    # Optional. None = server policy (INSIGHTIQ_AUTO_REFRESH_ON_RESEARCH).
    refresh: Literal["if_stale", "skip"] | None = None

    on_refresh_failure: Literal[
        "continue_with_cached",
        "fail",
    ] = "continue_with_cached"


def resolve_research_options(request):
    refresh = request.refresh

    if refresh is None:
        refresh = (
            "if_stale"
            if get_settings().auto_refresh_on_research
            else "skip"
        )

    return {
        "refresh": refresh,
        "on_refresh_failure": request.on_refresh_failure,
    }


# In-process research executor:
# one running task, up to three accepted tasks.
_WORKER_CAPACITY = 3

_worker_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="insightiq-research",
)

_worker_slots = threading.BoundedSemaphore(
    _WORKER_CAPACITY
)


# Serialises the duplicate-job check with job creation.
_submission_lock = threading.Lock()


def _execute_submitted_job(run_id, options=None):
    try:
        execute_queued_research_job(
            project_root(),
            run_id,
            partial(run_research_workflow, options=options),
        )

    except Exception:
        # The job service records a sanitized failure.
        # Provider exceptions are never returned
        # directly through the API.
        pass

    finally:
        _worker_slots.release()


@app.post(
    "/api/v1/jobs",
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
def submit_research_job(
    request: ResearchSubmission,
):
    options = resolve_research_options(request)

    from app.services.market_registry import research_profile as _research_profile

    ticker = request.ticker.strip().upper()
    if _research_profile(ticker) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Unsupported company. Load the company's data on its market page "
                "before starting research."
            ),
        )
    request = request.model_copy(update={"ticker": ticker})

    with _submission_lock:
        try:
            active = active_job_for_ticker(request.ticker)
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="Research submission unavailable.",
            )

        if active is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Research for this company is already "
                    + active["status"] + "."
                ),
                headers={"X-Active-Run-Id": active["run_id"]},
            )

        if not _worker_slots.acquire(blocking=False):
            raise HTTPException(
                status_code=429,
                detail="Research queue is full.",
                headers={
                    "Retry-After": "10"
                },
            )

        created = None

        try:
            created = create_job(
                project_root(),
                request.ticker,
            )
        except Exception:
            _worker_slots.release()
            raise HTTPException(
                status_code=503,
                detail="Research submission unavailable.",
            )

    try:
        import contextvars as _contextvars

        # The research job runs in the submitting request's trace context.
        _worker_executor.submit(
            _contextvars.copy_context().run,
            _execute_submitted_job,
            created["run_id"],
            options,
        )

    except Exception:
        if created is not None:
            try:
                transition_job(
                    project_root(),
                    created["run_id"],
                    "failed",
                    error_type="SubmissionFailure",
                    error_code="job_submission_failed",
                )
            except Exception:
                pass

        _worker_slots.release()

        raise HTTPException(
            status_code=503,
            detail="Research submission unavailable.",
        )

    return {
        "run_id": created["run_id"],
        "ticker": created["ticker"],
        "status": "queued",
        "options": options,
        "accepted": True,
        "job_url": (
            "/api/v1/jobs/"
            + created["run_id"]
        ),
        "report_url": (
            "/api/v1/reports/"
            + created["run_id"]
        ),
    }

@app.post(
    "/api/v1/jobs/{run_id}/cancel",
    dependencies=[Depends(require_api_key)],
)
def cancel_research_job(run_id: str):
    """Cancel a queued job, or stop a running job (in-flight AI calls are cancelled)."""
    run_id = validate_job_id(run_id)
    from app.services.research_jobs import ResearchJobError as _ResearchJobError
    from app.services.research_jobs import request_cancel as _request_cancel

    try:
        job = _request_cancel(project_root(), run_id)
    except _ResearchJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=404, detail="Research job not found.")
    return {
        "run_id": run_id,
        "status": job.get("status"),
        "error_code": job.get("error_code"),
        "cancel_requested": bool(job.get("cancel_requested")) or job.get("error_code") == "cancelled",
    }


# ==========================================================
# Completed research backup health — read-only
# ==========================================================

from app.services.runtime_startup import (
    read_backup_health,
)


@app.get(
    "/api/v1/system/backup-health",
    dependencies=[Depends(require_api_key)],
)
def backup_health():
    try:
        health = read_backup_health(
            project_root()
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Backup health is unavailable.",
        )

    if not health["recovery_points_complete"]:
        raise HTTPException(
            status_code=503,
            detail="Research backups require attention.",
        )

    return health

# ==========================================================
# Research observability endpoints
# ==========================================================

from fastapi import Query

from app.services.trace_explorer import (
    TraceExplorerError,
    trace_explorer_for_run,
    performance_metrics,
)


@app.get(
    "/api/v1/observability/runs/{run_id}/traces",
    dependencies=[Depends(require_api_key)],
)
def get_research_traces(
    run_id: str,
    limit: int = Query(default=100, ge=1, le=200),
):
    try:
        return trace_explorer_for_run(
            run_id,
            limit=limit,
        )

    except TraceExplorerError:
        raise HTTPException(
            status_code=422,
            detail="Invalid research trace request.",
        )

    except LookupError:
        raise HTTPException(
            status_code=404,
            detail="Research run not found.",
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Research trace service unavailable.",
        )


@app.get(
    "/api/v1/observability/metrics",
    dependencies=[Depends(require_api_key)],
)
def get_research_metrics(
    sample_limit: int = Query(
        default=1000,
        ge=1,
        le=5000,
    ),
):
    try:
        return performance_metrics(
            sample_limit=sample_limit,
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Research metrics unavailable.",
        )

# ==========================================================
# Research reliability monitoring endpoint
# ==========================================================

from fastapi import Query as ReliabilityQuery

from app.services.reliability_monitoring import (
    current_reliability,
)


@app.get(
    "/api/v1/observability/reliability",
    dependencies=[Depends(require_api_key)],
)
def get_reliability_monitoring(
    sample_limit: int = ReliabilityQuery(
        default=1000,
        ge=1,
        le=5000,
    ),
):
    try:
        return current_reliability(
            sample_limit=sample_limit,
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Reliability monitoring unavailable.",
        )

# ==========================================================
# Dashboard data — authenticated and read-only
# ==========================================================

from fastapi import Query as DashboardQuery

from app.services.dashboard_data import (
    dashboard_snapshot,
)


@app.get(
    "/api/v1/dashboard/overview",
    dependencies=[Depends(require_api_key)],
)
def dashboard_overview(
    recent_limit: int = DashboardQuery(
        default=10,
        ge=1,
        le=25,
    ),
):
    try:
        return dashboard_snapshot(
            project_root(),
            recent_limit=recent_limit,
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Dashboard data unavailable.",
        )

# INSIGHTIQ_NEWS_REFRESH_API_START

import asyncio as _insightiq_news_asyncio
from datetime import datetime as _insightiq_news_datetime
from datetime import timezone as _insightiq_news_timezone

from app.services.news_ingestion import (
    connect as _insightiq_news_connect,
)
from app.services.sec_coverage import SEC_VALIDATED as _insightiq_news_tickers
from app.services import source_refresh as _source_refresh

_insightiq_news_refresh_lock = (
    _insightiq_news_asyncio.Lock()
)


def _insightiq_news_status_snapshot(ticker):
    """Return stored coverage without contacting NewsData.io."""

    ticker = str(ticker).strip().upper()

    if ticker not in _insightiq_news_tickers:
        raise HTTPException(
            status_code=422,
            detail="Unsupported company ticker.",
        )

    from app.services.company_profile import news_status as _verified_news_status

    with _insightiq_news_connect() as conn:

        # Counts and dates describe verified company-specific news only;
        # stored rows that fail the relevance rules are reported as hidden.
        article = _verified_news_status(conn, ticker)

        refresh = conn.execute(
            """
            SELECT
                completed_at,
                status
            FROM source_refreshes
            WHERE company_ticker = ?
              AND source = 'NEWSDATA'
              AND status = 'success'
            ORDER BY id DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()

    return {
        "ticker": ticker,
        "source": "NEWSDATA",
        "article_count": article["article_count"],
        "latest_published_at": (
            article["latest_published_at"]
        ),
        "latest_retrieved_at": (
            article["latest_retrieved_at"]
        ),
        "last_successful_refresh_at": (
            refresh["completed_at"]
            if refresh else None
        ),
        "stored_article_count": article["stored_article_count"],
        "hidden_article_count": article["hidden_article_count"],
        "hidden_by_reason": article["hidden_by_reason"],
        "relevance_version": article["relevance_version"],
    }


@app.get(
    "/api/v1/news/status/{ticker}",
    dependencies=[Depends(require_api_key)],
)
def insightiq_company_news_status(ticker: str):
    """Read company news coverage without external API calls."""

    try:
        return _insightiq_news_status_snapshot(ticker)

    except HTTPException:
        raise

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="News status is temporarily unavailable.",
        )


@app.post(
    "/api/v1/news/refresh",
    dependencies=[Depends(require_api_key)],
)
async def insightiq_refresh_company_news(
    request: dict,
):
    """Fetch fresh news using the existing ingestion pipeline."""

    ticker = str(
        request.get("ticker", "")
    ).strip().upper()

    if ticker not in _insightiq_news_tickers:
        raise HTTPException(
            status_code=422,
            detail="Select a supported company ticker.",
        )

    # One refresh at a time within this app process.
    # Protects the shared API allowance against rapid clicks.

    if _insightiq_news_refresh_lock.locked():
        raise HTTPException(
            status_code=429,
            detail="Another news refresh is running.",
            headers={"Retry-After": "10"},
        )

    async with _insightiq_news_refresh_lock:

        try:
            before = _insightiq_news_status_snapshot(
                ticker
            )

            previous = before[
                "last_successful_refresh_at"
            ]

            # Manual refreshes can bypass the long-lived
            # ingestion cache, but not this brief cooldown.
            if previous:
                previous_dt = (
                    _insightiq_news_datetime.fromisoformat(
                        previous.replace("Z", "+00:00")
                    )
                )

                if previous_dt.tzinfo is None:
                    previous_dt = previous_dt.replace(
                        tzinfo=_insightiq_news_timezone.utc
                    )

                seconds_since = (
                    _insightiq_news_datetime.now(
                        _insightiq_news_timezone.utc
                    )
                    - previous_dt
                ).total_seconds()

                if 0 <= seconds_since < 600:
                    return {
                        **before,
                        "status": "recently_refreshed",
                        "records_fetched": 0,
                        "records_inserted": 0,
                        "message": (
                            "News was refreshed recently. "
                            "Existing coverage was reused."
                        ),
                    }

            # Bound the request to protect provider quota.
            # Existing ingestion handles relevance,
            # validation, deduplication, and persistence.
            # source_refresh shares its per-company lock with
            # research jobs, so they never refresh concurrently.
            result = await _insightiq_news_asyncio.to_thread(
                _source_refresh.refresh_one,
                ticker,
                "news",
                force=True,
            )

            if result["status"] == "busy":
                raise HTTPException(
                    status_code=429,
                    detail="Another news refresh is running.",
                    headers={"Retry-After": "10"},
                )

            if result["status"] == "failed":
                raise RuntimeError("news refresh failed")

            after = _insightiq_news_status_snapshot(
                ticker
            )

            return {
                **after,
                "status": (
                    "success"
                    if result["status"] == "refreshed"
                    else result["status"]
                ),
                "records_fetched": result.get(
                    "records_fetched",
                    0,
                ),
                "records_inserted": result.get(
                    "records_inserted",
                    0,
                ),
                "message": (
                    "Company news refresh completed."
                ),
            }

        except HTTPException:
            raise

        except Exception:
            raise HTTPException(
                status_code=502,
                detail=(
                    "News refresh could not complete. "
                    "Please retry later."
                ),
            )

# INSIGHTIQ_NEWS_REFRESH_API_END

# ==========================================================
# Company intelligence dashboard — authenticated
# GET routes are read-only: no AI, NewsData.io or SEC calls,
# no forecast training and no database writes.
# ==========================================================

from fastapi import Query as CompanyQuery

from app.services import company_profile as _company_profile
from app.services import report_history as _report_history


def _require_research_ticker(ticker):
    from app.services.market_registry import is_research_ticker

    ticker = str(ticker or "").strip().upper()

    if not is_research_ticker(ticker):
        raise HTTPException(
            status_code=404,
            detail="Unsupported company ticker.",
        )

    return ticker


def _require_supported_ticker(ticker):
    ticker = str(ticker or "").strip().upper()

    if ticker not in SUPPORTED_COMPANIES:
        raise HTTPException(
            status_code=404,
            detail="Unsupported company ticker.",
        )

    return ticker


@app.get(
    "/api/v1/companies/summary",
    dependencies=[Depends(require_api_key)],
)
def companies_summary():
    try:
        rows = []

        for ticker in SUPPORTED_COMPANIES:
            rows.append(
                _company_profile.company_summary_row(
                    ticker,
                    latest_report=_report_history.latest_completed(ticker),
                )
            )

        return {
            "companies": rows,
            "count": len(rows),
        }

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Company summary unavailable.",
        )


@app.get(
    "/api/v1/companies/{ticker}/overview",
    dependencies=[Depends(require_api_key)],
)
def company_overview(ticker: str):
    ticker = _require_supported_ticker(ticker)

    try:
        return _company_profile.company_overview(
            ticker,
            latest_report=_report_history.latest_completed(ticker),
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Company data unavailable.",
        )


@app.get(
    "/api/v1/companies/{ticker}/growth",
    dependencies=[Depends(require_api_key)],
)
def company_growth_analysis(ticker: str):
    """Growth analysis from stored data (SEC filings or provider statements).
    Read-only: no provider or AI calls."""
    from app.services import growth_analysis as _growth_analysis

    ticker = _require_research_ticker(ticker)
    try:
        result, _ = _growth_analysis.company_growth(ticker)
        return {"ticker": ticker, "growth": result}
    except Exception:
        logging.getLogger("insightiq.growth").exception("Growth analysis failed for %s", ticker)
        raise HTTPException(status_code=503, detail="Growth analysis unavailable.")


@app.get(
    "/api/v1/research/runs",
    dependencies=[Depends(require_api_key)],
)
def research_runs(
    ticker: str | None = CompanyQuery(default=None),
    limit: int = CompanyQuery(default=100, ge=1, le=500),
):
    if ticker is not None:
        ticker = _require_research_ticker(ticker)

    try:
        return _report_history.list_runs(
            ticker,
            limit=limit,
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Research history unavailable.",
        )


@app.get(
    "/api/v1/companies/{ticker}/reports/{run_id}",
    dependencies=[Depends(require_api_key)],
)
def company_report(ticker: str, run_id: str):
    ticker = _require_research_ticker(ticker)
    run_id = validate_job_id(run_id)

    try:
        return _report_history.read_report(ticker, run_id)

    except _report_history.ReportHistoryError:
        raise HTTPException(
            status_code=404,
            detail="Research report not found.",
        )

    except _report_history.ReportUnavailable:
        raise HTTPException(
            status_code=409,
            detail="Research report is not available.",
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Research report unavailable.",
        )


# Explicit, user-initiated AI action (uses Azure OpenAI tokens).
# Never overwrites an existing completed summary.

from app.services.executive_summary import (
    ExecutiveSummaryError as _ExecutiveSummaryError,
    brief_state as _brief_state,
    start_brief as _start_brief,
)


class SummaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The button forces a regeneration even when a current version exists.
    force: bool = False


def _summary_state(ticker, run_id):
    return _brief_state(project_root(), run_id, ticker)


def _require_saved_report(ticker, run_id):
    try:
        _report_history.read_report(ticker, run_id)
    except _report_history.ReportHistoryError:
        raise HTTPException(
            status_code=404,
            detail="Research report not found.",
        )
    except Exception:
        raise HTTPException(
            status_code=409,
            detail="Research report is not available.",
        )


@app.get(
    "/api/v1/companies/{ticker}/reports/{run_id}/insights",
    dependencies=[Depends(require_api_key)],
)
def report_insights(ticker: str, run_id: str):
    """Chart datasets, growth analysis, ratios vs sector median and news
    sources for one saved report, frozen point-in-time on first request.
    Read-only with respect to providers and AI: no external calls."""
    from app.services import report_insights as _report_insights

    ticker = _require_research_ticker(ticker)
    run_id = validate_job_id(run_id)
    _require_saved_report(ticker, run_id)
    try:
        return _report_insights.get_insights(ticker, run_id)
    except Exception:
        logging.getLogger("insightiq.report_insights").exception(
            "Report insights failed for %s %s", ticker, run_id)
        raise HTTPException(status_code=503, detail="Report charts are unavailable.")


@app.get(
    "/api/v1/companies/{ticker}/reports/{run_id}/pdf",
    dependencies=[Depends(require_api_key)],
)
def download_report_pdf(ticker: str, run_id: str):
    """The research report as a PDF: AI summary and research (when generated),
    charts drawn server-side from the frozen dataset, growth and ratio tables,
    news sources and disclaimer. No provider or AI calls."""
    from fastapi.responses import Response as _PdfResponse

    from app.services import company_report_pdf as _company_report_pdf

    ticker = _require_research_ticker(ticker)
    run_id = validate_job_id(run_id)
    _require_saved_report(ticker, run_id)
    try:
        from app.services import report_blob as _report_blob

        content, filename = _report_blob.research_pdf(ticker, run_id)
    except Exception:
        logging.getLogger("insightiq.report_pdf").exception("Report PDF failed for %s %s", ticker, run_id)
        raise HTTPException(status_code=503, detail="The report PDF could not be generated.")
    return _PdfResponse(content=content, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get(
    "/api/v1/companies/{ticker}/reports/{run_id}/executive-summary",
    dependencies=[Depends(require_api_key)],
)
def executive_summary_status(ticker: str, run_id: str):
    """Read-only: whether a summary exists, is being generated, or failed."""
    ticker = _require_research_ticker(ticker)
    run_id = validate_job_id(run_id)
    _require_saved_report(ticker, run_id)
    return _summary_state(ticker, run_id)


@app.post(
    "/api/v1/companies/{ticker}/reports/{run_id}/executive-summary",
    dependencies=[Depends(require_api_key)],
    status_code=202,
)
def create_executive_summary(ticker: str, run_id: str, request: SummaryRequest | None = None):
    """
    Start the AI summary and thorough research in the background (one Azure
    OpenAI call that can take minutes). Returns 202 with the job state; poll the
    GET route. Not forced: a brief built from this report's current data is
    returned as-is (no AI call). A request while one is running reuses it.
    """
    ticker = _require_research_ticker(ticker)
    run_id = validate_job_id(run_id)
    _require_saved_report(ticker, run_id)
    force = bool(request and request.force)
    try:
        outcome, _ = _start_brief(project_root(), run_id, ticker, force=force,
                                  trigger="button" if force else "auto")
    except _ExecutiveSummaryError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        logging.getLogger("insightiq.executive_summary").exception("Could not start brief for %s", run_id)
        raise HTTPException(status_code=503, detail="The summary service is unavailable.")
    return {**_summary_state(ticker, run_id), "request_outcome": outcome}


# ==========================================================
# Source freshness and data refresh — authenticated
# GET is read-only (no provider calls). POST calls SEC EDGAR
# and/or NewsData.io through the existing ingestion pipelines.
# ==========================================================

from typing import List as _List

from pydantic import Field as _Field


class SourceRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: _List[Literal["sec", "news"]] = _Field(
        default_factory=lambda: ["sec", "news"],
        min_length=1,
        max_length=2,
    )

    # False: refresh only sources older than the freshness policy.
    # True: refresh now (subject to a 10-minute cooldown per source).
    force: bool = False


@app.get(
    "/api/v1/companies/{ticker}/freshness",
    dependencies=[Depends(require_api_key)],
)
def company_freshness(ticker: str):
    ticker = _require_supported_ticker(ticker)

    try:
        return _source_refresh.freshness_snapshot(ticker)

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Source freshness is temporarily unavailable.",
        )


@app.post(
    "/api/v1/companies/{ticker}/refresh",
    dependencies=[Depends(require_api_key)],
)
async def refresh_company_sources(
    ticker: str,
    request: SourceRefreshRequest,
):
    ticker = _require_supported_ticker(ticker)

    try:
        outcome = await _insightiq_news_asyncio.to_thread(
            _source_refresh.refresh_sources,
            ticker,
            tuple(dict.fromkeys(request.sources)),
            request.force,
            0.0,
            _source_refresh.MANUAL_COOLDOWN_SECONDS if request.force else None,
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Source refresh is temporarily unavailable.",
        )

    statuses = {r["status"] for r in outcome["results"].values()}

    if statuses == {"busy"}:
        raise HTTPException(
            status_code=429,
            detail="A refresh of this data is already running.",
            headers={"Retry-After": "10"},
        )

    return outcome



# ==========================================================
# Markets, company search and sector comparison — authenticated
# GET routes are read-only (stored data only). POST routes may
# call the market's data providers and, for comparisons, Azure OpenAI.
# ==========================================================

import logging as _markets_logging
from typing import Annotated as _MarketsAnnotated
from typing import List as _MarketsList

from fastapi import Response as _MarketsResponse

from app.services import market_ingestion as _market_ingestion
from app.services import company_data as _company_data
from app.services import market_registry as _market_registry
from app.services import market_storage as _market_storage
from app.services import sector_comparison as _sector_comparison
from app.services import sector_report as _sector_report

_markets_logger = _markets_logging.getLogger("insightiq.api.markets")

@app.on_event("startup")
def _initialise_market_storage():
    # Runs when the server starts, not at import time, so importing this
    # module (for example in tests) never touches a database.
    try:
        _market_storage.ensure_market_storage()
    except Exception:  # storage is created lazily on first write instead
        _markets_logger.warning("Market storage could not be initialised at startup.")
    # Expired company-cache rows are deleted periodically (any instance may run it).
    from app.services import cache_cleanup

    cache_cleanup.start()
    # Correlated tracing of model calls, agents, calculations, forecasts and PDFs.
    from app.services import telemetry

    telemetry.install()
    telemetry.configure_exporters()  # optional Azure Monitor; off unless configured
    # Scheduled pg_dump backups (+ Blob upload); off unless BACKUP_INTERVAL_HOURS > 0.
    from app.services import backup_scheduler

    backup_scheduler.start()


@app.on_event("shutdown")
def _stop_background_database_work():
    from app.database import db
    from app.services import cache_cleanup

    cache_cleanup.stop()
    db.close_pools()


_MarketsSymbol = _MarketsAnnotated[str, _Field(min_length=1, max_length=24)]


class MarketSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str = _Field(min_length=1, max_length=60)


class CompanyRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: _MarketsList[Literal["profile", "quote", "statements", "news"]] = _Field(
        default_factory=lambda: ["profile", "quote", "statements", "news"],
        min_length=1,
        max_length=4,
    )
    force: bool = False
    # Display name from a provider search result, for companies not yet stored.
    name: str | None = _Field(default=None, max_length=120)


class SectorComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh: bool = True
    # Custom comparison: symbols from this sector to compare (absent = whole
    # sector). Membership, de-duplication and the min/max count are checked by
    # sector_comparison.validate_selection (HTTP 400 with a clear reason).
    companies: _MarketsList[_MarketsSymbol] | None = _Field(default=None, max_length=20)


def _market_or_404(market):
    try:
        return _market_registry.require_market(market)
    except _market_registry.UnknownMarket:
        raise HTTPException(status_code=404, detail="Unknown market. Use US or IN.")


def _symbol_or_422(symbol):
    try:
        return _market_registry.require_symbol(symbol)
    except _market_registry.RegistryError:
        raise HTTPException(status_code=422, detail="Invalid company symbol.")


def _sector_or_404(market, sector):
    try:
        key, _ = _market_registry.sector_config(market, sector)
        return key
    except _market_registry.UnknownSector:
        raise HTTPException(status_code=404, detail="Unknown sector for this market.")


@app.get("/api/v1/markets", dependencies=[Depends(require_api_key)])
def list_markets():
    return {"markets": _market_registry.markets()}


@app.get("/api/v1/markets/{market}/top-companies", dependencies=[Depends(require_api_key)])
def market_top_companies(market: str, limit: int = CompanyQuery(default=5, ge=1, le=20)):
    market = _market_or_404(market)
    try:
        return _market_registry.top_companies(market, limit)
    except Exception:
        raise HTTPException(status_code=503, detail="Market data unavailable.")


@app.get("/api/v1/markets/{market}/companies", dependencies=[Depends(require_api_key)])
def market_company_search(market: str, search: str = CompanyQuery(default="", max_length=60)):
    market = _market_or_404(market)
    try:
        return {"market": market, "query": search, "results": _market_registry.search_local(market, search)}
    except Exception:
        raise HTTPException(status_code=503, detail="Company search unavailable.")


@app.post("/api/v1/markets/{market}/companies/search", dependencies=[Depends(require_api_key)])
async def market_provider_search(market: str, request: MarketSearchRequest):
    """Live symbol search at the market's provider (explicit user action)."""
    market = _market_or_404(market)
    from app.services.market_providers.common import ProviderError

    from app.services import company_cache

    key = company_cache.search_key(market, request.q)
    cached = company_cache.get_valid(company_cache.SEARCH, key)
    if cached is not None:
        return {"market": market, "query": request.q, "results": cached["payload"]["results"],
                "source": "cache", "cached_at": cached["fetched_at"].isoformat(),
                "expires_at": cached["expires_at"].isoformat()}

    def run():
        clients, missing = _market_ingestion.default_clients(market)
        if market == "US":
            client = clients.get("finnhub") or clients.get("fmp")
            if client is None:
                raise ProviderError(next(iter(missing.values()), "No US search provider is configured."))
        else:
            client = clients["yfinance"]
        return client.search(request.q.strip())

    try:
        results = await _insightiq_news_asyncio.to_thread(run)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.safe_message)
    except Exception:
        raise HTTPException(status_code=502, detail="Provider search is unavailable.")
    if results:
        # Only a successful, non-empty result is cached (24 hours).
        try:
            company_cache.store(company_cache.SEARCH, key, market, None, {"results": results})
        except Exception:
            _markets_logger.warning("Search result could not be cached.")
    return {"market": market, "query": request.q, "results": results, "source": "provider"}


@app.get("/api/v1/markets/{market}/overview", dependencies=[Depends(require_api_key)])
def market_overview_endpoint(market: str):
    """Home Overview for one market from stored data (no provider calls)."""
    market = _market_or_404(market)
    from app.services import market_overview

    try:
        return market_overview.overview(market)
    except Exception:
        _markets_logger.exception("Market overview failed for %s", market)
        raise HTTPException(status_code=503, detail="Market overview unavailable.")


def _stored_company_or_404(market, symbol):
    summary = _market_registry.company_summary(market, symbol)
    if summary is None or not summary.get("registered"):
        raise HTTPException(status_code=404, detail="Company data not loaded yet. Open the company to load it.")
    return summary


@app.get("/api/v1/markets/{market}/companies/{symbol}/fundamentals", dependencies=[Depends(require_api_key)])
def market_company_fundamentals(market: str, symbol: str):
    """Deterministic fundamentals from stored statements and the latest quote."""
    market = _market_or_404(market)
    symbol = _symbol_or_422(symbol)
    _stored_company_or_404(market, symbol)
    from app.services import fundamentals

    return fundamentals.compute(market, symbol)


@app.get("/api/v1/markets/{market}/companies/{symbol}/financials", dependencies=[Depends(require_api_key)])
def market_company_financials(market: str, symbol: str):
    market = _market_or_404(market)
    symbol = _symbol_or_422(symbol)
    _stored_company_or_404(market, symbol)
    from app.services import fundamentals

    return fundamentals.financial_series(market, symbol)


@app.get("/api/v1/markets/{market}/companies/{symbol}/forecasts", dependencies=[Depends(require_api_key)])
def market_company_forecasts(market: str, symbol: str):
    """Forecast qualification checks with the genuine historical series."""
    market = _market_or_404(market)
    symbol = _symbol_or_422(symbol)
    _stored_company_or_404(market, symbol)
    from app.services import market_forecasts

    return market_forecasts.qualify(market, symbol)


@app.get("/api/v1/logos", dependencies=[Depends(require_api_key)])
def company_logos(symbols: str = CompanyQuery(default="", max_length=2000)):
    """Logo candidates for up to 100 symbols, from each company's own stored profile."""
    from app.services import logos

    requested = [s for s in symbols.split(",") if s.strip()]
    try:
        return {"logos": logos.resolve_many(requested)}
    except Exception:
        raise HTTPException(status_code=503, detail="Logo metadata unavailable.")


@app.get("/api/v1/markets/{market}/companies/{symbol}", dependencies=[Depends(require_api_key)])
def market_company_profile(market: str, symbol: str):
    """Stored company data only (read-only; no provider calls)."""
    market = _market_or_404(market)
    symbol = _symbol_or_422(symbol)
    try:
        payload = _company_data.build_payload(market, symbol)
    except Exception:
        raise HTTPException(status_code=503, detail="Company data unavailable.")
    if payload is None:
        raise HTTPException(status_code=404, detail="Company not found. Search providers to add it.")
    return payload


class CompanyDataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # auto: refresh only when never refreshed or older than the TTL.
    # force: refresh now (manual button), subject to a short cooldown.
    mode: Literal["auto", "force"] = "auto"
    # Display name from a provider search result, for companies not yet stored.
    name: str | None = _Field(default=None, max_length=120)


@app.post("/api/v1/markets/{market}/companies/{symbol}/data", dependencies=[Depends(require_api_key)])
async def market_company_data(market: str, symbol: str, request: CompanyDataRequest):
    """Company data with the server-side freshness rules (may call providers)."""
    market = _market_or_404(market)
    symbol = _symbol_or_422(symbol)
    try:
        return await _insightiq_news_asyncio.to_thread(
            _company_data.get_company_data, market, symbol, request.mode, request.name,
        )
    except _company_data.UnknownCompany as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except _company_data.NoCompanyData as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception:
        _markets_logger.exception("Company data request failed for %s", symbol)
        raise HTTPException(status_code=503, detail="Company data is temporarily unavailable.")


@app.post("/api/v1/markets/{market}/companies/{symbol}/refresh", dependencies=[Depends(require_api_key)])
async def market_company_refresh(market: str, symbol: str, request: CompanyRefreshRequest):
    market = _market_or_404(market)
    symbol = _symbol_or_422(symbol)
    try:
        outcome = await _insightiq_news_asyncio.to_thread(
            _market_ingestion.refresh_company, market, symbol,
            tuple(dict.fromkeys(request.sources)), request.force, None, request.name,
        )
    except _market_registry.RegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        _markets_logger.warning("Company refresh failed for %s", symbol)
        raise HTTPException(status_code=503, detail="Company refresh is temporarily unavailable.")
    statuses = {r.get("status") for r in outcome["results"].values()}
    if statuses == {"busy"}:
        raise HTTPException(status_code=429, detail="A refresh for this company is already running.",
                            headers={"Retry-After": "10"})
    return outcome


@app.post("/api/v1/markets/{market}/quotes/refresh", dependencies=[Depends(require_api_key)])
async def market_quotes_refresh(market: str):
    """Load the latest quotes for the market's universe (needed for the top-5 list)."""
    market = _market_or_404(market)
    symbols = [
        c["symbol"]
        for sector in _market_registry.market_config(market)["sectors"].values()
        for c in sector["companies"]
    ]
    try:
        outcome = await _insightiq_news_asyncio.to_thread(
            _market_ingestion.refresh_many, market, symbols, ("quote",), False, None, len(symbols),
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Market data refresh is temporarily unavailable.")
    counts = {}
    for results in outcome["outcomes"].values():
        status = (results.get("quote") or {}).get("status", "error")
        counts[status] = counts.get(status, 0) + 1
    return {"market": market, "status_counts": counts, "top": _market_registry.top_companies(market, 5)}


@app.get("/api/v1/markets/{market}/sectors", dependencies=[Depends(require_api_key)])
def market_sectors(market: str, search: str = CompanyQuery(default="", max_length=60)):
    market = _market_or_404(market)
    try:
        return {
            "market": market,
            "sectors": _market_registry.sectors(market, search),
            # Selection limits for "Compare selected companies" (single source: settings).
            "custom_comparison": _sector_comparison.custom_limits(),
        }
    except Exception:
        raise HTTPException(status_code=503, detail="Sector list unavailable.")


@app.get("/api/v1/markets/{market}/sectors/{sector}/comparison", dependencies=[Depends(require_api_key)])
def latest_sector_comparison(market: str, sector: str):
    market = _market_or_404(market)
    key = _sector_or_404(market, sector)
    try:
        return _sector_comparison.latest_snapshot(market, key)
    except _sector_comparison.SnapshotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=503, detail="Sector comparison unavailable.")


@app.get("/api/v1/markets/{market}/sectors/{sector}/comparisons", dependencies=[Depends(require_api_key)])
def sector_comparison_history(market: str, sector: str):
    market = _market_or_404(market)
    key = _sector_or_404(market, sector)
    return {"market": market, "sector_key": key,
            "comparisons": _sector_comparison.list_snapshots(market, key)}


@app.post("/api/v1/markets/{market}/sectors/{sector}/comparison", dependencies=[Depends(require_api_key)])
async def generate_sector_comparison(market: str, sector: str, request: SectorComparisonRequest):
    market = _market_or_404(market)
    key = _sector_or_404(market, sector)
    try:
        return await _insightiq_news_asyncio.to_thread(
            _sector_comparison.build_comparison, market, key, request.refresh,
            None, None, None, request.companies,
        )
    except _sector_comparison.SelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except _sector_comparison.ComparisonBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "15"})
    except _sector_comparison.ComparisonNoData as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        _markets_logger.exception("Sector comparison failed for %s/%s", market, key)
        raise HTTPException(status_code=503, detail="The sector comparison could not be generated.")


@app.get("/api/v1/sector-comparisons/{comparison_id}", dependencies=[Depends(require_api_key)])
def get_sector_comparison(comparison_id: str):
    try:
        return _sector_comparison.read_snapshot(comparison_id)
    except _sector_comparison.SnapshotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/v1/sector-comparisons/{comparison_id}/report", dependencies=[Depends(require_api_key)])
def download_sector_report(
    comparison_id: str,
    format: Literal["pdf", "docx"] = CompanyQuery(default="pdf"),
):
    try:
        snapshot = _sector_comparison.read_snapshot(comparison_id)
    except _sector_comparison.SnapshotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    from app.services import report_blob as _report_blob

    filename = _report_blob.sector_filename(snapshot, format)
    try:
        content, media_type = _report_blob.sector_file(snapshot, format, filename)
    except Exception:
        _markets_logger.exception("Report rendering failed for %s", comparison_id)
        raise HTTPException(status_code=503, detail="The report could not be generated.")
    return _MarketsResponse(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ==========================================================
# Operations: traces, latency, run metrics, tokens, alerts, audit
# ==========================================================

def _operations_unavailable():
    raise HTTPException(status_code=404, detail="Operational telemetry needs the PostgreSQL backend.")


@app.get("/api/v1/operations/overview", dependencies=[Depends(require_api_key)])
def operations_overview(hours: int = Query(default=24, ge=1, le=24 * 30), days: int = Query(default=30, ge=1, le=365)):
    """Measured latency percentiles, research-run outcomes, token usage and open alerts."""
    from app.services import db_backup, telemetry

    if not telemetry.enabled():
        _operations_unavailable()
    telemetry.evaluate_alerts()
    from app.services import errors as _errors
    from app.services import governance as _governance

    return {
        "governance": _governance.summary(days=days),
        "errors": _errors.recent(limit=20),
        "latency": telemetry.latency_summary(hours=hours),
        "runs": telemetry.run_metrics(days=days),
        "tokens": telemetry.token_usage(days=days),
        "alerts": telemetry.alerts(),
        "recent_traces": telemetry.recent_traces(limit=20),
        "last_backup": db_backup.latest_backup_status(),
    }


@app.get("/api/v1/operations/traces/{trace_id}", dependencies=[Depends(require_api_key)])
def operations_trace(trace_id: str):
    from app.services import telemetry

    if not telemetry.enabled():
        _operations_unavailable()
    try:
        detail = telemetry.trace_detail(trace_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid trace id.")
    if not detail["spans"]:
        raise HTTPException(status_code=404, detail="Trace not found.")
    return detail


@app.get("/api/v1/operations/alerts", dependencies=[Depends(require_api_key)])
def operations_alerts(include_resolved: bool = False):
    from app.services import telemetry

    if not telemetry.enabled():
        _operations_unavailable()
    return {"alerts": telemetry.alerts(include_resolved=include_resolved)}


@app.get("/api/v1/operations/audit", dependencies=[Depends(require_api_key)])
def operations_audit(limit: int = Query(default=50, ge=1, le=500)):
    from app.services import telemetry

    if not telemetry.enabled():
        _operations_unavailable()
    return {"events": telemetry.audit_events(limit=limit)}


# ==========================================================
# User authentication (email + password, server-side sessions)
# ==========================================================

from pydantic import BaseModel as _AuthModel, ConfigDict as _AuthConfig, Field as _AuthField

from app.core.security import RateLimiter as _RateLimiter

_LOGIN_LIMITER = _RateLimiter()
_LOGIN_WINDOW_SECONDS = 900.0


class LoginRequest(_AuthModel):
    model_config = _AuthConfig(extra="forbid")

    email: str = _AuthField(min_length=3, max_length=320)
    password: str = _AuthField(min_length=1, max_length=256)


def _cookie_options(request, max_age):
    # Secure when the request arrived over HTTPS (directly, or via trusted
    # proxies' X-Forwarded-Proto) or always with SESSION_COOKIE_SECURE=true
    # (set in production so cookies never depend on proxy header handling).
    forced = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
    return {
        "max_age": max_age,
        "path": "/",
        "samesite": "strict",
        "secure": forced or request.url.scheme == "https",
    }


@app.post("/api/v1/auth/login")
def auth_login(payload: LoginRequest, request: Request, response: Response):
    """Email + password login. One generic error for any wrong email/password."""
    from app.services import auth as _auth

    limit = int(os.getenv("LOGIN_ATTEMPTS_PER_15_MINUTES", "10"))
    client = request.client.host if request.client else "unknown"
    email_key = str(payload.email).strip().lower()[:320]
    for key in (f"login-ip|{client}", f"login-email|{email_key}"):
        allowed, retry = _LOGIN_LIMITER.check(key, limit, window=_LOGIN_WINDOW_SECONDS)
        if not allowed:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.",
                                headers={"Retry-After": str(retry)})

    user = _auth.authenticate(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token, csrf, expires = _auth.create_session(user)
    max_age = int((expires - _auth.utc_now()).total_seconds())
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **_cookie_options(request, max_age))
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **_cookie_options(request, max_age))
    return {"user": {"email": user["email"]}, "csrf_token": csrf, "expires_at": expires.isoformat()}


@app.get("/api/v1/auth/me")
def auth_me(request: Request):
    """The signed-in user (session cookie only)."""
    from app.services import auth as _auth

    session = _auth.validate_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return {"user": {"email": session["email"]}}


@app.post("/api/v1/auth/logout")
def auth_logout(request: Request, response: Response):
    """Revoke the session and clear the cookies (CSRF-checked when signed in)."""
    from app.services import auth as _auth

    token = request.cookies.get(SESSION_COOKIE)
    session = _auth.validate_session(token) if token else None
    if session is not None and not _auth.csrf_matches(session, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status_code=403, detail="Security token missing or invalid. Reload the page.")
    _auth.revoke_session(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"signed_out": True}


@app.get("/api/v1/saved-reports", dependencies=[Depends(require_api_key)])
def saved_reports_catalog():
    """All saved research reports (stored data only; no AI or provider calls)."""
    from app.services import report_history

    try:
        return {"reports": report_history.saved_report_catalog()}
    except Exception:
        raise HTTPException(status_code=503, detail="Saved reports are temporarily unavailable.")


@app.get("/api/v1/research/companies", dependencies=[Depends(require_api_key)])
def research_companies():
    """Companies (US and India) with at least one completed run that has a saved report."""
    from app.services import report_history

    try:
        return {"companies": report_history.companies_with_reports()}
    except Exception:
        raise HTTPException(status_code=503, detail="Research history unavailable.")


# ==========================================================
# Company-research chatbot (signed-in users only)
# ==========================================================

from typing import Literal as _ChatLiteral


class ChatAction(_AuthModel):
    model_config = _AuthConfig(extra="forbid")

    type: _ChatLiteral["confirm_research", "cancel", "select_company"]
    ticker: str | None = _AuthField(default=None, max_length=20, pattern=r"^[A-Za-z0-9][A-Za-z0-9&.\-]{0,19}$")
    nonce: str | None = _AuthField(default=None, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class ChatTurn(_AuthModel):
    model_config = _AuthConfig(extra="forbid")

    # Over-long text is accepted here and rejected politely by the chatbot guard.
    text: str | None = _AuthField(default=None, max_length=4000)
    action: ChatAction | None = None


def _chat_user(request: Request):
    """Chat is per user: a signed-in session is required (the server-to-server
    key has no user and cannot read or write conversations)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=403, detail="Sign in to use the research assistant.")
    return user


def _chat_call(func, *args, **kwargs):
    from app.services import chatbot

    try:
        return func(*args, **kwargs)
    except chatbot.ChatError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    except HTTPException:
        raise
    except Exception:
        logging.getLogger("insightiq.api.chat").error("chat_request_failed", exc_info=True)
        raise HTTPException(status_code=503, detail="The research assistant is temporarily unavailable.")


def _chat_submit_research(ticker):
    """Start research through the same path as POST /api/v1/jobs (same lock,
    duplicate-run check and worker limit). Returns the new or active run."""
    try:
        receipt = submit_research_job(ResearchSubmission(ticker=ticker))
        return {"run_id": receipt["run_id"], "status": receipt["status"], "created": True}
    except HTTPException as exc:
        if exc.status_code == 409 and exc.headers and exc.headers.get("X-Active-Run-Id"):
            return {"run_id": exc.headers["X-Active-Run-Id"], "status": "running", "created": False}
        return {"error": str(exc.detail)}


class ChatScope(_AuthModel):
    model_config = _AuthConfig(extra="forbid")

    ticker: str = _AuthField(max_length=20, pattern=r"^[A-Za-z0-9][A-Za-z0-9&.\-]{0,19}$")
    run_id: str = _AuthField(min_length=36, max_length=36, pattern=r"^[0-9a-fA-F-]{36}$")


class NewChat(_AuthModel):
    model_config = _AuthConfig(extra="forbid")

    # Optional: bind the chat to one saved report's executive summary.
    scope: ChatScope | None = None


@app.get("/api/v1/chat/conversations", dependencies=[Depends(require_api_key)])
def chat_conversations(request: Request, run_id: str | None = Query(default=None, max_length=36)):
    from app.services import chatbot

    user = _chat_user(request)
    return {"conversations": _chat_call(chatbot.list_conversations, user["id"], run_id=run_id)}


@app.post("/api/v1/chat/conversations", dependencies=[Depends(require_api_key)])
def chat_new_conversation(request: Request, payload: NewChat | None = None):
    from app.services import chatbot

    user = _chat_user(request)
    scope = payload.scope.model_dump() if payload and payload.scope else None
    return _chat_call(chatbot.create_conversation, user["id"], scope)


@app.get("/api/v1/chat/conversations/{conversation_id}/messages", dependencies=[Depends(require_api_key)])
def chat_messages(conversation_id: str, request: Request):
    from app.services import chatbot

    user = _chat_user(request)
    return _chat_call(chatbot.get_messages, user["id"], conversation_id)


@app.post("/api/v1/chat/conversations/{conversation_id}/messages", dependencies=[Depends(require_api_key)])
def chat_send(conversation_id: str, payload: ChatTurn, request: Request):
    from app.services import chatbot

    user = _chat_user(request)
    if (payload.text is None) == (payload.action is None):
        raise HTTPException(status_code=422, detail="Send a message or an action.")
    return _chat_call(chatbot.handle_turn, user["id"], conversation_id, text=payload.text,
                      action=payload.action.model_dump() if payload.action else None,
                      submit_research=_chat_submit_research)


# ==========================================================
# Built frontend (single same-origin container). Registered last, so every
# /api and /health route takes precedence. Off unless INSIGHTIQ_STATIC_DIR
# points at a build (the Vite dev server is used locally).
# ==========================================================

def _mount_frontend():
    static_dir = os.getenv("INSIGHTIQ_STATIC_DIR", "").strip()
    if not static_dir or not os.path.isfile(os.path.join(static_dir, "index.html")):
        return False
    from starlette.staticfiles import StaticFiles

    ui_csp = ("default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
              "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    class _Frontend(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Content-Security-Policy"] = ui_csp
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-store")
            return response

    app.mount("/", _Frontend(directory=static_dir, html=True), name="frontend")
    return True


_mount_frontend()

