"""
Financial Modeling Prep adapter (US market): company profile, income,
balance-sheet and cash-flow statements, symbol search.

Uses the FMP "stable" API. The key is sent in the ``apikey`` header. A daily
request budget (INSIGHTIQ_FMP_DAILY_REQUEST_BUDGET) protects the free-tier
quota; once used up, requests fail with ProviderQuotaExceeded.
"""

import time

import httpx

from app.services.market_providers.common import (
    ProviderNoData,
    ProviderNotConfigured,
    ProviderQuotaExceeded,
    get_json,
    limiter_for,
    to_decimal_text,
)

BASE_URL = "https://financialmodelingprep.com/stable"
PROVIDER = "FMP"

STATEMENTS = {
    "income": ("/income-statement", {
        "revenue": "revenue",
        "grossProfit": "gross_profit",
        "operatingIncome": "operating_income",
        "netIncome": "net_income",
        # Per-share; the stable API names it epsDiluted (older: epsdiluted).
        "epsDiluted": "eps_diluted",
        "epsdiluted": "eps_diluted",
        "interestExpense": "interest_expense",
        "ebitda": "ebitda",
        "depreciationAndAmortization": "depreciation_amortization",
        "weightedAverageShsOutDil": "shares_outstanding",
    }),
    "balance": ("/balance-sheet-statement", {
        "totalAssets": "total_assets",
        "totalStockholdersEquity": "total_equity",
        "totalDebt": "total_debt",
        "cashAndCashEquivalents": "cash",
        "totalCurrentAssets": "current_assets",
        "totalCurrentLiabilities": "current_liabilities",
        "inventory": "inventory",
    }),
    "cash_flow": ("/cash-flow-statement", {
        "operatingCashFlow": "operating_cash_flow",
        "capitalExpenditure": "capital_expenditure",
    }),
}


def _settings():
    from app.core.config import get_settings

    return get_settings()


class FmpClient:
    def __init__(self, api_key=None, http=None, sleep=time.sleep, usage_hook=None, usage_count=None,
                 limiter=None):
        settings = _settings()
        if api_key is None and settings.fmp_api_key is not None:
            api_key = settings.fmp_api_key.get_secret_value()
        if not api_key:
            raise ProviderNotConfigured("FMP_API_KEY is not configured.")
        self._headers = {"apikey": api_key, "Accept": "application/json"}
        self._http = http or httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0))
        self._limiter = limiter or limiter_for(PROVIDER, settings.fmp_requests_per_minute)
        self._budget = settings.fmp_daily_request_budget
        self._sleep = sleep
        if usage_hook is None or usage_count is None:
            from app.services import market_storage

            usage_hook = usage_hook or market_storage.record_provider_request
            usage_count = usage_count or market_storage.provider_requests_today
        self._usage, self._count = usage_hook, usage_count

    def _get(self, path, params):
        if self._count(PROVIDER) >= self._budget:
            raise ProviderQuotaExceeded(
                "The daily FMP request budget is used up; stored data is shown until tomorrow."
            )
        return get_json(
            self._http, BASE_URL + path, provider=PROVIDER, limiter=self._limiter,
            params=params, headers=self._headers, sleep=self._sleep, usage_hook=self._usage,
        )

    def profile(self, symbol):
        raw = self._get("/profile", {"symbol": symbol})
        item = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else None
        if not item or not item.get("companyName"):
            raise ProviderNoData("FMP returned no profile for this symbol.")
        cik = str(item.get("cik") or "").strip()
        return {
            "name": item.get("companyName"),
            "exchange": item.get("exchange") or item.get("exchangeShortName"),
            "currency": item.get("currency"),
            "sector": item.get("sector"),
            "industry": item.get("industry"),
            "country": item.get("country"),
            "cik": cik.zfill(10) if cik.isdigit() else None,
            "website": item.get("website"),
            "description": item.get("description"),
            # Logo hosted by FMP for exactly this symbol (provider-licensed asset).
            "logo_url": item.get("image"),
            "market_cap": to_decimal_text(item.get("marketCap") or item.get("mktCap")),
            "beta": to_decimal_text(item.get("beta")),
            "price": to_decimal_text(item.get("price")),
        }

    def statements(self, symbol, period="annual", limit=5):
        """Canonical statement rows for annual or quarterly periods."""
        rows = []
        for statement, (path, fields) in STATEMENTS.items():
            raw = self._get(path, {"symbol": symbol, "period": period, "limit": limit})
            for record in raw if isinstance(raw, list) else []:
                period_end = str(record.get("date") or "")[:10]
                if len(period_end) != 10:
                    continue
                for source_field, metric in fields.items():
                    value = to_decimal_text(record.get(source_field))
                    if value is None:
                        continue
                    rows.append({
                        "symbol": symbol,
                        "provider": PROVIDER,
                        "statement": statement,
                        "period_type": "annual" if period == "annual" else "quarterly",
                        "period_end": period_end,
                        "fiscal_year": str(record.get("fiscalYear") or record.get("calendarYear") or "") or None,
                        "metric": metric,
                        "value_text": value,
                        "currency": record.get("reportedCurrency"),
                        "source_field": f"{path.strip('/')}.{source_field}",
                    })
        if not rows:
            raise ProviderNoData("FMP returned no financial statements for this symbol.")
        return rows

    def search(self, query):
        raw = self._get("/search-symbol", {"query": query, "limit": 15})
        results = []
        for item in raw if isinstance(raw, list) else []:
            symbol = item.get("symbol") or ""
            exchange = item.get("exchange") or item.get("exchangeShortName") or ""
            if not symbol or "." in symbol or exchange.upper() not in ("NASDAQ", "NYSE", "AMEX", "NYSE ARCA", "NYSEARCA"):
                continue
            results.append({"symbol": symbol, "name": item.get("name"), "exchange": exchange})
        return results
