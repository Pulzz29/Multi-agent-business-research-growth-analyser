import hmac
import json
import os
import uuid
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
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


SUPPORTED_COMPANIES = (
    "AAPL",
    "AMZN",
    "GOOGL",
    "META",
    "MSFT",
    "NVDA",
)

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


def project_root():
    return Path(
        get_settings().project_root
    ).resolve()


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        security_scheme
    ),
):
    expected = os.getenv("API_ACCESS_KEY")

    if (
        not expected
        or credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(
            credentials.credentials,
            expected,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        )

    return True


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

            tables = {
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()
            }

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

        if not (
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

    return {
        "status": "ready",
        "service": "insightiq-api",
        "database": "available",
        "research_job_journal": "available",
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
        document = json.loads(
            expected_report.read_text(
                encoding="utf-8"
            )
        )

        if (
            document.get("run_id") != run_id
            or document.get("ticker") != job["ticker"]
            or document.get("status") != "completed"
        ):
            raise ValueError(
                "Stored report identity mismatch."
            )

    except (OSError, ValueError, TypeError):
        raise HTTPException(
            status_code=503,
            detail="Research report unavailable.",
        )

    return document

# ==========================================================
# Background research submission — Colab worker
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


# Colab development executor:
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
        _worker_executor.submit(
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
    SUPPORTED_TICKERS as _insightiq_news_tickers,
)
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

    with _insightiq_news_connect() as conn:

        article = conn.execute(
            """
            SELECT
                COUNT(*) AS article_count,
                MAX(published_at) AS latest_published_at,
                MAX(retrieved_at) AS latest_retrieved_at
            FROM news_articles
            WHERE company_ticker = ?
            """,
            (ticker,),
        ).fetchone()

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
# GET routes are read-only: no Groq, NewsData.io or SEC calls,
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


# Explicit, user-initiated AI action (uses Groq quota).
# Never overwrites an existing completed summary.

from app.services.executive_summary import (
    ExecutiveSummaryError as _ExecutiveSummaryError,
    generate_summary as _generate_summary,
)

_summary_request_lock = threading.Lock()


@app.post(
    "/api/v1/companies/{ticker}/reports/{run_id}/executive-summary",
    dependencies=[Depends(require_api_key)],
)
def create_executive_summary(ticker: str, run_id: str):
    ticker = _require_research_ticker(ticker)
    run_id = validate_job_id(run_id)

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

    if not _summary_request_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Another summary is being generated.",
            headers={"Retry-After": "10"},
        )

    try:
        return _generate_summary(
            project_root(),
            run_id,
            ticker=ticker,
        )

    except _ExecutiveSummaryError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="The summary service is unavailable.",
        )

    finally:
        _summary_request_lock.release()


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
# call the market's data providers and, for comparisons, Groq.
# ==========================================================

import logging as _markets_logging
from typing import List as _MarketsList

from fastapi import Response as _MarketsResponse

from app.services import market_ingestion as _market_ingestion
from app.services import company_data as _company_data
from app.services import market_registry as _market_registry
from app.services import market_storage as _market_storage
from app.services import sector_comparison as _sector_comparison
from app.services import sector_report as _sector_report

_markets_logger = _markets_logging.getLogger("insightiq.api.markets")

try:
    _market_storage.ensure_market_storage()
except Exception:  # storage is created lazily on first write instead
    _markets_logger.warning("Market storage could not be initialised at startup.")


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
    return {"market": market, "query": request.q, "results": results, "source": "provider"}


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
        return {"market": market, "sectors": _market_registry.sectors(market, search)}
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
        )
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
    try:
        content, media_type, extension = _sector_report.render(snapshot, format)
    except Exception:
        _markets_logger.exception("Report rendering failed for %s", comparison_id)
        raise HTTPException(status_code=503, detail="The report could not be generated.")
    filename = (
        f"InsightIQ_{snapshot['market']}_{snapshot['sector_key']}_"
        f"{snapshot['generated_at_utc'][:10]}.{extension}"
    )
    return _MarketsResponse(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
