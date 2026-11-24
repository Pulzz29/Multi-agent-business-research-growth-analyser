"""
Freshness-aware SEC EDGAR and NewsData.io refresh orchestration.

* ``freshness_snapshot`` is strictly read-only (SQLite + forecast cache
  files); it never contacts a provider.
* ``refresh_one`` / ``refresh_sources`` call the existing ingestion
  pipelines (``refresh_sec_company``, ``refresh_news_company``), which
  validate, deduplicate and persist records and log every attempt in
  ``source_refreshes``.

Statuses are reported exactly as they happened:

    refreshed           provider returned data; new rows may or may not exist
    no_new_filings      SEC has no periodic filing newer than stored evidence
    cached_fresh        stored evidence is within the freshness policy; no call
    recently_refreshed  manual refresh within the cooldown; no call
    busy                another refresh of this source is in progress
    failed              the provider call or persistence failed

Policy (configurable, see core/config.py):
    INSIGHTIQ_SEC_CACHE_HOURS   (default 24)  SEC evidence max age
    INSIGHTIQ_NEWS_CACHE_HOURS  (default 6)   news evidence max age
    INSIGHTIQ_REFRESH_RUNNING_STALE_MINUTES (default 15)
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

from app.core.config import get_settings
from app.database.sqlite_manager import connect


# Companies on the validated SEC pipeline (the original six plus every US
# universe company whose SEC filings reconcile); see sec_coverage.py.
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_TICKERS  # noqa: E402
SOURCES = {"sec": "SEC", "news": "NEWSDATA"}
SOURCE_ORDER = ("sec", "news")  # news ingestion needs the SEC company row
MANUAL_COOLDOWN_SECONDS = 600
NEWS_MAX_ARTICLES = 30
NEWS_MAX_PAGES = 2

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

ERROR_MESSAGES = {
    "SecIngestionError": "SEC EDGAR could not be reached or returned unusable data.",
    "NewsIngestionError": "NewsData.io could not be reached or returned unusable data.",
    "ValueError": "The refresh request was invalid.",
}


class SourceRefreshError(RuntimeError):
    """Raised when a required refresh failed and the caller chose to stop."""


def utc_now():
    return datetime.now(timezone.utc)


def _parse(ts):
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _lock_for(ticker, source):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((ticker, source), threading.Lock())


def _require(ticker, source=None):
    ticker = str(ticker or "").strip().upper()
    if ticker not in SUPPORTED_TICKERS:
        raise ValueError("Unsupported company ticker.")
    if source is not None and source not in SOURCES:
        raise ValueError("Unsupported data source.")
    return ticker


def _refresh_rows(conn, ticker, provider):
    def one(status):
        return conn.execute(
            """
            SELECT id, started_at, completed_at, status, records_fetched,
                   records_inserted, error_category
            FROM source_refreshes
            WHERE company_ticker = ? AND source = ? AND status = ?
            ORDER BY id DESC LIMIT 1
            """,
            (ticker, provider, status),
        ).fetchone()

    return one("success"), one("failed"), one("running")


def _source_state(ticker, source, conn, now, policy_hours, stale_minutes):
    provider = SOURCES[source]
    success, failure, running = _refresh_rows(conn, ticker, provider)
    last_success = _parse(success["completed_at"]) if success else None
    age_hours = (
        round((now - last_success).total_seconds() / 3600, 2) if last_success else None
    )

    if last_success is None:
        state = "never"
    elif 0 <= age_hours < policy_hours:
        state = "fresh"
    else:
        state = "stale"

    running_started = _parse(running["started_at"]) if running else None
    in_progress = bool(
        running_started
        and (now - running_started).total_seconds() < stale_minutes * 60
    )

    return {
        "source": provider,
        "policy_max_age_hours": policy_hours,
        "state": state,
        "last_successful_refresh_at": success["completed_at"] if success else None,
        "last_successful_records_inserted": success["records_inserted"] if success else None,
        "age_hours": age_hours,
        "last_failed_refresh_at": failure["completed_at"] if failure else None,
        "last_failure_category": failure["error_category"] if failure else None,
        "last_attempt_failed": bool(
            failure and (not success or failure["id"] > success["id"])
        ),
        "refresh_in_progress": in_progress,
    }


def freshness_snapshot(ticker, now=None):
    """Read-only freshness view for one company. No provider calls."""
    ticker = _require(ticker)
    settings = get_settings()
    now = now or utc_now()
    stale_minutes = settings.refresh_running_stale_minutes

    with connect() as conn:
        sec = _source_state(ticker, "sec", conn, now, settings.sec_cache_hours, stale_minutes)
        news = _source_state(ticker, "news", conn, now, settings.news_cache_hours, stale_minutes)

        facts = conn.execute(
            """
            SELECT COUNT(*) AS n, MAX(id) AS max_id, MAX(filed_at) AS latest_filed_at
            FROM sec_facts WHERE company_ticker = ?
            """,
            (ticker,),
        ).fetchone()
        periodic = conn.execute(
            """
            SELECT accession, form, filed_at FROM sec_facts
            WHERE company_ticker = ? AND form IN ('10-Q', '10-K', '10-Q/A', '10-K/A')
            ORDER BY filed_at DESC, id DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        articles = conn.execute(
            """
            SELECT COUNT(*) AS n, MAX(published_at) AS latest_published_at,
                   MAX(retrieved_at) AS latest_retrieved_at
            FROM news_articles WHERE company_ticker = ?
            """,
            (ticker,),
        ).fetchone()

    sec.update({
        "fact_count": facts["n"],
        "sec_facts_fingerprint": [facts["n"], facts["max_id"]],
        "latest_filed_at": facts["latest_filed_at"],
        "latest_periodic_filing": dict(periodic) if periodic else None,
        "latest_financial_period_end": _latest_period_end(ticker),
        "note": (
            "SEC financial statements are periodic filings, not real-time data. "
            "A successful refresh confirms the latest available filing."
        ),
    })
    news.update({
        "article_count": articles["n"],
        "latest_published_at": articles["latest_published_at"],
        "latest_retrieved_at": articles["latest_retrieved_at"],
    })

    return {
        "ticker": ticker,
        "checked_at_utc": now.isoformat(),
        "sec": sec,
        "news": news,
        "forecasts": _forecast_state(ticker),
    }


