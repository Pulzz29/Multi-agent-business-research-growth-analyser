"""
Company data for market company pages, with server-side freshness rules.

``get_company_data(market, symbol, mode)`` is the single place that decides:

* mode "auto"  - never refreshed -> fetch; older than the TTL -> fetch;
                 otherwise serve stored data without calling any provider.
* mode "force" - fetch regardless of age, unless the same company was force-
                 refreshed within the cooldown (then stored data + a message).

A refresh is successful when profile, quote and statements all refreshed
(news failures are warnings). Only a successful refresh updates
``last_refreshed_at`` (UTC). A failed refresh returns the stored data with
``stale: true`` and ``refresh_error``, or raises NoCompanyData when nothing is
stored. Concurrent requests for the same company share one refresh: later
callers wait for and reuse the first one's result (no second provider round).
"""

import logging
import threading
from datetime import timedelta

from app.services import market_registry as registry
from app.services import market_storage as storage
from app.services.data_freshness import configured_ttl_hours, needs_refresh, parse_utc, utc_now


logger = logging.getLogger("insightiq.company_data")

ALL_SOURCES = ("profile", "quote", "statements", "news")
CORE_SOURCES = ("profile", "quote", "statements")
JOIN_TIMEOUT_SECONDS = 180.0
MODES = ("auto", "force")

DECISIONS = {
    "never_refreshed": "auto_refresh_new",
    "stale": "auto_refresh_stale",
    "fresh": "served_from_cache",
}


class UnknownCompany(LookupError):
    pass


class NoCompanyData(RuntimeError):
    pass


class _InFlight:
    def __init__(self):
        self.event = threading.Event()
        self.outcome = None


_INFLIGHT = {}
_LAST_FORCE = {}
_GUARD = threading.Lock()


def _cooldown_seconds():
    from app.core.config import get_settings

    return int(get_settings().force_refresh_cooldown_seconds)


def cooldown_remaining(symbol, now):
    with _GUARD:
        last = _LAST_FORCE.get(symbol)
    if last is None:
        return 0
    remaining = (last + timedelta(seconds=_cooldown_seconds()) - now).total_seconds()
    return max(0, int(round(remaining)))


# ----------------------------------------------------------------- payload

def build_payload(market, symbol, now=None):
    """Stored company data (read-only). None when the company is unknown."""
    from app.services import market_ingestion
    from app.services import sector_comparison

    summary = registry.company_summary(market, symbol)
    if summary is None:
        return None
    registered = summary["registered"]
    stored = storage.get_company(symbol) if registered else None
    metrics, inputs = sector_comparison.company_metrics(symbol) if registered else ({}, {})
    statements = storage.statement_values(symbol, "annual") if registered else {}
    last = (stored or {}).get("last_refreshed_at")
    due, reason, _ = needs_refresh(last, now or utc_now())
    return {
        **summary,
        "metrics": metrics,
        "metric_inputs": inputs,
        "annual_statements": {
            period: {metric: row["value_text"] for metric, row in values.items()}
            for period, values in sorted(statements.items(), reverse=True)[:5]
        },
        "news": market_ingestion.stored_news(symbol) if registered else [],
        "freshness": market_ingestion.source_status(market, symbol) if registered else {},
        "metric_definitions": sector_comparison.ranking_service.load_ranking_config()["metrics"],
        "last_refreshed_at": last,
        # Display only; the decision is made by get_company_data on the server.
        "refresh_due": due,
        "refresh_due_reason": reason,
    }


def _has_data(payload):
    return bool(payload and (payload.get("quote") or payload.get("annual_statements")))


# ------------------------------------------------------------------ refresh

def _owner_refresh(market, symbol, name, clients, now, refresh_fn):
    """Run one provider refresh and record success. Never raises."""
    try:
        outcome = refresh_fn(
            market, symbol, ALL_SOURCES, force=True, clients=clients,
            name=name, wait_seconds=60.0,
        )
    except registry.RegistryError as exc:
        return {"unknown": True, "error": str(exc), "results": {}}
    except Exception as exc:  # provider/persistence failure; message not echoed
        logger.warning("company_data symbol=%s refresh_exception=%s", symbol, type(exc).__name__)
        return {"success": False, "error": "The refresh could not complete.", "results": {}}

    results = outcome.get("results") or {}
    failed = [k for k in CORE_SOURCES if (results.get(k) or {}).get("status") != "refreshed"]
    news = results.get("news") or {}
    warnings = []
    if news and news.get("status") not in ("refreshed", "cached_fresh"):
        warnings.append(f"News could not be refreshed: {news.get('message') or news.get('status')}")

    if failed:
        first = results.get(failed[0]) or {}
        error = first.get("message") or f"{failed[0]} could not be refreshed ({first.get('status') or 'no result'})."
        return {"success": False, "error": error, "failed_sources": failed,
                "results": results, "warnings": warnings}

    storage.set_last_refreshed_at(symbol, now.isoformat())
    return {"success": True, "error": None, "results": results, "warnings": warnings}


