"""
Finnhub adapter (US market): quote, basic metrics, profile, company news,
symbol search. The key is sent in the X-Finnhub-Token header, never in URLs.
"""

import time
from datetime import date, datetime, timedelta, timezone

import httpx

from app.services.market_providers.common import (
    ProviderNoData,
    ProviderNotConfigured,
    get_json,
    limiter_for,
    to_decimal_text,
)

BASE_URL = "https://finnhub.io/api/v1"
PROVIDER = "FINNHUB"


def _settings():
    from app.core.config import get_settings

    return get_settings()


class FinnhubClient:
    def __init__(self, api_key=None, http=None, sleep=time.sleep, usage_hook=None, limiter=None):
        settings = _settings()
        if api_key is None and settings.finnhub_api_key is not None:
            api_key = settings.finnhub_api_key.get_secret_value()
        if not api_key:
            raise ProviderNotConfigured("FINNHUB_API_KEY is not configured.")
        self._headers = {"X-Finnhub-Token": api_key, "Accept": "application/json"}
        self._http = http or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))
        self._limiter = limiter or limiter_for(PROVIDER, settings.finnhub_requests_per_minute)
        self._sleep = sleep
        if usage_hook is None:
            from app.services.market_storage import record_provider_request as usage_hook
        self._usage = usage_hook

    def _get(self, path, params):
        return get_json(
            self._http, BASE_URL + path, provider=PROVIDER, limiter=self._limiter,
            params=params, headers=self._headers, sleep=self._sleep, usage_hook=self._usage,
        )

    def quote(self, symbol):
        raw = self._get("/quote", {"symbol": symbol})
        if not isinstance(raw, dict) or not raw.get("c"):
            raise ProviderNoData("Finnhub returned no price for this symbol.")
        metrics = (self._get("/stock/metric", {"symbol": symbol, "metric": "all"}) or {}).get("metric") or {}
        cap_millions = metrics.get("marketCapitalization")
        pe = metrics.get("peTTM", metrics.get("peBasicExclExtraTTM"))
        pb = metrics.get("pbQuarterly", metrics.get("pbAnnual"))
        dividend = metrics.get("dividendYieldIndicatedAnnual", metrics.get("currentDividendYieldTTM"))
        as_of = (
            datetime.fromtimestamp(raw["t"], tz=timezone.utc).isoformat()
            if isinstance(raw.get("t"), (int, float)) and raw["t"] > 0 else None
        )
        cap = to_decimal_text(cap_millions)
        return {
            "price": to_decimal_text(raw.get("c")),
            "market_cap": None if cap is None else to_decimal_text(float(cap) * 1_000_000),
            "pe_ratio": to_decimal_text(pe),
            "pb_ratio": to_decimal_text(pb),
            "dividend_yield_pct": to_decimal_text(dividend),
            "beta": to_decimal_text(metrics.get("beta")),
            "currency": "USD",
            "as_of": as_of,
            "source_fields": {
                "price": "quote.c",
                "market_cap": "metric.marketCapitalization (USD millions)",
                "pe_ratio": "metric.peTTM | peBasicExclExtraTTM",
                "pb_ratio": "metric.pbQuarterly | pbAnnual",
                "dividend_yield_pct": "metric.dividendYieldIndicatedAnnual",
                "beta": "metric.beta",
            },
        }

    def profile(self, symbol):
        raw = self._get("/stock/profile2", {"symbol": symbol})
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ProviderNoData("Finnhub returned no profile for this symbol.")
        return {
            "name": raw.get("name"),
            "exchange": raw.get("exchange"),
            "currency": raw.get("currency"),
            "sector": raw.get("finnhubIndustry"),
            "industry": raw.get("finnhubIndustry"),
            "country": raw.get("country"),
            "website": raw.get("weburl"),
            # Logo hosted by Finnhub for exactly this symbol.
            "logo_url": raw.get("logo"),
        }

    def company_news(self, symbol, days=14, today=None):
        today = today or date.today()
        raw = self._get("/company-news", {
            "symbol": symbol,
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
        })
        articles = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not item.get("headline") or not item.get("url"):
                continue
            published = item.get("datetime")
            articles.append({
                "provider_article_id": f"finnhub:{item.get('id')}" if item.get("id") else None,
                "title": str(item["headline"]).strip(),
                "description": (item.get("summary") or None),
                "article_url": str(item["url"]).strip(),
                "source_name": item.get("source"),
                "published_at": (
                    datetime.fromtimestamp(published, tz=timezone.utc).isoformat()
                    if isinstance(published, (int, float)) and published > 0 else None
                ),
            })
        return articles

    def search(self, query):
        raw = self._get("/search", {"q": query, "exchange": "US"})
        results = []
        for item in (raw or {}).get("result") or []:
            symbol = item.get("symbol") or ""
            if not symbol or "." in symbol or item.get("type") not in ("Common Stock", None, ""):
                continue
            results.append({"symbol": symbol, "name": item.get("description"), "exchange": "US"})
        return results[:15]
