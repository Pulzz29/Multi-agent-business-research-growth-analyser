"""
Yahoo Finance adapter via yfinance (Indian market, NSE ``.NS`` symbols):
quote and profile (``Ticker.info``), annual and quarterly statements,
symbol search.

yfinance is an unofficial client: it needs no key, can be throttled or break
when Yahoo changes its site, and Yahoo's terms restrict automated use. All
failures surface as ProviderError with a short message; stored data is kept.
"""

import math
import time

from app.services.market_providers.common import (
    ProviderError,
    ProviderNoData,
    limiter_for,
    to_decimal_text,
)

PROVIDER = "YFINANCE"

STATEMENT_FRAMES = {
    # attribute: (statement, period_type)
    "income_stmt": ("income", "annual"),
    "balance_sheet": ("balance", "annual"),
    "cashflow": ("cash_flow", "annual"),
    "quarterly_income_stmt": ("income", "quarterly"),
    "quarterly_balance_sheet": ("balance", "quarterly"),
    "quarterly_cashflow": ("cash_flow", "quarterly"),
}

ROW_MAP = {
    "income": [
        ("revenue", ("Total Revenue", "Operating Revenue")),
        ("gross_profit", ("Gross Profit",)),
        ("operating_income", ("Operating Income",)),
        ("net_income", ("Net Income", "Net Income Common Stockholders")),
        ("eps_diluted", ("Diluted EPS",)),
    ],
    "balance": [
        ("total_assets", ("Total Assets",)),
        ("total_equity", ("Stockholders Equity", "Common Stock Equity")),
        ("total_debt", ("Total Debt",)),
    ],
    "cash_flow": [
        ("operating_cash_flow", ("Operating Cash Flow",)),
    ],
}


def _settings():
    from app.core.config import get_settings

    return get_settings()


def _default_ticker_factory(symbol):
    import yfinance

    return yfinance.Ticker(symbol)


def _default_search(query):
    import yfinance

    return yfinance.Search(query, max_results=10, news_count=0, lists_count=0,
                           recommended=0, raise_errors=True).quotes


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class YFinanceClient:
    def __init__(self, ticker_factory=None, search=None, usage_hook=None, limiter=None):
        self._ticker = ticker_factory or _default_ticker_factory
        self._search = search or _default_search
        self._limiter = limiter or limiter_for(PROVIDER, _settings().yfinance_requests_per_minute)
        if usage_hook is None:
            from app.services.market_storage import record_provider_request as usage_hook
        self._usage = usage_hook

    def _call(self, fn, what):
        self._limiter.acquire()
        self._usage(PROVIDER)
        try:
            return fn()
        except ProviderError:
            raise
        except Exception:  # yfinance raises many types; never echo them
            raise ProviderError(f"Yahoo Finance {what} could not be retrieved.") from None

    def profile_and_quote(self, symbol):
        info = self._call(lambda: self._ticker(symbol).info, "profile")
        if not isinstance(info, dict) or not (info.get("longName") or info.get("shortName")):
            raise ProviderNoData("Yahoo Finance returned no profile for this symbol.")
        price = _finite(info.get("currentPrice")) or _finite(info.get("regularMarketPrice"))
        trailing_yield = _finite(info.get("trailingAnnualDividendYield"))  # a fraction
        if trailing_yield is None:
            rate = _finite(info.get("trailingAnnualDividendRate"))
            trailing_yield = (rate / price) if rate is not None and price else None
        profile = {
            "name": info.get("longName") or info.get("shortName"),
            "exchange": info.get("exchange"),
            "currency": info.get("currency"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "website": info.get("website"),
            "description": info.get("longBusinessSummary"),
        }
        quote = {
            "price": to_decimal_text(price),
            "market_cap": to_decimal_text(info.get("marketCap")),
            "pe_ratio": to_decimal_text(info.get("trailingPE")),
            "pb_ratio": to_decimal_text(info.get("priceToBook")),
            "dividend_yield_pct": (
                None if trailing_yield is None else to_decimal_text(round(trailing_yield * 100, 6))
            ),
            "beta": to_decimal_text(info.get("beta")),
            "currency": info.get("currency"),
            "as_of": None,
            "source_fields": {
                "price": "info.currentPrice | regularMarketPrice",
                "market_cap": "info.marketCap",
                "pe_ratio": "info.trailingPE",
                "pb_ratio": "info.priceToBook",
                "dividend_yield_pct": "info.trailingAnnualDividendYield x 100",
                "beta": "info.beta",
            },
        }
        if quote["price"] is None and quote["market_cap"] is None:
            raise ProviderNoData("Yahoo Finance returned no price data for this symbol.")
        return profile, quote

    def statements(self, symbol, currency=None):
        ticker = self._call(lambda: self._ticker(symbol), "ticker")
        rows = []
        for attribute, (statement, period_type) in STATEMENT_FRAMES.items():
            frame = self._call(lambda a=attribute: getattr(ticker, a), f"{statement} statement")
            if frame is None or getattr(frame, "empty", True):
                continue
            index = set(str(i) for i in frame.index)
            for metric, candidates in ROW_MAP[statement]:
                label = next((c for c in candidates if c in index), None)
                if label is None:
                    continue
                series = frame.loc[label]
                for column, value in series.items():
                    text = to_decimal_text(_finite(value))
                    if text is None:
                        continue
                    period_end = str(getattr(column, "date", lambda: column)())[:10]
                    rows.append({
                        "symbol": symbol,
                        "provider": PROVIDER,
                        "statement": statement,
                        "period_type": period_type,
                        "period_end": period_end,
                        "fiscal_year": period_end[:4],
                        "metric": metric,
                        "value_text": text,
                        "currency": currency,
                        "source_field": f"{attribute}['{label}']",
                    })
        if not rows:
            raise ProviderNoData("Yahoo Finance returned no financial statements for this symbol.")
        return rows

    def search(self, query):
        quotes = self._call(lambda: self._search(query), "search")
        results = []
        for item in quotes or []:
            symbol = str(item.get("symbol") or "")
            if not (symbol.endswith(".NS") or symbol.endswith(".BO")):
                continue
            if item.get("quoteType") not in (None, "EQUITY"):
                continue
            results.append({
                "symbol": symbol,
                "name": item.get("longname") or item.get("shortname"),
                "exchange": item.get("exchange"),
            })
        return results[:15]