def _latest_period_end(ticker):
    try:
        from app.services.company_profile import _normalized

        data = _normalized(ticker)
        return {
            metric: (data.get(metric) or [{}])[-1].get("period_end")
            for metric in ("revenue", "operating_cash_flow")
        }
    except Exception:
        return None


def _forecast_state(ticker):
    try:
        from app.services.company_profile import read_forecast
    except Exception:
        return {}
    out = {}
    for metric in ("revenue", "operating_cash_flow"):
        try:
            item = read_forecast(ticker, metric)
        except Exception:
            item = {"available": False}
        out[metric] = {
            "available": bool(item.get("available")),
            "data_cutoff": item.get("data_cutoff"),
            "cached_at_utc": item.get("cached_at_utc"),
        }
    return out


def _run_ingestion(ticker, source, force):
    if source == "sec":
        from app.services.sec_ingestion import refresh_sec_company

        return asyncio.run(refresh_sec_company(ticker, force_refresh=force))

    from app.services.news_ingestion import refresh_news_company

    return asyncio.run(refresh_news_company(
        ticker,
        max_articles=NEWS_MAX_ARTICLES,
        max_pages=NEWS_MAX_PAGES,
        force_refresh=force,
    ))


def refresh_one(ticker, source, force=False, wait_seconds=0.0, cooldown_seconds=None, runner=None):
    """
    Refresh one source. Never raises for provider failures; returns a
    status dictionary that states exactly what happened.
    """
    ticker = _require(ticker, source)
    runner = runner or _run_ingestion
    lock = _lock_for(ticker, source)
    started = time.perf_counter()
    result = {
        "source": SOURCES[source],
        "status": None,
        "force": bool(force),
        "records_fetched": 0,
        "records_inserted": 0,
        "attempted_at_utc": utc_now().isoformat(),
        "completed_at_utc": None,
        "provider_called": False,
        "error_category": None,
        "message": None,
    }

    acquired = lock.acquire(timeout=wait_seconds) if wait_seconds else lock.acquire(blocking=False)
    if not acquired:
        result.update(status="busy", message="Another refresh of this source is running.")
        return result

    try:
        state = freshness_snapshot(ticker)[source]

        if state["refresh_in_progress"]:
            result.update(
                status="busy",
                message="Another process is refreshing this source.",
            )
            return result

        if force and cooldown_seconds and state["last_successful_refresh_at"]:
            last = _parse(state["last_successful_refresh_at"])
            if last and 0 <= (utc_now() - last).total_seconds() < cooldown_seconds:
                result.update(
                    status="recently_refreshed",
                    message="This source was refreshed recently; stored evidence was reused.",
                )
                return result

        result["provider_called"] = True
        try:
            outcome = runner(ticker, source, force) or {}
        except Exception as exc:  # provider or persistence failure
            category = type(exc).__name__
            result.update(
                status="failed",
                error_category=category,
                message=ERROR_MESSAGES.get(category, "The refresh could not complete."),
            )
            return result

        provider_status = outcome.get("status")
        if provider_status == "cached":
            status = "cached_fresh"
            result["provider_called"] = False
        elif provider_status == "no_new_filings":
            status = "no_new_filings"
        elif provider_status == "success":
            status = "refreshed"
        else:
            status = "failed"
            result["error_category"] = "UnexpectedIngestionResult"

        result.update(
            status=status,
            provider_status=provider_status,
            records_fetched=int(outcome.get("records_fetched") or 0),
            records_inserted=int(outcome.get("records_inserted") or 0),
            completed_at_utc=utc_now().isoformat(),
            coverage=outcome.get("coverage"),
            latest_filing=outcome.get("latest_filing"),
            message={
                "cached_fresh": "Stored evidence is within the freshness policy; no provider call was made.",
                "no_new_filings": "SEC EDGAR has no periodic filing newer than the stored evidence.",
                "refreshed": "Provider data retrieved, validated and stored.",
                "failed": "The refresh returned an unexpected result.",
            }[status],
        )
        return result
    finally:
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        lock.release()


