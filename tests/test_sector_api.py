"""
Market ingestion, sector comparison, reports and API (offline, isolated DB).

Provider clients are test-only fakes (tests/market_fixtures.py) and Groq is
the deterministic fake from research_v2_support. No network is used.
"""
import io
import json
import os
import zipfile

import pytest

from market_fixtures import US_TECH, us_clients
from research_v2_support import (  # noqa: F401
    FakeGroqClient,
    completion,
    db_rows,
    isolated_runtime,
)


def seed_us_tech(clients=None):
    from app.services.market_ingestion import refresh_company

    clients = clients or us_clients()
    for symbol in US_TECH:
        refresh_company("US", symbol, ("profile", "quote", "statements", "news"), clients=clients)
    return clients


def groq_narrative(kwargs, n, mutate=None):
    payload = json.loads(kwargs["messages"][-1]["content"])["comparison"]
    companies = []
    for c in payload["companies"]:
        metric = next(iter(c["metrics"]))
        companies.append({
            "symbol": c["symbol"],
            "why_it_ranks": (
                f"{c['name']} is ranked {c['rank']} with a score of {c['score']}. "
                "Its position reflects relative metric scores within this sector."
                if c["ranked"] else f"{c['name']} was not ranked because data coverage is too low."
            ),
            "strengths": [f"Relative standing on {metric}"],
            "weaknesses": [],
        })
    body = {
        "overview": (
            "The companies in this sector were compared on profitability, growth, returns, "
            "leverage, valuation and risk metrics. Scores are relative positions within the "
            "sector and the leader combines strong profitability with solid growth on the "
            "available data. Some metrics may be imputed where provider data was missing."
        ),
        "companies": companies,
    }
    if mutate:
        mutate(body)
    return completion(json.dumps(body))


@pytest.fixture
def fake_groq(monkeypatch):
    from app.services import groq_structured

    holder = {"mutate": None}
    client = FakeGroqClient(lambda kw, n: groq_narrative(kw, n, holder["mutate"]))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TEST_ONLY")
    monkeypatch.setattr(groq_structured, "default_client", lambda timeout: client)
    client.holder = holder
    return client


# ----------------------------------------------------------- ingestion

def test_refresh_registers_and_stores_provider_data(isolated_runtime):
    from app.services import market_storage

    clients = seed_us_tech()
    company = market_storage.get_company("ORCL")
    assert company["market"] == "US" and company["sector_key"] == "technology"
    assert company["provider_sector"] == "Technology"
    quote = market_storage.latest_quote("AAPL")
    assert quote["provider"] == "FINNHUB" and quote["market_cap"] == str(int(US_TECH["AAPL"][8]))
    annual = market_storage.statement_values("AAPL", "annual")
    assert set(annual) == {"2024-12-31", "2025-12-31"}
    # Quarterly FMP statements were refused (plan restriction) but annual data was kept.
    assert any(c[0] == "statements" and c[2] == "quarter" for c in clients["fmp"].calls)
    news = db_rows(isolated_runtime.db, "SELECT provider, company_ticker FROM news_articles "
                   "WHERE company_ticker='ORCL'")
    assert news and news[0]["provider"] == "FINNHUB"
    # The six original companies' rows were not modified.
    original = db_rows(isolated_runtime.db, "SELECT name, cik FROM companies WHERE ticker='AAPL'")
    assert original == [{"name": "Apple Inc.", "cik": "0000320193"}]


def test_second_refresh_is_cached_and_does_not_duplicate(isolated_runtime):
    from app.services.market_ingestion import refresh_company

    clients = seed_us_tech()
    before = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM market_statements")
    outcome = refresh_company("US", "AAPL", ("quote", "statements", "news"), clients=clients)
    assert {r["status"] for r in outcome["results"].values()} == {"cached_fresh"}
    forced = refresh_company("US", "AAPL", ("statements", "news"), force=True, clients=clients)
    assert forced["results"]["statements"]["records_inserted"] == 0
    assert forced["results"]["news"]["records_inserted"] == 0
    assert db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM market_statements") == before


def test_provider_failure_is_reported_and_logged(isolated_runtime):
    from app.services.market_ingestion import refresh_company

    outcome = refresh_company("US", "NVDA", ("statements",), clients=us_clients(fail={"NVDA"}))
    result = outcome["results"]["statements"]
    assert result["status"] == "failed" and "HTTP 503" in result["message"]
    row = db_rows(isolated_runtime.db, "SELECT status, error_message FROM source_refreshes "
                  "WHERE company_ticker='NVDA' AND source='FMP_STATEMENTS' ORDER BY id DESC LIMIT 1")[0]
    assert row["status"] == "failed"


def test_missing_keys_are_reported_as_not_configured(isolated_runtime):
    from app.services.market_ingestion import refresh_company

    outcome = refresh_company("US", "AAPL", ("quote", "statements"), clients={})
    assert {r["status"] for r in outcome["results"].values()} == {"not_configured"}


