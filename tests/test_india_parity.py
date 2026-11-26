"""
India/US parity: fiscal labels, fundamentals, forecast qualification,
market overview, logo resolution, currency-correct research evidence and the
new read-only API routes. Isolated database; test-only provider fakes.
"""
import json
import os
from decimal import Decimal

import pytest

from market_fixtures import india_clients, us_clients
from research_v2_support import isolated_runtime  # noqa: F401


def seed(market, symbol, clients):
    from app.services.market_ingestion import refresh_company

    return refresh_company(market, symbol, ("profile", "quote", "statements"), clients=clients)


def metric(data, key):
    return next(m for m in data["metrics"] if m["key"] == key)


# ------------------------------------------------------------ fiscal labels

def test_indian_fiscal_labels():
    from app.services import fiscal

    assert fiscal.period_label("2025-03-31", "IN") == "FY2025"
    assert fiscal.period_label("2025-03-31", "IN", "quarterly") == "Q4 FY2025"
    assert fiscal.period_label("2024-04-30", "IN", "quarterly") == "Q1 FY2025"
    assert fiscal.period_label("2024-12-31", "IN", "quarterly") == "Q3 FY2025"
    assert fiscal.period_label("2025-09-27", "US") == "FY ending 2025-09-27"


# ------------------------------------------------------------- fundamentals

def test_indian_fundamentals_are_computed_from_stored_inr_statements(isolated_runtime):
    from app.services import fundamentals

    seed("IN", "RELIANCE.NS", india_clients())
    data = fundamentals.compute("IN", "RELIANCE.NS")
    assert data["period_label"] == "FY2025" and data["statement_currency"] == "INR"
    assert data["quote_currency"] == "INR"
    assert metric(data, "net_margin_pct")["value"] == pytest.approx(980 / 8600 * 100, rel=1e-4)
    assert metric(data, "roe_pct")["value"] == pytest.approx(980 / ((7000 + 7600) / 2) * 100, rel=1e-4)
    assert metric(data, "debt_to_equity")["value"] == pytest.approx(3100 / 7600, rel=1e-4)
    assert metric(data, "revenue_growth_pct")["value"] == pytest.approx((8600 - 8000) / 8000 * 100, rel=1e-4)
    assert metric(data, "earnings_growth_pct")["value"] == pytest.approx((980 - 900) / 900 * 100, rel=1e-4)
    assert metric(data, "market_cap")["value"] == 19_600_000_000_000 and metric(data, "market_cap")["currency"] == "INR"
    # Missing inputs are never estimated.
    for key in ("ev_to_ebitda", "current_ratio", "interest_coverage", "free_cash_flow", "gross_margin_pct"):
        m = metric(data, key)
        assert m["available"] is False and m["value"] is None and m["reason"], key
    assert "EBITDA" in metric(data, "ev_to_ebitda")["reason"]


def test_ev_to_ebitda_requires_one_currency(isolated_runtime):
    from app.services import fundamentals, market_storage as storage

    seed("IN", "RELIANCE.NS", india_clients())
    extra = [{"symbol": "RELIANCE.NS", "provider": "YFINANCE", "statement": s, "period_type": "annual",
              "period_end": "2025-03-31", "fiscal_year": "2025", "metric": m, "value_text": str(v),
              "currency": "INR", "source_field": "test"} for s, m, v in (
        ("income", "ebitda", 1_500_000_000_000), ("balance", "cash", 400_000_000_000),
        ("balance", "current_assets", 5_000_000_000_000), ("balance", "current_liabilities", 4_000_000_000_000),
        ("balance", "inventory", 1_000_000_000_000), ("income", "operating_income", 1_300_000_000_000),
        ("income", "interest_expense", 260_000_000_000), ("cash_flow", "capital_expenditure", -700_000_000_000))]
    storage.insert_statement_rows(extra)
    data = fundamentals.compute("IN", "RELIANCE.NS")
    ev = metric(data, "ev_to_ebitda")
    expected = (Decimal("19600000000000") + Decimal("3100000000000") - Decimal("400000000000")) / Decimal("1500000000000")
    assert ev["available"] and ev["value"] == pytest.approx(float(expected), rel=1e-6)
    assert metric(data, "current_ratio")["value"] == pytest.approx(1.25)
    assert metric(data, "quick_ratio")["value"] == pytest.approx(1.0)
    assert metric(data, "interest_coverage")["value"] == pytest.approx(5.0)
    assert metric(data, "free_cash_flow")["value"] == pytest.approx(1_250e9 - 700e9)

    # A quote in another currency: no cross-currency multiple.
    storage.insert_quote("RELIANCE.NS", "YFINANCE", {"price": "17", "market_cap": "235000000000", "currency": "USD",
                                                     "as_of": None, "source_fields": {}})
    ev = metric(fundamentals.compute("IN", "RELIANCE.NS"), "ev_to_ebitda")
    assert ev["available"] is False and "USD" in ev["reason"] and "INR" in ev["reason"]


