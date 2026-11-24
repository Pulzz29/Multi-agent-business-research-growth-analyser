"""
Read-only company intelligence profile for the dashboard.

Everything here is assembled from data InsightIQ has already stored:
SEC facts (through the existing normalization), stored NewsData.io
articles, and the persisted forecast cache files.

This module never calls the AI provider, NewsData.io or SEC, never trains a
forecast model, and never writes to the database or to disk.
In particular it does not call forecast_cache.forecast_financial_metric_cached()
or forecast_cache.source_fingerprint(), and reads the cache envelope directly.
"""

import json
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.core.config import get_settings
from app.database.sqlite_manager import connect
from app.services import financial_normalization as fin
from app.services import forecast_cache

# Companies on the validated SEC pipeline (live set; see sec_coverage.py).
from app.services.sec_coverage import SEC_VALIDATED as SUPPORTED_COMPANIES  # noqa: E402


def display_name(ticker):
    """Display name from the verified company identity (universe / registry)."""
    try:
        from app.services.company_identity import identity_for

        return identity_for(ticker).display_name
    except Exception:
        return ticker

METRICS = ("revenue", "operating_cash_flow")

METHOD_LABELS = {
    "first_quarter_ytd": "Reported first-quarter value (fiscal Q1 year-to-date equals the quarter).",
    "current_ytd_minus_previous_ytd": "Derived: current year-to-date total minus the prior year-to-date total.",
    "annual_minus_nine_month_ytd": "Derived: full-year 10-K total minus the nine-month year-to-date total.",
}

_CACHE_LOCK = threading.Lock()
_FINANCIALS_CACHE = {}


class CompanyProfileError(LookupError):
    pass


def project_root():
    return Path(get_settings().project_root).resolve()


def require_ticker(ticker):
    ticker = str(ticker or "").strip().upper()
    if ticker not in SUPPORTED_COMPANIES:
        raise CompanyProfileError("Unsupported company ticker.")
    return ticker


def _parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _sec_fingerprint(conn, ticker):
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(id) AS max_id FROM sec_facts WHERE company_ticker = ?",
        (ticker,),
    ).fetchone()
    return (row["n"], row["max_id"])


def _normalized(ticker):
    """Normalized quarterly series, cached until the ticker's SEC facts change."""
    with connect() as conn:
        key = (ticker, *_sec_fingerprint(conn, ticker))

    with _CACHE_LOCK:
        cached = _FINANCIALS_CACHE.get(ticker)
        if cached and cached[0] == key:
            return cached[1]

    data = fin.normalized_quarterly_financials(ticker)

    with _CACHE_LOCK:
        _FINANCIALS_CACHE[ticker] = (key, data)
    return data


def _pct_change(current, previous):
    """Percentage change under the growth-analysis rules: no percentage from a
    zero or negative base (see growth_analysis.change)."""
    from app.services.growth_analysis import change

    return change(current, previous)["value_pct"]


def _growth(current, previous):
    from app.services.growth_analysis import change

    outcome = change(current, previous)
    return outcome["value_pct"], outcome["status"], outcome.get("reason")


def _fiscal_label(row):
    """Fiscal label, e.g. FY2026 Q2, where FY is the calendar year in which the fiscal year ends."""
    start = _parse_date(row["fiscal_year_start"])
    if start is None:
        return None
    # Companies name a fiscal year after the calendar year in which it ends:
    # MSFT Jul 2026-Jun 2027 = FY2027, NVDA late Jan 2026-Jan 2027 = FY2027,
    # calendar-year filers Jan-Dec 2025 = FY2025.
    fy = (start + timedelta(days=363)).year
    return f"FY{fy} Q{row['fiscal_quarter']}"