def refresh_sources(ticker, sources=SOURCE_ORDER, force=False, wait_seconds=0.0,
                    cooldown_seconds=None, runner=None):
    ticker = _require(ticker)
    requested = [s for s in SOURCE_ORDER if s in set(sources)]
    if not requested:
        raise ValueError("Select at least one data source.")

    before = freshness_snapshot(ticker)
    results = {
        source: refresh_one(
            ticker, source, force=force, wait_seconds=wait_seconds,
            cooldown_seconds=cooldown_seconds, runner=runner,
        )
        for source in requested
    }
    after = freshness_snapshot(ticker)

    sec_changed = (
        before["sec"]["sec_facts_fingerprint"] != after["sec"]["sec_facts_fingerprint"]
    )
    return {
        "ticker": ticker,
        "results": results,
        "freshness": after,
        "forecast_inputs": {
            "sec_facts_changed": sec_changed,
            "forecast_cache": (
                "will_recompute_on_next_research" if sec_changed else "unchanged"
            ),
        },
    }


EVIDENCE_STATE = {
    "refreshed": "refreshed",
    "no_new_filings": "latest_available_confirmed",
    "cached_fresh": "cached_within_policy",
    "recently_refreshed": "cached_within_policy",
    "busy": "refresh_busy_using_cached",
    "failed": "refresh_failed_using_cached",
}


def prepare_sources_for_research(ticker, mode="if_stale", on_failure="continue_with_cached",
                                 runner=None, wait_seconds=300.0):
    """
    Called by the research workflow before any agent runs.

    mode: "if_stale" refreshes stale or never-refreshed sources;
          "skip" uses stored evidence as-is.
    on_failure: "continue_with_cached" labels the evidence;
                "fail" raises SourceRefreshError.
    """
    ticker = _require(ticker)
    if mode not in ("if_stale", "skip"):
        raise ValueError("Invalid refresh mode.")
    if on_failure not in ("continue_with_cached", "fail"):
        raise ValueError("Invalid refresh failure policy.")

    before = freshness_snapshot(ticker)
    outcomes = {}

    for source in SOURCE_ORDER:
        if mode == "skip":
            outcomes[source] = {
                "source": SOURCES[source],
                "status": "skipped_by_request",
                "provider_called": False,
            }
            continue
        if before[source]["state"] == "fresh":
            outcomes[source] = {
                "source": SOURCES[source],
                "status": "cached_fresh",
                "provider_called": False,
                "message": "Stored evidence is within the freshness policy.",
            }
            continue
        outcomes[source] = refresh_one(
            ticker, source, force=False, wait_seconds=wait_seconds, runner=runner,
        )

    failures = [s for s, o in outcomes.items() if o["status"] in ("failed", "busy")]
    if failures and on_failure == "fail":
        raise SourceRefreshError(
            "Source refresh did not complete for: " + ", ".join(SOURCES[s] for s in failures)
        )

    after = freshness_snapshot(ticker)
    for source, outcome in outcomes.items():
        outcome["evidence_state"] = (
            "cached_skipped_by_request" if outcome["status"] == "skipped_by_request"
            else EVIDENCE_STATE.get(outcome["status"], "cached")
        )

    return {
        "mode": mode,
        "on_failure": on_failure,
        "before": before,
        "after": after,
        "outcomes": outcomes,
        "failures": [SOURCES[s] for s in failures],
        "sec_facts_changed": (
            before["sec"]["sec_facts_fingerprint"] != after["sec"]["sec_facts_fingerprint"]
        ),
    }