# ---------------------------------------------------- forecast qualification

def test_indian_forecast_is_unavailable_with_exact_reasons(isolated_runtime):
    from app.services import forecasting, market_forecasts

    seed("IN", "RELIANCE.NS", india_clients(quarters=6))
    result = market_forecasts.qualify("IN", "RELIANCE.NS")
    revenue = next(m for m in result["metrics"] if m["metric"] == "revenue")
    assert revenue["status"] == "unavailable" and revenue["forecast"] is None
    assert revenue["checks"]["contiguous_quarters"] == 6
    assert revenue["checks"]["required_for_validation"] == forecasting.MIN_TRAIN + forecasting.HORIZON
    assert any("Only 6 contiguous quarters" in r for r in revenue["reasons"])
    assert any("filing dates" in r for r in revenue["reasons"])
    # Genuine reported history for the chart (no interpolation, labels in Indian fiscal terms).
    assert len(revenue["quarterly"]) == 6 and revenue["quarterly"][0]["label"] == "Q4 FY2024"
    assert [p["label"] for p in revenue["annual"]] == ["FY2024", "FY2025"]
    # FY2025 quarters (2,040 + 2,080 + 2,120 + 2,160 = 8,400) vs annual 8,600: 2.3 % apart.
    fy2025 = next(r for r in revenue["checks"]["reconciliation"] if r["fiscal_period_end"] == "2025-03-31")
    assert fy2025["status"] == "mismatch" and fy2025["difference_pct"] == pytest.approx(200 / 8600 * 100, rel=1e-6)
    assert any("do not reconcile" in r for r in revenue["reasons"])


def test_reconciliation_detects_cumulative_quarterly_disclosures():
    from app.services.market_forecasts import _reconcile

    quarters = [{"period_end": e, "value": v} for e, v in
                (("2024-06-30", 100.0), ("2024-09-30", 210.0), ("2024-12-31", 330.0), ("2025-03-31", 440.0))]
    annual = [{"period_end": "2025-03-31", "value": 440.0}]
    assert _reconcile(quarters, annual)[0]["status"] == "cumulative_quarters"
    standalone = [{"period_end": e, "value": 110.0} for e in ("2024-06-30", "2024-09-30", "2024-12-31", "2025-03-31")]
    assert _reconcile(standalone, annual)[0]["status"] == "reconciled"


def test_non_contiguous_quarters_are_never_bridged(isolated_runtime):
    from app.services import market_forecasts, market_storage as storage

    seed("IN", "RELIANCE.NS", india_clients(quarters=4))
    storage.insert_statement_rows([{
        "symbol": "RELIANCE.NS", "provider": "YFINANCE", "statement": "income", "period_type": "quarterly",
        "period_end": "2026-03-31", "fiscal_year": "2026", "metric": "revenue", "value_text": "2300000000000",
        "currency": "INR", "source_field": "test"}])
    revenue = next(m for m in market_forecasts.qualify("IN", "RELIANCE.NS")["metrics"] if m["metric"] == "revenue")
    assert revenue["checks"]["quarters_stored"] == 5 and revenue["checks"]["contiguous_quarters"] == 1


# ------------------------------------------------------------ market overview