def build_series(rows):
    """Add period labels and only well-defined QoQ / YoY comparisons."""
    out = []
    by_end = {}
    for row in rows:
        value = Decimal(row["value"])
        start = _parse_date(row["period_start"])
        end = _parse_date(row["period_end"])
        item = {
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "fiscal_year_start": row["fiscal_year_start"],
            "fiscal_quarter": row["fiscal_quarter"],
            "fiscal_label": _fiscal_label(row),
            "calendar_label": f"{end.year} Q{(end.month - 1) // 3 + 1}" if end else None,
            "days": (end - start).days + 1 if start and end else None,
            "value_usd": row["value"],
            "unit": row["unit"],
            "concept": row["concept"],
            "method": row["method"],
            "method_label": METHOD_LABELS.get(row["method"], row["method"]),
            "is_derived": row["method"] != "first_quarter_ytd",
            "evidence": row["evidence"],
            "qoq_change_pct": None,
            "qoq_basis": None,
            "qoq_status": None,
            "qoq_reason": None,
            "yoy_change_pct": None,
            "yoy_basis": None,
            "yoy_status": None,
            "yoy_reason": None,
        }
        out.append((item, value, start, end))
        by_end[row["period_end"]] = (item, value)

    for index, (item, value, start, end) in enumerate(out):
        # Quarter-over-quarter: only when the previous quarter ends the day before this one starts.
        if index > 0:
            prev_item, prev_value, _, prev_end = out[index - 1]
            if prev_end and start and prev_end + timedelta(days=1) == start:
                item["qoq_change_pct"], item["qoq_status"], item["qoq_reason"] = _growth(value, prev_value)
                item["qoq_basis"] = prev_item["period_end"]
        # Year-over-year: same fiscal quarter in the immediately preceding fiscal year,
        # with a period end 350-380 days earlier (handles 52/53-week fiscal calendars).
        if end:
            for prev_item, prev_value, _, prev_end in reversed(out[:index]):
                if prev_end is None:
                    continue
                gap = (end - prev_end).days
                if gap > 380:
                    break
                if 350 <= gap <= 380 and prev_item["fiscal_quarter"] == item["fiscal_quarter"]:
                    item["yoy_change_pct"], item["yoy_status"], item["yoy_reason"] = _growth(value, prev_value)
                    item["yoy_basis"] = prev_item["period_end"]
                    break
    return [item for item, *_ in out]


