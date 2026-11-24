"""
Provider adapters against recorded-shape TEST payloads (offline).

httpx.MockTransport answers requests in memory, so the real adapter code
(headers, parsing, retries, error mapping) runs without any network.
"""
import logging

import httpx
import pandas as pd
import pytest

from app.services.market_providers import common
from app.services.market_providers.finnhub_client import FinnhubClient
from app.services.market_providers.fmp_client import FmpClient
from app.services.market_providers.yfinance_client import YFinanceClient

KEY = "TEST_ONLY_PROVIDER_KEY_123"


def transport(routes, seen):
    def handler(request):
        seen.append(request)
        for fragment, response in routes.items():
            if fragment in request.url.path:
                status, body = response() if callable(response) else response
                return httpx.Response(status, json=body, headers={"retry-after": "0"})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def no_usage(provider):
    pass


class NoWait:
    def acquire(self):
        pass


FAST = NoWait()


def finnhub(routes, seen):
    return FinnhubClient(api_key=KEY, http=httpx.Client(transport=transport(routes, seen)),
                         sleep=lambda s: None, usage_hook=no_usage, limiter=FAST)


def fmp(routes, seen, budget_used=0):
    return FmpClient(api_key=KEY, http=httpx.Client(transport=transport(routes, seen)),
                     sleep=lambda s: None, usage_hook=no_usage, usage_count=lambda p: budget_used,
               limiter=FAST)


def test_decimal_text_is_exact():
    assert common.to_decimal_text(394328000000) == "394328000000"
    assert common.to_decimal_text(0.1) == "0.1"
    assert common.to_decimal_text("1.50") == "1.5"
    assert common.to_decimal_text(float("nan")) is None
    assert common.to_decimal_text(None) is None and common.to_decimal_text(True) is None


def test_finnhub_quote_normalised_and_key_in_header_only():
    seen = []
    client = finnhub({
        "/quote": (200, {"c": 230.5, "t": 1763700000}),
        "/stock/metric": (200, {"metric": {"marketCapitalization": 3500000.5, "peTTM": 35.2,
                                           "pbQuarterly": 50.1, "dividendYieldIndicatedAnnual": 0.44,
                                           "beta": 1.21}}),
    }, seen)
    quote = client.quote("AAPL")
    assert quote["price"] == "230.5"
    assert quote["market_cap"] == "3500000500000"  # millions -> absolute
    assert quote["pe_ratio"] == "35.2" and quote["dividend_yield_pct"] == "0.44"
    assert all(r.headers["X-Finnhub-Token"] == KEY for r in seen)
    assert all(KEY not in str(r.url) for r in seen)


def test_finnhub_unknown_symbol_is_no_data():
    with pytest.raises(common.ProviderNoData):
        finnhub({"/quote": (200, {"c": 0})}, []).quote("ZZZZ")


def test_finnhub_news_filters_bad_items():
    seen = []
    client = finnhub({"/company-news": (200, [
        {"id": 1, "headline": "Apple expands services", "url": "https://example.com/a",
         "source": "Wire", "datetime": 1763700000, "summary": "Apple said..."},
        {"id": 2, "headline": "", "url": "https://example.com/b"},
    ])}, seen)
    news = client.company_news("AAPL")
    assert len(news) == 1 and news[0]["provider_article_id"] == "finnhub:1"
    assert news[0]["published_at"].startswith("2025-") or news[0]["published_at"].startswith("2026-")


def test_fmp_statements_map_to_canonical_metrics():
    seen = []
    record = {"date": "2025-09-27", "fiscalYear": "2025", "reportedCurrency": "USD",
              "revenue": 416161000000, "operatingIncome": 133050000000, "netIncome": 112010000000,
              "totalAssets": 359241000000, "totalStockholdersEquity": 73733000000, "totalDebt": 98657000000,
              "operatingCashFlow": 111482000000}
    client = fmp({"income-statement": (200, [record]), "balance-sheet-statement": (200, [record]),
                  "cash-flow-statement": (200, [record])}, seen)
    rows = client.statements("AAPL", "annual")
    metrics = {r["metric"]: r["value_text"] for r in rows}
    assert metrics == {
        "revenue": "416161000000", "operating_income": "133050000000", "net_income": "112010000000",
        "total_assets": "359241000000", "total_equity": "73733000000", "total_debt": "98657000000",
        "operating_cash_flow": "111482000000",
    }
    assert {r["period_end"] for r in rows} == {"2025-09-27"}
    assert all(r.headers["apikey"] == KEY and KEY not in str(r.url) for r in seen)


def test_fmp_daily_budget_blocks_requests():
    seen = []
    with pytest.raises(common.ProviderQuotaExceeded):
        fmp({}, seen, budget_used=10_000).profile("AAPL")
    assert seen == []