def test_market_overview_counts_are_from_the_database_and_isolated(isolated_runtime):
    from app.services import market_overview

    seed("IN", "RELIANCE.NS", india_clients())
    seed("US", "ORCL", us_clients())
    india = market_overview.overview("IN")
    us = market_overview.overview("US")
    assert india["currency"] == "INR" and us["currency"] == "USD"
    assert india["counts"]["stored_companies"] == 1 and india["counts"]["with_statements"] == 1
    assert [t["symbol"] for t in india["top_companies"]] == ["RELIANCE.NS"]
    assert all(t["symbol"] != "RELIANCE.NS" for t in us["top_companies"])
    assert india["counts"]["universe_companies"] == sum(s["universe_count"] for s in india["sectors"]) > 0
    assert sum(s["loaded_count"] for s in india["sectors"]) <= india["counts"]["stored_companies"]


# --------------------------------------------------------------------- logos

def test_logo_resolution_uses_only_the_companys_own_profile(isolated_runtime):
    from app.services import logos, market_storage as storage

    assert logos.website_domain("https://www.tcs.com/en-in") == "www.tcs.com"
    assert logos.website_domain("infosys.com") == "infosys.com"
    for bad in ("", "not a url", "ftp://x.com", "http://localhost/", "https://intranet.local/"):
        assert logos.website_domain(bad) is None, bad
    assert logos.provider_logo("https://static2.finnhub.io/file/publicdatany/finnhubimage/stock_logo/AAPL.png")
    assert logos.provider_logo("https://evil.example.com/logo.png") is None
    assert logos.provider_logo("http://static2.finnhub.io/x.png") is None

    storage.register_company(symbol="TCS.NS", market="IN", name="Tata Consultancy Services", currency="INR",
                             profile={"website": "https://www.tcs.com"})
    storage.register_company(symbol="TCSX.NS", market="IN", name="Tata Consultancy Services Clone", currency="INR")
    resolved = logos.resolve_many(["TCS.NS", "TCSX.NS", "bad symbol!"])
    assert [c["url"] for c in resolved["TCS.NS"]["candidates"]] == [
        "https://www.tcs.com/apple-touch-icon.png", "https://www.tcs.com/favicon.ico"]
    assert resolved["TCSX.NS"]["candidates"] == []  # no name-based matching
    assert "BAD SYMBOL!" not in resolved


# --------------------------------------------------------- research evidence

def test_indian_research_evidence_is_inr_with_indian_fiscal_labels(isolated_runtime):
    from app.agents import provider_financial as pf
    from app.services import evidence_package as packages
    from app.services import fundamentals

    seed("IN", "RELIANCE.NS", india_clients(quarters=6))
    records, choice, currency = pf.select_records("RELIANCE.NS")
    package = pf.build_package("RELIANCE.NS", records, currency, {}, market="IN",
                               fundamentals=fundamentals.compute("IN", "RELIANCE.NS"),
                               unavailable={"revenue": "Only 6 contiguous quarters."})
    assert currency == "INR" and package["market"] == "IN"
    quarter = next(r for r in package["records"] if r["period_end"] == "2025-03-31" and r["period_type"] == "quarterly")
    assert quarter["fiscal_label"] == "FY2025 Q4"
    rendered = " ".join(packages.render_observation(i) or "" for i in package["items"].values())
    assert "INR" in rendered and "USD" not in rendered and "$" not in rendered
    ratio_ids = [r["evidence_id"] for r in package["ratios"]]
    assert any("_ratio_net_margin_pct_" in i for i in ratio_ids) and all(i in package["items"] for i in ratio_ids)
    assert any("April to March" in l for l in package["limitations"])
    assert any("No statistical forecast for Revenue" in l for l in package["limitations"])
    prompt = json.dumps(pf.prompt_evidence(package, {"periods_per_metric": 8}))
    assert '"currency": "INR"' in prompt and "AAPL" not in prompt and "MSFT" not in prompt


# ---------------------------------------------------------------- API routes