def test_unknown_company_requires_name(isolated_runtime):
    from app.services import market_registry
    from app.services.market_ingestion import refresh_company

    with pytest.raises(market_registry.RegistryError):
        refresh_company("US", "ZZZZ", ("quote",), clients=us_clients())


# --------------------------------------------------------- comparison

def test_comparison_metrics_are_computed_from_statements(isolated_runtime):
    from app.services.sector_comparison import company_metrics

    seed_us_tech()
    metrics, inputs = company_metrics("MSFT")
    assert round(metrics["revenue_growth_pct"], 2) == round((280 - 245) / 245 * 100, 2)
    assert round(metrics["net_margin_pct"], 2) == round(100 / 280 * 100, 2)
    assert round(metrics["roe_pct"], 2) == round(100 / ((300 + 260) / 2) * 100, 2)
    assert round(metrics["debt_to_equity"], 4) == round(60 / 300, 4)
    assert metrics["pe_ratio"] == 35.0
    assert inputs["fiscal_period_end"] == "2025-12-31" and inputs["roe_basis"] == "average equity"


def test_build_comparison_ranks_explains_and_saves(isolated_runtime, fake_groq):
    from app.services import sector_comparison

    seed_us_tech()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=False)
    ranking = snapshot["ranking"]
    ranked = [c for c in ranking["companies"] if c["ranked"]]
    assert ranking["status"] == "ranked" and len(ranked) == 4
    assert [c["rank"] for c in ranked] == sorted(c["rank"] for c in ranked)
    # Universe members without data (ADBE, CSCO) are listed but not ranked.
    unranked = {c["symbol"] for c in ranking["companies"] if not c["ranked"]}
    assert unranked == {"ADBE", "CSCO"}
    assert snapshot["narrative"]["status"] == "completed", snapshot["narrative"]["withheld"]
    assert snapshot["narrative"]["companies"]["MSFT"]["source"] == "ai"
    assert len(fake_groq.requests) == 1
    path = isolated_runtime.root / "reports" / "sector_comparisons" / f"{snapshot['comparison_id']}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["comparison_id"] == snapshot["comparison_id"]


def test_invalid_ai_text_falls_back_per_company(isolated_runtime, fake_groq):
    from app.services import sector_comparison

    def mutate(body):
        body["companies"][0]["why_it_ranks"] = (
            "Its revenue grew 999% because of a new product, so investors should buy the stock."
        )
        body["companies"].append({"symbol": "FAKE", "why_it_ranks": "x " * 20,
                                  "strengths": [], "weaknesses": []})

    fake_groq.holder["mutate"] = mutate
    seed_us_tech()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=False)
    narrative = snapshot["narrative"]
    assert narrative["status"] == "partial"
    withheld = {w["item"] for w in narrative["withheld"]}
    assert "FAKE" in withheld
    first = snapshot["ranking"]["companies"][0]["symbol"]
    assert narrative["companies"][first]["source"] == "deterministic"
    assert narrative["companies"][first]["why_it_ranks"].startswith("Ranked 1")


