"""
TEST-ONLY market data fixtures.

Values below are invented test inputs shaped like provider responses (FMP,
Finnhub, Yahoo Finance). They exist only to exercise the code offline and
are never written anywhere except isolated test databases.
"""

from decimal import Decimal

# symbol: (revenue_t, revenue_t-1, net_income, operating_income, equity, equity_prev,
#          assets, debt, market_cap, pe, pb, dividend_yield_pct, beta)
US_TECH = {
    "AAPL": (400, 380, 100, 120, 70, 60, 360, 100, 3500e9, 32.0, 50.0, 0.4, 1.2),
    "MSFT": (280, 245, 100, 125, 300, 260, 520, 60, 3600e9, 35.0, 11.0, 0.7, 0.9),
    "NVDA": (130, 60, 70, 80, 90, 45, 140, 10, 3300e9, 50.0, 40.0, 0.03, 1.7),
    "ORCL": (57, 53, 12, 17, 9, 6, 160, 95, 500e9, 40.0, 50.0, 1.0, 1.0),
}
BILLION = Decimal(1_000_000_000)


def fmp_statement_rows(symbol, period="annual"):
    rev, rev_prev, ni, oi, eq, eq_prev, assets, debt, *_ = US_TECH[symbol]
    rows = []
    for end, revenue, equity in (("2025-12-31", rev, eq), ("2024-12-31", rev_prev, eq_prev)):
        values = {
            ("income", "revenue"): revenue,
            ("income", "net_income"): ni if end.startswith("2025") else ni * 0.9,
            ("income", "operating_income"): oi if end.startswith("2025") else oi * 0.9,
            ("balance", "total_equity"): equity,
            ("balance", "total_assets"): assets,
            ("balance", "total_debt"): debt,
            ("cash_flow", "operating_cash_flow"): ni * 1.2,
        }
        for (statement, metric), value in values.items():
            rows.append({
                "symbol": symbol, "provider": "FMP", "statement": statement,
                "period_type": "annual" if period == "annual" else "quarterly",
                "period_end": end, "fiscal_year": end[:4], "metric": metric,
                "value_text": str(int(Decimal(str(value)) * BILLION)), "currency": "USD",
                "source_field": f"test.{metric}",
            })
    return rows


class FakeFmp:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []

    def profile(self, symbol):
        self.calls.append(("profile", symbol))
        return {"name": f"{symbol} Test Corp", "exchange": "NASDAQ", "currency": "USD",
                "sector": "Technology", "industry": "Software—Infrastructure", "cik": "0000000001"}

    def statements(self, symbol, period="annual", limit=5):
        self.calls.append(("statements", symbol, period))
        from app.services.market_providers.common import ProviderAccessDenied

        if symbol in self.fail:
            from app.services.market_providers.common import ProviderError
            raise ProviderError("FMP is busy or unavailable (HTTP 503).")
        if period != "annual":
            raise ProviderAccessDenied("FMP rejected the request (HTTP 402).")
        return fmp_statement_rows(symbol)

    def search(self, query):
        return [{"symbol": "ORCL", "name": "Oracle Corporation", "exchange": "NYSE"}]


class FakeFinnhub:
    def __init__(self):
        self.calls = []

    def quote(self, symbol):
        self.calls.append(("quote", symbol))
        *_, cap, pe, pb, dy, beta = US_TECH[symbol]
        return {"price": "100", "market_cap": str(int(cap)), "pe_ratio": str(pe),
                "pb_ratio": str(pb), "dividend_yield_pct": str(dy), "beta": str(beta),
                "currency": "USD", "as_of": "2026-11-20T21:00:00+00:00",
                "source_fields": {"price": "quote.c"}}

    def profile(self, symbol):
        return {"name": f"{symbol} Inc.", "sector": "Technology", "industry": "Technology"}

    def company_news(self, symbol, days=14):
        self.calls.append(("news", symbol))
        return [{
            "provider_article_id": f"finnhub:{symbol}-1",
            "title": f"{symbol} Test Corp expands cloud business partnership",
            "description": f"{symbol} Test Corp announced a cloud partnership with a customer.",
            "article_url": f"https://example.com/{symbol.lower()}-cloud",
            "source_name": "Example Wire",
            "published_at": "2026-11-21T10:00:00+00:00",
        }]

    def search(self, query):
        return [{"symbol": "ORCL", "name": "ORACLE CORP", "exchange": "US"}]


def us_clients(fail=()):
    return {"fmp": FakeFmp(fail), "finnhub": FakeFinnhub()}


# ---------------------------------------------------------------- India (yfinance-shaped)

def yf_rows(symbol, quarters=6, start_year=2024, annual_ocf=True, revenue_base=2_000):
    """Quarterly revenue (INR, crores scaled to units) plus annual figures."""
    rows = []
    ends = []
    year, quarter_ends = start_year, ["03-31", "06-30", "09-30", "12-31"]
    index = 0
    while len(ends) < quarters:
        ends.append(f"{year}-{quarter_ends[index % 4]}")
        index += 1
        if index % 4 == 0:
            year += 1
    for i, end in enumerate(ends):
        value = (revenue_base + 40 * i) * 1_000_000_000
        rows.append({"symbol": symbol, "provider": "YFINANCE", "statement": "income",
                     "period_type": "quarterly", "period_end": end, "fiscal_year": end[:4],
                     "metric": "revenue", "value_text": str(value), "currency": "INR",
                     "source_field": "quarterly_income_stmt['Total Revenue']"})
    for year_end, revenue, net, ocf, equity, assets, debt in (
        ("2024-03-31", 8_000, 900, 1_100, 7_000, 17_000, 3_000),
        ("2025-03-31", 8_600, 980, 1_250, 7_600, 18_000, 3_100),
    ):
        for metric, value in (("revenue", revenue), ("net_income", net),
                              ("total_equity", equity), ("total_assets", assets),
                              ("total_debt", debt)) + ((("operating_cash_flow", ocf),) if annual_ocf else ()):
            rows.append({"symbol": symbol, "provider": "YFINANCE",
                         "statement": "cash_flow" if metric == "operating_cash_flow" else "income",
                         "period_type": "annual", "period_end": year_end, "fiscal_year": year_end[:4],
                         "metric": metric, "value_text": str(value * 1_000_000_000), "currency": "INR",
                         "source_field": f"test.{metric}"})
    return rows


class FakeYF:
    def __init__(self, quarters=6):
        self.quarters = quarters

    def profile_and_quote(self, symbol):
        profile = {"name": f"{symbol.split('.')[0].title()} Industries Ltd", "exchange": "NSI",
                   "currency": "INR", "sector": "Energy", "industry": "Oil & Gas Refining & Marketing"}
        quote = {"price": "1450.5", "market_cap": "19600000000000", "pe_ratio": "24.1",
                 "pb_ratio": "2.3", "dividend_yield_pct": "0.34", "beta": "0.6", "currency": "INR",
                 "as_of": None, "source_fields": {}}
        return profile, quote

    def statements(self, symbol, currency=None):
        return yf_rows(symbol, quarters=self.quarters)

    def search(self, query):
        return []


def india_clients(quarters=6):
    return {"yfinance": FakeYF(quarters)}