def test_new_market_routes(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.security import LIMITER
    from app.main import app

    LIMITER.reset()
    seed("IN", "RELIANCE.NS", india_clients())
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        base = "/api/v1/markets/IN/companies/RELIANCE.NS"
        for path in ("/fundamentals", "/financials", "/forecasts"):
            assert client.get(base + path).status_code == 401
            response = client.get(base + path, headers=headers)
            assert response.status_code == 200, (path, response.text)
        assert client.get(base + "/financials", headers=headers).json()["currencies"] == ["INR"]
        assert client.get("/api/v1/markets/IN/companies/INFY.NS/fundamentals", headers=headers).status_code == 404
        overview = client.get("/api/v1/markets/IN/overview", headers=headers).json()
        assert overview["market"] == "IN" and overview["counts"]["stored_companies"] == 1
        assert client.get("/api/v1/markets/XX/overview", headers=headers).status_code == 404
        logos = client.get("/api/v1/logos?symbols=RELIANCE.NS,AAPL", headers=headers).json()["logos"]
        assert set(logos) == {"RELIANCE.NS", "AAPL"}


def test_announcement_dates_give_point_in_time_availability_without_forcing_a_forecast(isolated_runtime):
    from app.services import market_forecasts, market_storage as storage

    seed("IN", "RELIANCE.NS", india_clients(quarters=6))
    # Quarter ends 2024-03-31 .. 2025-06-30; results announced 2-6 weeks later.
    dates = ["2024-04-22", "2024-07-19", "2024-10-14", "2025-01-16", "2025-04-25", "2025-07-18", "2025-10-17"]
    storage.merge_company_profile("RELIANCE.NS", {"announcement_dates": dates,
                                                  "announcement_dates_source": "Yahoo Finance earnings calendar"})
    assert storage.get_company("RELIANCE.NS")["profile"]["announcement_dates"] == dates  # other keys kept
    revenue = next(m for m in market_forecasts.qualify("IN", "RELIANCE.NS")["metrics"] if m["metric"] == "revenue")
    checks = revenue["checks"]
    assert checks["announcement_dates_matched"] == 6
    assert checks["first_announced"]["2024-03-31"] == "2024-04-22"
    # Announcement dates are not filing records and do not prove the stored values
    # were the ones published then: no point-in-time / leakage-free claim.
    assert checks["filing_dates_available"] is False and checks["point_in_time_values"] is False
    assert any("matched for 6 of 6" in r and "cannot be established" in r for r in revenue["reasons"])
    # Still unavailable: genuine history is far below the existing gate (not weakened).
    assert revenue["status"] == "unavailable" and any("Only 6 contiguous quarters" in r for r in revenue["reasons"])

    storage.merge_company_profile("RELIANCE.NS", {"announcement_dates": dates[:3]})
    revenue = next(m for m in market_forecasts.qualify("IN", "RELIANCE.NS")["metrics"] if m["metric"] == "revenue")
    assert revenue["checks"]["announcement_dates_matched"] == 3
    assert any("matched for 3 of 6" in r for r in revenue["reasons"])


def test_announcement_matching_rules():
    from app.services.market_forecasts import _announcements

    quarters = [{"period_end": "2025-03-31"}, {"period_end": "2025-06-30"}]
    out = _announcements(quarters, ["2025-03-31", "2025-04-20", "2025-11-30"])
    assert out["dates"] == {"2025-03-31": "2025-04-20"}  # same-day and >120-day matches rejected
    assert out["all_dated"] is False


def test_announcement_dates_use_reported_events_in_exchange_time():
    import pandas as pd

    from app.services.market_providers.yfinance_client import YFinanceClient

    index = pd.DatetimeIndex(["2027-01-11 05:00", "2026-07-08 20:00", "2026-04-09 06:00"]).tz_localize("America/New_York")
    frame = pd.DataFrame({"EPS Estimate": [38.3, 37.3, 38.1], "Reported EPS": [None, 38.28, 37.92]}, index=index)

    class Ticker:
        def get_earnings_dates(self, limit=60):
            return frame

    client = YFinanceClient.__new__(YFinanceClient)
    client._ticker = lambda symbol: Ticker()
    client._call = lambda fn, what: fn()
    # Scheduled (no reported EPS) dropped; 20:00 New York on 8 July is 9 July in India.
    assert client.announcement_dates("TCS.NS") == ["2026-04-09", "2026-07-09"]
