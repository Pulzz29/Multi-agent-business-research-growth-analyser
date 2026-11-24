"""
Load and refresh company data from the market's providers.

US: profile + statements from Financial Modeling Prep, quote/metrics and
company news from Finnhub. India: profile, quote and statements from
yfinance, news from NewsData.io (existing ingestion, with search terms).

Each source is refreshed only when stale (or forced), under a per
(symbol, source) lock; every attempt is logged in ``source_refreshes``.
Statuses mirror services/source_refresh.py: refreshed, cached_fresh, busy,
failed, not_configured. Failures never delete or alter stored data.
"""

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from app.core.config import get_settings
from app.database.sqlite_manager import connect, create_recovery_point, transaction
from app.services import market_registry as registry
from app.services import market_storage as storage
from app.services.market_providers.common import (
    ProviderAccessDenied,
    ProviderError,
    ProviderNoData,
    ProviderNotConfigured,
)


logger = logging.getLogger("insightiq.market_ingestion")

SOURCES = ("profile", "quote", "statements", "news")

# source_refreshes.source codes per (market, kind)
SOURCE_CODES = {
    ("US", "profile"): "FMP_PROFILE",
    ("US", "quote"): "FINNHUB_QUOTE",
    ("US", "statements"): "FMP_STATEMENTS",
    ("US", "news"): "FINNHUB_NEWS",
    ("IN", "profile"): "YFINANCE_PROFILE",
    ("IN", "quote"): "YFINANCE_QUOTE",
    ("IN", "statements"): "YFINANCE_STATEMENTS",
    ("IN", "news"): "NEWSDATA",
}

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock(symbol, kind):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((symbol, kind), threading.Lock())


def default_clients(market):
    """Lazily constructed provider clients (missing keys -> None + reason)."""
    clients, missing = {}, {}
    if market == "US":
        from app.services.market_providers.finnhub_client import FinnhubClient
        from app.services.market_providers.fmp_client import FmpClient

        for name, factory in (("finnhub", FinnhubClient), ("fmp", FmpClient)):
            try:
                clients[name] = factory()
            except ProviderNotConfigured as exc:
                missing[name] = exc.safe_message
    else:
        from app.services.market_providers.yfinance_client import YFinanceClient

        clients["yfinance"] = YFinanceClient()
    return clients, missing


def _age_hours(timestamp):
    if not timestamp:
        return None
    try:
        then = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def _policy_hours(kind):
    settings = get_settings()
    return {
        "profile": settings.market_statement_cache_hours,
        "statements": settings.market_statement_cache_hours,
        "quote": settings.market_quote_cache_hours,
        "news": settings.news_cache_hours,
    }[kind]


def ensure_registered(market, symbol, name=None):
    """Register a universe company (or a searched one with a known name)."""
    stored = storage.get_company(symbol)
    if stored:
        return stored
    entry = registry.universe_entry(market, symbol)
    if entry is None and not name:
        return None
    return storage.register_company(
        symbol=symbol,
        market=market,
        name=(entry or {}).get("name") or name,
        currency=registry.market_config(market)["currency"],
        sector_key=(entry or {}).get("sector_key"),
        in_universe=entry is not None,
    )


# ------------------------------------------------------------ source work

def _apply_profile(market, symbol, profile, provider):
    entry = registry.universe_entry(market, symbol)
    sector_key = (entry or {}).get("sector_key") or registry.map_provider_sector(
        market, profile.get("sector"), profile.get("industry"))
    storage.register_company(
        symbol=symbol, market=market, name=profile.get("name") or symbol,
        exchange=profile.get("exchange"), currency=profile.get("currency"),
        sector_key=sector_key, provider_sector=profile.get("sector"),
        provider_industry=profile.get("industry"), cik=profile.get("cik"),
        in_universe=entry is not None, profile_provider=provider,
        profile={k: profile.get(k) for k in ("country", "website", "description") if profile.get(k)},
    )


def _store_news_articles(symbol, company, articles, provider):
    """Store provider news (Finnhub) in news_articles with the existing dedupe rules."""
    from app.services.news_ingestion import normalized_article_url, relevance_for_article
    import json

    terms = registry.news_terms_for(company)
    inserted = 0
    with transaction() as conn:
        for item in articles:
            canonical = normalized_article_url(item["article_url"])
            if not canonical or not item["article_url"].startswith("https://"):
                continue
            score, reason = max(
                (relevance_for_article(
                    {"title": item["title"], "description": item.get("description") or ""},
                    term, symbol) for term in terms),
                key=lambda r: r[0],
            )
            if score < 0.60:
                continue
            provider_id = item.get("provider_article_id") or f"url:{canonical}"
            existing = conn.execute(
                """
                SELECT id FROM news_articles WHERE company_ticker = ? AND (
                    provider_article_id = ? OR article_url = ? OR
                    (CASE WHEN json_valid(raw_metadata_json)
                     THEN json_extract(raw_metadata_json, '$.canonical_url') END) = ?)
                LIMIT 1
                """,
                (symbol, provider_id, item["article_url"], canonical),
            ).fetchone()
            if existing:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO news_articles (
                    company_ticker, provider, provider_article_id, title, description,
                    article_url, source_name, published_at, retrieved_at,
                    relevance_score, relevance_reason, raw_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, provider, provider_id, item["title"], item.get("description"),
                 item["article_url"], item.get("source_name"), item.get("published_at"),
                 storage.utc_now(), score, reason,
                 json.dumps({"canonical_url": canonical, "provider": provider})),
            )
            inserted += cursor.rowcount == 1
    return inserted


