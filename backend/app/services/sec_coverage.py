"""
Which US companies are on the validated SEC research pipeline.

* SEC-eligible: every company in the curated US universe (SEC EDGAR covers
  US-listed filers; Indian NSE companies are not SEC filers).
* SEC-validated: an eligible company whose stored SEC facts normalise into
  standalone quarters for revenue and operating cash flow, where every
  fiscal year reconciles exactly to the 10-K annual figure (enforced by
  financial_normalization), with enough contiguous quarters to analyse.

The six companies validated by the original migration are always included.
Validation results are stored in ``sec_company_validation`` (created
additively; nothing else is changed) with the reason for any company that is
not validated, so pages can show an accurate "SEC financials unavailable"
state instead of guessing.
"""

import threading
import time
from datetime import datetime, timezone

from app.database.sqlite_manager import connect, transaction

CORE_SEC_VALIDATED = frozenset({"AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA"})
MIN_VALIDATED_QUARTERS = 8
_CACHE_SECONDS = 15.0
_cache = {"at": 0.0, "value": CORE_SEC_VALIDATED}
_lock = threading.Lock()


def ensure_table(conn=None):
    def create(c):
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_company_validation (
                ticker TEXT PRIMARY KEY,
                cik TEXT,
                status TEXT NOT NULL CHECK (status IN ('validated', 'unavailable')),
                reason TEXT,
                revenue_quarters INTEGER,
                ocf_quarters INTEGER,
                latest_period_end TEXT,
                diagnostics_count INTEGER,
                checked_at TEXT NOT NULL
            )
            """
        )

    if conn is not None:
        create(conn)
    else:
        with transaction() as c:
            create(c)


def sec_eligible(ticker):
    """True for companies in the curated US universe."""
    from app.services import market_registry as registry

    ticker = str(ticker or "").strip().upper()
    return ticker in CORE_SEC_VALIDATED or registry.universe_entry("US", ticker) is not None


def _read_validated():
    try:
        with connect() as conn:
            from app.database.db import table_exists

            exists = table_exists(conn, "sec_company_validation")
            if not exists:
                return CORE_SEC_VALIDATED
            rows = conn.execute(
                "SELECT ticker FROM sec_company_validation WHERE status = 'validated'"
            ).fetchall()
        return CORE_SEC_VALIDATED | {r["ticker"] for r in rows}
    except Exception:
        return CORE_SEC_VALIDATED


def validated_tickers():
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] > _CACHE_SECONDS:
            _cache["value"] = _read_validated()
            _cache["at"] = now
        return _cache["value"]


def invalidate_cache():
    with _lock:
        _cache["at"] = 0.0


class _LiveTickerSet:
    """Set-like view of the SEC-validated tickers (read from the database)."""

    def __contains__(self, ticker):
        return str(ticker or "").strip().upper() in validated_tickers()

    def __iter__(self):
        return iter(sorted(validated_tickers()))

    def __len__(self):
        return len(validated_tickers())

    def __repr__(self):
        return f"SEC_VALIDATED({sorted(validated_tickers())})"


SEC_VALIDATED = _LiveTickerSet()


def validation_status(ticker):
    ticker = str(ticker or "").strip().upper()
    if ticker in CORE_SEC_VALIDATED:
        return {"ticker": ticker, "status": "validated", "reason": "original validated SEC pipeline"}
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM sec_company_validation WHERE ticker = ?", (ticker,)
            ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    if sec_eligible(ticker):
        return {"ticker": ticker, "status": "pending", "reason": "SEC filings not yet loaded and validated"}
    return {"ticker": ticker, "status": "not_applicable", "reason": "not a US SEC filer in the universe"}


def validate_company(ticker):
    """Normalise and reconcile stored SEC facts; store and return the result."""
    from app.services import financial_normalization as fin
    from app.services import forecasting

    ticker = str(ticker).strip().upper()
    with connect() as conn:
        company = conn.execute("SELECT cik FROM companies WHERE ticker = ?", (ticker,)).fetchone()
        facts = conn.execute("SELECT COUNT(*) FROM sec_facts WHERE company_ticker = ?", (ticker,)).fetchone()[0]
    result = {
        "ticker": ticker,
        "cik": (company["cik"] if company else None) or None,
        "status": "unavailable",
        "reason": None,
        "revenue_quarters": 0,
        "ocf_quarters": 0,
        "latest_period_end": None,
        "diagnostics_count": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if not facts:
        result["reason"] = "no SEC financial facts stored"
    else:
        try:
            financials = fin.normalized_quarterly_financials(ticker)
        except fin.FinancialNormalizationError as exc:
            result["reason"] = f"SEC normalization failed: {str(exc)[:200]}"
        else:
            revenue = forecasting.contiguous_recent_history(financials["revenue"])
            ocf = forecasting.contiguous_recent_history(financials["operating_cash_flow"])
            result.update({
                "revenue_quarters": len(revenue),
                "ocf_quarters": len(ocf),
                "latest_period_end": revenue[-1]["period_end"] if revenue else None,
                "diagnostics_count": len(financials.get("diagnostics") or []),
            })
            if not financials["revenue"]:
                result["reason"] = "no supported revenue concept in SEC filings"
            elif not financials["operating_cash_flow"]:
                result["reason"] = "no operating cash flow in SEC filings"
            elif len(revenue) < MIN_VALIDATED_QUARTERS or len(ocf) < MIN_VALIDATED_QUARTERS:
                result["reason"] = (
                    f"fewer than {MIN_VALIDATED_QUARTERS} contiguous reconciled quarters "
                    f"(revenue {len(revenue)}, operating cash flow {len(ocf)})"
                )
            else:
                result["status"] = "validated"
                result["reason"] = "quarters reconcile to 10-K annual totals"

    with transaction() as conn:
        ensure_table(conn)
        conn.execute(
            """
            INSERT INTO sec_company_validation (
                ticker, cik, status, reason, revenue_quarters, ocf_quarters,
                latest_period_end, diagnostics_count, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                cik = excluded.cik, status = excluded.status, reason = excluded.reason,
                revenue_quarters = excluded.revenue_quarters, ocf_quarters = excluded.ocf_quarters,
                latest_period_end = excluded.latest_period_end,
                diagnostics_count = excluded.diagnostics_count, checked_at = excluded.checked_at
            """,
            tuple(result[k] for k in (
                "ticker", "cik", "status", "reason", "revenue_quarters", "ocf_quarters",
                "latest_period_end", "diagnostics_count", "checked_at")),
        )
    invalidate_cache()
    return result