def test_groq_failure_gives_deterministic_comparison(isolated_runtime, monkeypatch):
    from app.services import groq_structured, sector_comparison

    client = FakeGroqClient(lambda kw, n: completion("{bad json"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TEST_ONLY")
    monkeypatch.setattr(groq_structured, "default_client", lambda timeout: client)
    seed_us_tech()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=False)
    assert snapshot["narrative"]["status"] == "unavailable"
    assert snapshot["narrative"]["overview_source"] == "deterministic"
    assert snapshot["ranking"]["status"] == "ranked"
    assert len(client.requests) == 2  # bounded: full + reduced profile


def test_sector_without_data_is_rejected(isolated_runtime, fake_groq):
    from app.services import sector_comparison

    with pytest.raises(sector_comparison.ComparisonNoData):
        sector_comparison.build_comparison("IN", "pharma", refresh=False)


# ------------------------------------------------------------------ API

@pytest.fixture
def api(isolated_runtime):
    from fastapi.testclient import TestClient

    from app.main import app

    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        yield client, headers


PROTECTED = [
    "/api/v1/markets",
    "/api/v1/markets/US/top-companies",
    "/api/v1/markets/US/sectors",
    "/api/v1/markets/US/companies?search=app",
    "/api/v1/markets/US/companies/AAPL",
    "/api/v1/markets/US/sectors/technology/comparison",
]


@pytest.mark.parametrize("path", PROTECTED)
def test_market_routes_require_auth(api, path):
    client, _ = api
    assert client.get(path).status_code == 401


def test_markets_sectors_and_search(api):
    client, headers = api
    markets = client.get("/api/v1/markets", headers=headers).json()["markets"]
    assert {m["id"] for m in markets} == {"US", "IN"}
    sectors = client.get("/api/v1/markets/IN/sectors?search=pha", headers=headers).json()["sectors"]
    assert [s["key"] for s in sectors] == ["pharma"]
    all_sectors = client.get("/api/v1/markets/US/sectors", headers=headers).json()["sectors"]
    assert {"technology", "banking", "pharma", "energy", "automobile", "retail", "fmcg"} <= {
        s["key"] for s in all_sectors}
    found = client.get("/api/v1/markets/IN/companies?search=infos", headers=headers).json()["results"]
    assert found[0]["symbol"] == "INFY.NS"
    assert client.get("/api/v1/markets/XX/sectors", headers=headers).status_code == 404
    assert client.get("/api/v1/markets/US/sectors/space/comparison", headers=headers).status_code == 404
    assert client.get("/api/v1/markets/US/companies/%3Cbad%3E", headers=headers).status_code == 422


def test_top_companies_by_market_cap(api):
    client, headers = api
    assert client.get("/api/v1/markets/US/top-companies", headers=headers).json()["companies"] == []
    seed_us_tech()
    top = client.get("/api/v1/markets/US/top-companies?limit=3", headers=headers).json()
    assert [c["symbol"] for c in top["companies"]] == ["MSFT", "AAPL", "NVDA"]


def test_company_profile_endpoint(api):
    client, headers = api
    seed_us_tech()
    body = client.get("/api/v1/markets/US/companies/ORCL", headers=headers).json()
    assert body["sector_key"] == "technology" and body["quote"]["provider"] == "FINNHUB"
    assert body["metrics"]["net_margin_pct"] is not None
    assert body["news"] and body["freshness"]["statements"]["state"] == "fresh"
    assert body["research_profile"] == "provider"
    unknown = client.get("/api/v1/markets/US/companies/ZZZZ", headers=headers)
    assert unknown.status_code == 404


def test_comparison_endpoints_and_reports(api, fake_groq):
    client, headers = api
    seed_us_tech()
    assert client.get("/api/v1/markets/US/sectors/technology/comparison", headers=headers).status_code == 404
    created = client.post("/api/v1/markets/US/sectors/technology/comparison",
                          json={"refresh": False}, headers=headers)
    assert created.status_code == 200, created.text
    snapshot = created.json()
    latest = client.get("/api/v1/markets/US/sectors/technology/comparison", headers=headers).json()
    assert latest["comparison_id"] == snapshot["comparison_id"]

    # Reading and downloading never calls Groq again.
    calls = len(fake_groq.requests)
    pdf = client.get(f"/api/v1/sector-comparisons/{snapshot['comparison_id']}/report?format=pdf",
                     headers=headers)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and len(pdf.content) > 3000
    assert "attachment" in pdf.headers["content-disposition"]
    docx = client.get(f"/api/v1/sector-comparisons/{snapshot['comparison_id']}/report?format=docx",
                      headers=headers)
    assert docx.status_code == 200
    with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Methodology" in xml and "MSFT" in xml
    assert len(fake_groq.requests) == calls
    assert client.get(f"/api/v1/sector-comparisons/{snapshot['comparison_id']}/report?format=xls",
                      headers=headers).status_code == 422
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/api/v1/sector-comparisons/{missing}/report", headers=headers).status_code == 404
    history = client.get("/api/v1/markets/US/sectors/technology/comparisons", headers=headers).json()
    assert history["comparisons"][0]["comparison_id"] == snapshot["comparison_id"]


def test_comparison_without_data_returns_409(api, fake_groq):
    client, headers = api
    response = client.post("/api/v1/markets/IN/sectors/telecom/comparison",
                           json={"refresh": False}, headers=headers)
    assert response.status_code == 409
    assert "No stored financial data" in response.json()["detail"]


def test_request_validation(api):
    client, headers = api
    assert client.post("/api/v1/markets/US/sectors/technology/comparison",
                       json={"refresh": False, "extra": 1}, headers=headers).status_code == 422
    assert client.post("/api/v1/markets/US/companies/search", json={"q": ""},
                       headers=headers).status_code == 422
    assert client.post("/api/v1/markets/US/companies/AAPL/refresh",
                       json={"sources": ["prices"]}, headers=headers).status_code == 422


def test_rejected_finnhub_key_falls_back_to_fmp_quote(isolated_runtime):
    from app.services import market_storage
    from app.services.market_ingestion import refresh_company
    from app.services.market_providers.common import ProviderAccessDenied
    from market_fixtures import FakeFmp

    class RejectingFinnhub:
        def quote(self, symbol):
            raise ProviderAccessDenied("FINNHUB rejected the request (HTTP 401).")

    class FmpWithPrice(FakeFmp):
        def profile(self, symbol):
            return {**super().profile(symbol), "price": "250.5", "market_cap": "3600000000000", "beta": "1.1"}

    outcome = refresh_company("US", "AAPL", ("quote",), clients={"finnhub": RejectingFinnhub(), "fmp": FmpWithPrice()})
    assert outcome["results"]["quote"]["status"] == "refreshed"
    quote = market_storage.latest_quote("AAPL")
    assert quote["provider"] == "FMP" and quote["market_cap"] == "3600000000000"

    # Without FMP the Finnhub rejection is reported, not hidden.
    failed = refresh_company("US", "MSFT", ("quote",), clients={"finnhub": RejectingFinnhub()})
    assert failed["results"]["quote"]["status"] == "failed"
    assert "401" in failed["results"]["quote"]["message"]