def test_fmp_plan_restriction_is_access_denied():
    with pytest.raises(common.ProviderAccessDenied):
        fmp({"income-statement": (402, {"Error Message": "Premium endpoint"})}, []).statements("AAPL", "quarter")


def test_http_429_retries_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return (429, {}) if calls["n"] == 1 else (200, [{"companyName": "Apple Inc.", "cik": "320193"}])

    profile = fmp({"/profile": flaky}, []).profile("AAPL")
    assert profile["name"] == "Apple Inc." and profile["cik"] == "0000320193"
    assert calls["n"] == 2


def test_persistent_5xx_fails_bounded_without_leaking(caplog):
    caplog.set_level(logging.DEBUG)
    seen = []
    with pytest.raises(common.ProviderError) as info:
        fmp({"/profile": (503, {"detail": KEY})}, seen).profile("AAPL")
    assert len(seen) == 3  # max_attempts
    assert KEY not in info.value.safe_message and KEY not in caplog.text


def test_missing_key_is_not_configured(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "finnhub_api_key", None)
    with pytest.raises(common.ProviderNotConfigured):
        FinnhubClient(usage_hook=no_usage)


# ------------------------------------------------------------- yfinance

class FakeTicker:
    def __init__(self, info=None, frames=None, fail=False):
        self._info, self._frames, self._fail = info or {}, frames or {}, fail

    @property
    def info(self):
        if self._fail:
            raise RuntimeError("Too Many Requests. Rate limited.")
        return self._info

    def __getattr__(self, name):
        if name in ("income_stmt", "balance_sheet", "cashflow", "quarterly_income_stmt",
                    "quarterly_balance_sheet", "quarterly_cashflow"):
            return self._frames.get(name, pd.DataFrame())
        raise AttributeError(name)


def yf_frames():
    cols = [pd.Timestamp("2025-03-31"), pd.Timestamp("2024-03-31")]
    return {
        "income_stmt": pd.DataFrame(
            {cols[0]: [2_550_000_000_000, 450_000_000_000, 480_000_000_000],
             cols[1]: [2_400_000_000_000, float("nan"), 460_000_000_000]},
            index=["Total Revenue", "Operating Income", "Net Income"]),
        "balance_sheet": pd.DataFrame(
            {cols[0]: [18_000_000_000_000, 8_000_000_000_000, 3_500_000_000_000]},
            index=["Total Assets", "Stockholders Equity", "Total Debt"]),
    }


def test_yfinance_profile_quote_and_statements():
    info = {"longName": "Reliance Industries Limited", "currency": "INR", "sector": "Energy",
            "industry": "Oil & Gas Refining & Marketing", "currentPrice": 1450.5,
            "marketCap": 19_600_000_000_000, "trailingPE": 24.1, "priceToBook": 2.3,
            "trailingAnnualDividendYield": 0.0034, "beta": 0.6, "exchange": "NSI"}
    client = YFinanceClient(ticker_factory=lambda s: FakeTicker(info, yf_frames()), usage_hook=no_usage, limiter=FAST)
    profile, quote = client.profile_and_quote("RELIANCE.NS")
    assert profile["name"] == "Reliance Industries Limited" and quote["currency"] == "INR"
    assert quote["dividend_yield_pct"] == "0.34" and quote["market_cap"] == "19600000000000"
    rows = client.statements("RELIANCE.NS", currency="INR")
    by_key = {(r["period_end"], r["metric"]): r["value_text"] for r in rows}
    assert by_key[("2025-03-31", "revenue")] == "2550000000000"
    assert ("2024-03-31", "operating_income") not in by_key  # NaN is missing, not zero
    assert all(r["period_type"] == "annual" for r in rows)


def test_yfinance_errors_are_wrapped():
    client = YFinanceClient(ticker_factory=lambda s: FakeTicker(fail=True), usage_hook=no_usage, limiter=FAST)
    with pytest.raises(common.ProviderError) as info:
        client.profile_and_quote("TCS.NS")
    assert "Too Many Requests" not in info.value.safe_message


def test_yfinance_search_keeps_indian_equities_only():
    quotes = [{"symbol": "TCS.NS", "longname": "Tata Consultancy Services", "quoteType": "EQUITY"},
              {"symbol": "TCS", "shortname": "US listing", "quoteType": "EQUITY"},
              {"symbol": "TCS.BO", "shortname": "TCS BSE", "quoteType": "EQUITY"}]
    client = YFinanceClient(search=lambda q: quotes, usage_hook=no_usage, limiter=FAST)
    assert [r["symbol"] for r in client.search("tcs")] == ["TCS.NS", "TCS.BO"]


def test_rate_limiter_spaces_requests():
    clock = {"t": 0.0}
    sleeps = []
    limiter = common.RateLimiter(60, clock=lambda: clock["t"],
                                 sleep=lambda s: (sleeps.append(s), clock.__setitem__("t", clock["t"] + s)))
    for _ in range(3):
        limiter.acquire()
    assert sleeps == [1.0, 1.0]