def _month_last_day(year, month):
    first_of_next = date(year + month // 12, month % 12 + 1, 1)
    return (first_of_next - timedelta(days=1)).day


def _shift_quarter_end(end, quarters):
    """Approximate end date N quarters after `end`.

    Month-end period ends stay month-end; 52/53-week calendars
    (e.g. AAPL, NVDA) keep their day-of-month, so the date is
    an approximation and is labelled as such in the UI.
    """
    index = end.month - 1 + 3 * quarters
    year, month = end.year + index // 12, index % 12 + 1
    last = _month_last_day(year, month)
    if end.day == _month_last_day(end.year, end.month):
        return date(year, month, last)
    return date(year, month, min(end.day, last))


def read_forecast(ticker, metric):
    """Read the persisted forecast cache envelope without recomputation."""
    from app.services.forecast_cache import cache_directory_name

    path = project_root() / "data" / cache_directory_name() / f"{ticker}_{metric}.json"
    if not path.is_file():
        return {"available": False, "reason": "No stored forecast for this metric."}

    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        result = envelope["result"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"available": False, "reason": "Stored forecast could not be read."}

    if envelope.get("version") != forecast_cache.CACHE_VERSION:
        return {
            "available": False,
            "reason": (
                "The stored forecast was computed with an earlier validation method; "
                "it is recomputed (with point-in-time validation) on the next research run."
            ),
        }

    integrity_ok = (
        forecast_cache.validate_cached_result(result, ticker, metric)
        and forecast_cache.sha256_text(forecast_cache.canonical_json(result))
        == envelope.get("result_sha256")
    )
    if not integrity_ok:
        return {"available": False, "reason": "Stored forecast failed its integrity check."}

    cached_at = envelope.get("cached_at_utc")
    ttl = forecast_cache.cache_ttl_seconds()
    # The fingerprint covers every SEC fact and the forecasting code: a
    # mismatch means newer data exists than the forecast was built from.
    try:
        inputs_current = forecast_cache.source_fingerprint(ticker, metric) == envelope.get("fingerprint")
    except Exception:
        inputs_current = None
    ttl_expired = None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)).total_seconds()
        ttl_expired = age > ttl
    except (TypeError, ValueError):
        pass

    last_end = _parse_date(result.get("last_observed_period_end"))
    predictions = []
    for row in result.get("forecast", []):
        ahead = row.get("fiscal_quarter_ahead")
        predictions.append({
            **row,
            "approx_period_end": (
                _shift_quarter_end(last_end, ahead).isoformat() if last_end and ahead else None
            ),
        })

    backtesting = {}
    for name, bt in (result.get("backtesting") or {}).items():
        backtesting[name] = {
            "specification": bt.get("specification"),
            "mean_mae_billion_usd": bt.get("mean_mae_billion_usd"),
            "fold_count": len(bt.get("folds") or []),
            "failures": bt.get("failures") or [],
            "folds": [
                {
                    "train_length": f.get("train_length"),
                    "horizon": f.get("horizon"),
                    "mae_billion_usd": f.get("mae_billion_usd"),
                    "converged": (f.get("fit_diagnostics") or {}).get("converged"),
                }
                for f in bt.get("folds") or []
            ],
        }

    return {
        "available": True,
        "metric": metric,
        "selected_model": result.get("selected_model"),
        "specification": result.get("specification"),
        "selection_reason": result.get("selection_reason"),
        "interval_method": result.get("interval_method"),
        "history_quarters": result.get("history_quarters"),
        "horizon_quarters": result.get("horizon_quarters"),
        "data_cutoff": result.get("last_observed_period_end"),
        "generated_at_utc": result.get("generated_at_utc"),
        "cached_at_utc": cached_at,
        "ttl_seconds": ttl,
        "ttl_expired": ttl_expired,
        "inputs_current": inputs_current,
        "stale_reason": (
            "Newer SEC data exists than this forecast used; it is recomputed on the next research run."
            if inputs_current is False else None
        ),
        "validation": result.get("validation"),
        "predictions": predictions,
        "backtesting": backtesting,
        "final_fit_converged": (result.get("final_fit_diagnostics") or {}).get("converged"),
        "caveats": result.get("caveats") or [],
    }


def _news(conn, ticker, limit=60):
    """Company news that passes the strict relevance rules (read-only)."""
    from app.services.news_relevance import load_company_news

    rows, _ = load_company_news(conn, ticker, limit=limit)
    return [
        {
            "evidence_id": f"NEWS_{ticker}_{r['id']}",
            "database_id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "source_url": r["article_url"] if str(r["article_url"] or "").startswith("https://") else None,
            "publisher": r["source_name"],
            "published_at": r["published_at"],
            "retrieved_at": r["retrieved_at"],
            "relevance_score": r["relevance"]["score"],
            "relevance_reason": r["relevance"]["reason"],
            "event_type": r["relevance"]["event_type"],
            "provider": r["provider"],
        }
        for r in rows
    ]