def _shared_refresh(market, symbol, name, clients, now, refresh_fn):
    """One refresh per company at a time; concurrent callers reuse it."""
    with _GUARD:
        flight = _INFLIGHT.get(symbol)
        owner = flight is None
        if owner:
            flight = _INFLIGHT[symbol] = _InFlight()

    if not owner:
        if not flight.event.wait(JOIN_TIMEOUT_SECONDS):
            return {"success": False, "error": "A refresh for this company is still running.",
                    "results": {}}, True
        return flight.outcome, True

    try:
        flight.outcome = _owner_refresh(market, symbol, name, clients, now, refresh_fn)
        return flight.outcome, False
    finally:
        flight.event.set()
        with _GUARD:
            _INFLIGHT.pop(symbol, None)


# --------------------------------------------------------------------- API

def get_company_data(market, symbol, mode="auto", name=None, clients=None, now=None, refresh_fn=None):
    if mode not in MODES:
        raise ValueError("mode must be 'auto' or 'force'.")
    market = registry.require_market(market)
    symbol = registry.require_symbol(symbol)
    now = now or utc_now()
    if refresh_fn is None:
        from app.services.market_ingestion import refresh_company as refresh_fn

    summary = registry.company_summary(market, symbol)
    if summary is None and not name:
        raise UnknownCompany("Company not found. Search providers to add it.")
    stored = storage.get_company(symbol) if summary and summary["registered"] else None
    last = (stored or {}).get("last_refreshed_at")
    ttl = configured_ttl_hours()

    info = {
        "mode": mode, "ttl_hours": ttl, "decision": None, "refreshed": False,
        "stale": False, "refresh_error": None, "warnings": [], "message": None,
        "joined_in_progress_refresh": False, "cooldown_seconds_remaining": 0,
        "sources": None,
    }

    if mode == "force":
        remaining = cooldown_remaining(symbol, now)
        if remaining > 0:
            do_refresh = False
            info["decision"] = "force_cooldown"
            info["cooldown_seconds_remaining"] = remaining
            info["message"] = (
                f"This company was refreshed moments ago. You can refresh it again in "
                f"{remaining} seconds; the stored data is shown."
            )
        else:
            do_refresh = True
            info["decision"] = "forced"
            with _GUARD:
                _LAST_FORCE[symbol] = now
        age_hours = None
        last_dt = parse_utc(last)
        if last_dt is not None:
            age_hours = (now - last_dt).total_seconds() / 3600
    else:
        do_refresh, reason, age_hours = needs_refresh(last, now, ttl)
        info["decision"] = DECISIONS[reason]

    logger.info(
        "company_data symbol=%s mode=%s decision=%s age_hours=%s ttl_hours=%s",
        symbol, mode, info["decision"],
        None if age_hours is None else round(age_hours, 2), ttl,
    )

    if do_refresh:
        outcome, joined = _shared_refresh(market, symbol, name, clients, now, refresh_fn)
        info["joined_in_progress_refresh"] = joined
        info["sources"] = outcome.get("results")
        info["warnings"] = list(outcome.get("warnings") or [])
        if outcome.get("unknown"):
            raise UnknownCompany(outcome["error"])
        if outcome.get("success"):
            info["refreshed"] = True
            logger.info("company_data symbol=%s outcome=refreshed joined=%s", symbol, joined)
        else:
            payload = build_payload(market, symbol, now)
            if not _has_data(payload):
                logger.warning("company_data symbol=%s outcome=failed_no_data", symbol)
                raise NoCompanyData(
                    f"No data could be loaded for {symbol}: {outcome.get('error')}"
                )
            info["stale"] = True
            info["refresh_error"] = outcome.get("error")
            logger.warning("company_data symbol=%s outcome=failed_stale", symbol)

    payload = build_payload(market, symbol, now)
    if payload is None or (not _has_data(payload) and not payload.get("registered")):
        raise NoCompanyData(f"No data is stored for {symbol} yet.")
    payload["data_refresh"] = {**info, "last_refreshed_at": payload.get("last_refreshed_at")}
    return payload