def _run_source(market, symbol, kind, company, clients, missing):
    """Returns (fetched, inserted). Raises ProviderError on failure."""
    if market == "US":
        if kind == "profile":
            if "fmp" in clients:
                _apply_profile(market, symbol, clients["fmp"].profile(symbol), "FMP")
            elif "finnhub" in clients:
                _apply_profile(market, symbol, clients["finnhub"].profile(symbol), "FINNHUB")
            else:
                raise ProviderNotConfigured(missing.get("fmp") or "No US profile provider is configured.")
            return 1, 0
        if kind == "quote":
            def fmp_quote():
                profile = clients["fmp"].profile(symbol)
                storage.insert_quote(symbol, "FMP", {
                    "price": profile.get("price"), "market_cap": profile.get("market_cap"),
                    "beta": profile.get("beta"), "currency": profile.get("currency") or "USD",
                    "source_fields": {"price": "profile.price", "market_cap": "profile.marketCap",
                                      "beta": "profile.beta"},
                })

            if "finnhub" in clients:
                try:
                    storage.insert_quote(symbol, "FINNHUB", clients["finnhub"].quote(symbol))
                except (ProviderAccessDenied, ProviderNoData) as exc:
                    # Rejected key/plan or unknown symbol at Finnhub: use FMP's
                    # profile quote (labelled as FMP) when FMP is configured.
                    if "fmp" not in clients:
                        raise
                    logger.info("Finnhub quote unavailable for %s (%s); using FMP profile quote.",
                                symbol, type(exc).__name__)
                    fmp_quote()
            elif "fmp" in clients:
                fmp_quote()
            else:
                raise ProviderNotConfigured(missing.get("finnhub") or "No US quote provider is configured.")
            return 1, 1
        if kind == "statements":
            if "fmp" not in clients:
                raise ProviderNotConfigured(missing.get("fmp") or "FMP_API_KEY is not configured.")
            rows = clients["fmp"].statements(symbol, "annual", limit=5)
            try:
                rows += clients["fmp"].statements(symbol, "quarter", limit=8)
            except ProviderError as exc:
                logger.info("FMP quarterly statements unavailable for %s: %s", symbol, exc.safe_message)
            return len(rows), storage.insert_statement_rows(rows)
        if kind == "news":
            if "finnhub" not in clients:
                raise ProviderNotConfigured(missing.get("finnhub") or "FINNHUB_API_KEY is not configured.")
            articles = clients["finnhub"].company_news(symbol)
            return len(articles), _store_news_articles(symbol, company, articles, "FINNHUB")
    else:
        if kind in ("profile", "quote"):
            # One Yahoo Finance call returns both profile and quote fields.
            profile, quote = clients["yfinance"].profile_and_quote(symbol)
            _apply_profile(market, symbol, profile, "YFINANCE")
            storage.insert_quote(symbol, "YFINANCE", quote)
            return 1, 1
        if kind == "statements":
            stored = storage.get_company(symbol) or {}
            rows = clients["yfinance"].statements(symbol, currency=stored.get("currency"))
            return len(rows), storage.insert_statement_rows(rows)
        if kind == "news":
            from app.services.news_ingestion import refresh_news_company

            result = asyncio.run(refresh_news_company(
                symbol, max_articles=30, max_pages=2, force_refresh=True,
                search_terms=registry.news_terms_for(company), country="in",
            ))
            return int(result.get("records_fetched") or 0), int(result.get("records_inserted") or 0)
    raise ValueError("Unknown source kind.")