def news_status(conn, ticker):
    from app.services.news_relevance import load_company_news

    verified, summary = load_company_news(conn, ticker, limit=1000)
    article = {
        # Counts and dates describe verified company-specific news only.
        "article_count": len(verified),
        "latest_published_at": max((r["published_at"] for r in verified if r.get("published_at")), default=None),
        "latest_retrieved_at": max((r["retrieved_at"] for r in verified if r.get("retrieved_at")), default=None),
    }
    refresh = conn.execute(
        """
        SELECT completed_at FROM source_refreshes
        WHERE company_ticker = ? AND source = 'NEWSDATA' AND status = 'success'
        ORDER BY id DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return {
        "article_count": article["article_count"],
        "latest_published_at": article["latest_published_at"],
        "latest_retrieved_at": article["latest_retrieved_at"],
        "last_successful_refresh_at": refresh["completed_at"] if refresh else None,
        "stored_article_count": summary["stored"],
        "hidden_article_count": summary["hidden"],
        "hidden_by_reason": summary["hidden_by_reason"],
        "relevance_version": summary["relevance_version"],
        "message": summary["message"],
    }


def _sec_status(conn, ticker):
    row = conn.execute(
        """
        SELECT COUNT(*) AS fact_count, MAX(filed_at) AS latest_filed_at,
               MAX(retrieved_at) AS latest_retrieved_at
        FROM sec_facts WHERE company_ticker = ?
        """,
        (ticker,),
    ).fetchone()
    return dict(row)


def _company_row(conn, ticker):
    row = conn.execute(
        "SELECT ticker, name, cik FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    return dict(row) if row else {"ticker": ticker, "name": None, "cik": None}


def _latest(series):
    return series[-1] if series else None


def company_overview(ticker, latest_report=None):
    ticker = require_ticker(ticker)
    data = _normalized(ticker)

    with connect() as conn:
        company = _company_row(conn, ticker)
        news = _news(conn, ticker)
        status = news_status(conn, ticker)
        sec = _sec_status(conn, ticker)

    financials = {metric: build_series(data.get(metric, [])) for metric in METRICS}
    forecasts = {metric: read_forecast(ticker, metric) for metric in METRICS}

    latest_period = max(
        (s[-1]["period_end"] for s in financials.values() if s), default=None
    )
    # The headline cards describe one common quarter: the latest quarter for
    # which every metric is stored (None when the series do not overlap).
    common_ends = set.intersection(*[{r["period_end"] for r in s} for s in financials.values()])         if all(financials.values()) else set()
    latest_common = max(common_ends, default=None)
    latest_age = (date.today() - _parse_date(latest_period)).days if latest_period else None

    return {
        "ticker": ticker,
        "company": {
            "ticker": ticker,
            "name": company.get("name") or display_name(ticker),
            "display_name": display_name(ticker),
            "cik": company.get("cik"),
        },
        "financials": financials,
        "normalization": {
            "warnings": data.get("warnings", []),
            "diagnostics": data.get("diagnostics", []),
        },
        "forecasts": forecasts,
        "news": news,
        "freshness": {
            "latest_financial_period_end": latest_period,
            "latest_common_period_end": latest_common,
            "latest_period_age_days": latest_age,
            "stale": latest_age is not None and latest_age > 200,
            "sec_fact_count": sec["fact_count"],
            "sec_latest_filed_at": sec["latest_filed_at"],
            "sec_latest_retrieved_at": sec["latest_retrieved_at"],
            "news": status,
            "latest_completed_report": latest_report,
        },
        "limitations": [
            "Quarterly values are standalone quarters derived from SEC year-to-date and annual filings where needed; the derivation method is shown for each quarter.",
            "Growth rates are shown only between contiguous quarters (QoQ) or the same fiscal quarter one year earlier (YoY).",
            "Forecasts are statistical estimates from stored model runs, not reported results, and are not regenerated when this page is opened.",
            "News items are stored NewsData.io records; their claims are not independently verified.",
        ],
    }


def company_summary_row(ticker, latest_report=None):
    ticker = require_ticker(ticker)
    data = _normalized(ticker)
    with connect() as conn:
        status = news_status(conn, ticker)

    row = {"ticker": ticker, "name": display_name(ticker)}
    for metric in METRICS:
        series = build_series(data.get(metric, []))
        latest = _latest(series)
        row[metric] = (
            {
                "value_usd": latest["value_usd"],
                "period_start": latest["period_start"],
                "period_end": latest["period_end"],
                "fiscal_label": latest["fiscal_label"],
                "yoy_change_pct": latest["yoy_change_pct"],
                "spark": [s["value_usd"] for s in series[-8:]],
            }
            if latest else None
        )
    forecast = read_forecast(ticker, "revenue")
    row["forecast_available"] = bool(forecast.get("available"))
    row["news"] = status
    row["latest_completed_report"] = latest_report
    return row