def refresh_company(market, symbol, sources=SOURCES, force=False, clients=None,
                    name=None, wait_seconds=0.0, checkpoint=True):
    """Refresh the requested sources for one company. Never raises for provider errors."""
    market = registry.require_market(market)
    symbol = registry.require_symbol(symbol)
    storage.ensure_market_storage()

    company = ensure_registered(market, symbol, name=name)
    if company is None:
        raise registry.RegistryError(
            "Unknown company: search for it first so its name is known.")
    entry = registry.universe_entry(market, symbol) or {}
    company = {**company, "news_terms": entry.get("news_terms")}

    missing = {}
    if clients is None:
        clients, missing = default_clients(market)

    results = {}
    kinds = [k for k in SOURCES if k in set(sources)]
    if market == "IN" and "profile" in kinds and "quote" in kinds:
        kinds.remove("profile")  # one yfinance call provides both

    for kind in kinds:
        code = SOURCE_CODES[(market, kind)]
        result = {"source": code, "kind": kind, "status": None, "records_fetched": 0,
                  "records_inserted": 0, "provider_called": False, "message": None}
        results[kind] = result
        lock = _lock(symbol, kind)
        if not (lock.acquire(timeout=wait_seconds) if wait_seconds else lock.acquire(blocking=False)):
            result.update(status="busy", message="Another refresh of this data is running.")
            continue
        try:
            last = storage.last_success(symbol, code)
            age = _age_hours(last)
            if not force and age is not None and 0 <= age < _policy_hours(kind):
                result.update(status="cached_fresh", last_successful_refresh_at=last,
                              message="Stored data is within the freshness policy; no provider call.")
                continue
            refresh_id = None if code == "NEWSDATA" else storage.start_refresh(symbol, code)
            started = time.perf_counter()
            try:
                result["provider_called"] = True
                fetched, inserted = _run_source(market, symbol, kind, company, clients, missing)
            except ProviderNotConfigured as exc:
                if refresh_id:
                    storage.finish_refresh(refresh_id, False, error=exc)
                result.update(status="not_configured", provider_called=False, message=exc.safe_message)
                continue
            except ProviderError as exc:
                if refresh_id:
                    storage.finish_refresh(refresh_id, False, error=exc)
                result.update(status="failed", error_category=type(exc).__name__, message=exc.safe_message)
                continue
            except Exception as exc:  # ingestion/persistence failure; message not echoed
                logger.warning("market refresh %s/%s failed: %s", symbol, kind, type(exc).__name__)
                if refresh_id:
                    storage.finish_refresh(refresh_id, False, error=exc)
                result.update(status="failed", error_category=type(exc).__name__,
                              message="The refresh could not complete.")
                continue
            if refresh_id:
                storage.finish_refresh(refresh_id, True, fetched=fetched, inserted=inserted)
            result.update(status="refreshed", records_fetched=fetched, records_inserted=inserted,
                          elapsed_seconds=round(time.perf_counter() - started, 3),
                          message="Provider data retrieved, validated and stored.")
        finally:
            lock.release()

    if market == "IN" and "profile" in sources and "quote" in sources:
        results["profile"] = {**results.get("quote", {}), "kind": "profile",
                              "note": "Profile and quote come from the same Yahoo Finance call."}

    # Market data must survive a lost runtime database (for example a
    # cleared session temp folder): record a verified recovery point, as
    # the SEC and news refreshes do.
    recovery = None
    if checkpoint and any(r.get("status") == "refreshed" for r in results.values()):
        recovery = create_recovery_point(f"market_refresh:{symbol}")

    return {"market": market, "symbol": symbol, "results": results,
            "recovery_point": recovery,
            "company": registry.company_summary(market, symbol)}


def refresh_many(market, symbols, sources, force=False, clients=None, max_companies=None):
    """Refresh several companies sequentially (bounded)."""
    market = registry.require_market(market)
    max_companies = max_companies or get_settings().sector_refresh_max_companies
    if clients is None:
        clients, missing = default_clients(market)
    outcomes = {}
    for symbol in list(symbols)[:max_companies]:
        try:
            outcomes[symbol] = refresh_company(market, symbol, sources, force=force,
                                               clients=clients, wait_seconds=60.0,
                                               checkpoint=False)["results"]
        except registry.RegistryError as exc:
            outcomes[symbol] = {"error": str(exc)}
    skipped = list(symbols)[max_companies:]
    refreshed = any(
        isinstance(r, dict) and r.get("status") == "refreshed"
        for results in outcomes.values() for r in results.values()
    )
    recovery = create_recovery_point(f"market_batch_refresh:{market}") if refreshed else None
    return {"outcomes": outcomes, "skipped_over_limit": skipped, "recovery_point": recovery}


def stored_news(symbol, limit=30):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, description, article_url, source_name, published_at,
                   retrieved_at, relevance_score, relevance_reason, provider
            FROM news_articles WHERE company_ticker = ?
            ORDER BY published_at DESC, id DESC LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()
    return [
        {
            "evidence_id": f"NEWS_{symbol}_{r['id']}",
            "title": r["title"],
            "description": r["description"],
            "source_url": r["article_url"] if str(r["article_url"] or "").startswith("https://") else None,
            "publisher": r["source_name"],
            "published_at": r["published_at"],
            "retrieved_at": r["retrieved_at"],
            "relevance_score": r["relevance_score"],
            "provider": r["provider"],
        }
        for r in rows
    ]


def source_status(market, symbol):
    out = {}
    for kind in SOURCES:
        code = SOURCE_CODES[(market, kind)]
        last = storage.last_success(symbol, code)
        age = _age_hours(last)
        out[kind] = {
            "source": code,
            "last_successful_refresh_at": last,
            "policy_max_age_hours": _policy_hours(kind),
            "state": "never" if last is None else ("fresh" if age < _policy_hours(kind) else "stale"),
        }
    return out
