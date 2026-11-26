"""
Market ingestion, sector comparison, reports and API (offline, isolated DB).

Provider clients are test-only fakes (tests/market_fixtures.py) and Azure OpenAI is
the deterministic fake from research_v2_support. No network is used.
"""
import io
import json
import os
import zipfile

import pytest

from market_fixtures import US_TECH, us_clients
from research_v2_support import (  # noqa: F401
    FakeAIClient,
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


def ai_narrative(kwargs, n, mutate=None):
    payload = json.loads(kwargs["input"][-1]["content"])["comparison"]
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
def fake_ai(monkeypatch):
    from app.services import ai_provider

    holder = {"mutate": None}
    client = FakeAIClient(lambda kw, n: ai_narrative(kw, n, holder["mutate"]))
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: client)
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


def test_build_comparison_ranks_explains_and_saves(isolated_runtime, fake_ai):
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
    assert len(fake_ai.requests) == 1
    from research_v2_support import get_doc

    path = isolated_runtime.root / "reports" / "sector_comparisons" / f"{snapshot['comparison_id']}.json"
    assert json.loads(get_doc(path))["comparison_id"] == snapshot["comparison_id"]


def test_invalid_ai_text_falls_back_per_company(isolated_runtime, fake_ai):
    from app.services import sector_comparison

    def mutate(body):
        body["companies"][0]["why_it_ranks"] = (
            "Its revenue grew 999% because of a new product, so investors should buy the stock."
        )
        body["companies"].append({"symbol": "FAKE", "why_it_ranks": "x " * 20,
                                  "strengths": [], "weaknesses": []})

    fake_ai.holder["mutate"] = mutate
    seed_us_tech()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=False)
    narrative = snapshot["narrative"]
    assert narrative["status"] == "partial"
    withheld = {w["item"] for w in narrative["withheld"]}
    assert "FAKE" in withheld
    first = snapshot["ranking"]["companies"][0]["symbol"]
    assert narrative["companies"][first]["source"] == "deterministic"
    assert narrative["companies"][first]["why_it_ranks"].startswith("Ranked 1")


def test_ai_failure_gives_deterministic_comparison(isolated_runtime, monkeypatch):
    from app.services import ai_provider, sector_comparison

    client = FakeAIClient(lambda kw, n: completion("{bad json"))
    monkeypatch.setattr(ai_provider, "default_client", lambda timeout=None: client)
    seed_us_tech()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=False)
    assert snapshot["narrative"]["status"] == "unavailable"
    assert snapshot["narrative"]["overview_source"] == "deterministic"
    assert snapshot["ranking"]["status"] == "ranked"
    assert len(client.requests) == 2  # bounded: full + reduced profile


def test_sector_without_data_is_rejected(isolated_runtime, fake_ai):
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


def test_comparison_endpoints_and_reports(api, fake_ai):
    client, headers = api
    seed_us_tech()
    assert client.get("/api/v1/markets/US/sectors/technology/comparison", headers=headers).status_code == 404
    created = client.post("/api/v1/markets/US/sectors/technology/comparison",
                          json={"refresh": False}, headers=headers)
    assert created.status_code == 200, created.text
    snapshot = created.json()
    latest = client.get("/api/v1/markets/US/sectors/technology/comparison", headers=headers).json()
    assert latest["comparison_id"] == snapshot["comparison_id"]

    # Reading and downloading never calls the AI provider again.
    calls = len(fake_ai.requests)
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
    assert len(fake_ai.requests) == calls
    assert client.get(f"/api/v1/sector-comparisons/{snapshot['comparison_id']}/report?format=xls",
                      headers=headers).status_code == 422
    missing = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/api/v1/sector-comparisons/{missing}/report", headers=headers).status_code == 404
    history = client.get("/api/v1/markets/US/sectors/technology/comparisons", headers=headers).json()
    assert history["comparisons"][0]["comparison_id"] == snapshot["comparison_id"]


def test_comparison_without_data_returns_409(api, fake_ai):
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


# ------------------------------------------- custom comparison (selected companies)

def _custom(client, headers, companies, refresh=False, sector="technology"):
    return client.post(f"/api/v1/markets/US/sectors/{sector}/comparison",
                       json={"refresh": refresh, "companies": companies}, headers=headers)


def test_custom_comparison_ranks_only_selected_companies(api, fake_ai):
    client, headers = api
    seed_us_tech()
    response = _custom(client, headers, ["MSFT", "ORCL"])
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["comparison_type"] == "custom"
    assert [c["symbol"] for c in snapshot["selected_companies"]] == ["MSFT", "ORCL"]
    assert {c["symbol"] for c in snapshot["companies"]} == {"MSFT", "ORCL"}
    ranking = snapshot["ranking"]
    assert {c["symbol"] for c in ranking["companies"]} == {"MSFT", "ORCL"}
    assert sorted(c["rank"] for c in ranking["companies"]) == [1, 2]
    assert ranking["method"]["universe"] == "selected"
    assert snapshot["relative_note"] == (
        "Scores are relative to the selected companies and may differ from the full sector ranking."
    )
    assert ranking["method"]["relative_note"] == snapshot["relative_note"]
    assert any("selected companies" in line for line in snapshot["methodology"])
    # The AI prompt is told the scores compare only the selection.
    prompt = json.loads(fake_ai.requests[-1]["input"][-1]["content"])
    assert "selected by the user" in prompt["task"]
    assert {c["symbol"] for c in prompt["comparison"]["companies"]} == {"MSFT", "ORCL"}


def test_custom_scores_are_normalised_over_the_selection(isolated_runtime, fake_ai):
    from app.services import sector_comparison

    seed_us_tech()
    full = sector_comparison.build_comparison("US", "technology", refresh=False)
    custom = sector_comparison.build_comparison("US", "technology", refresh=False,
                                                companies=["AAPL", "NVDA"])
    full_scores = {c["symbol"]: c["score"] for c in full["ranking"]["companies"]}
    custom_rows = {c["symbol"]: c for c in custom["ranking"]["companies"]}
    # With two companies every used metric is 0 or 100 (or 50 when equal).
    for row in custom_rows.values():
        assert all(b["normalized_score"] in (0.0, 50.0, 100.0) for b in row["breakdown"])
    assert any(custom_rows[s]["score"] != full_scores[s] for s in ("AAPL", "NVDA"))


def test_custom_comparison_accepts_maximum_and_reports_duplicates(api, fake_ai):
    client, headers = api
    seed_us_tech()
    six = ["AAPL", "MSFT", "NVDA", "ORCL", "ADBE", "CSCO"]
    response = _custom(client, headers, six)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["selected_companies"]) == 6
    # Members without stored data are listed and flagged, not ranked.
    unranked = {c["symbol"] for c in body["ranking"]["companies"] if not c["ranked"]}
    assert unranked == {"ADBE", "CSCO"}

    dupes = _custom(client, headers, ["msft", " MSFT ", "AAPL", "AAPL"])
    assert dupes.status_code == 200, dupes.text
    assert [c["symbol"] for c in dupes.json()["selected_companies"]] == ["MSFT", "AAPL"]
    assert dupes.json()["duplicates_removed"] == ["MSFT", "AAPL"]


def test_custom_comparison_validation_errors(api, fake_ai, monkeypatch, isolated_runtime):
    from app.core.config import get_settings

    client, headers = api
    seed_us_tech()
    too_few = _custom(client, headers, ["MSFT"])
    assert too_few.status_code == 400 and "at least 2" in too_few.json()["detail"]
    # Duplicates of one company count once.
    assert _custom(client, headers, ["MSFT", "msft"]).status_code == 400
    assert _custom(client, headers, []).status_code == 400

    outside = _custom(client, headers, ["MSFT", "JPM"])
    assert outside.status_code == 400
    assert "JPM" in outside.json()["detail"] and "Technology" in outside.json()["detail"]
    seven = _custom(client, headers, ["AAPL", "MSFT", "NVDA", "ORCL", "ADBE", "CSCO", "GOOGL"])
    assert seven.status_code == 400 and "GOOGL" in seven.json()["detail"]
    invalid = _custom(client, headers, ["MSFT", "<script>"])
    assert invalid.status_code == 400 and "Invalid company symbol" in invalid.json()["detail"]

    monkeypatch.setattr(get_settings(), "custom_comparison_max_companies", 3)
    too_many = _custom(client, headers, ["AAPL", "MSFT", "NVDA", "ORCL"])
    assert too_many.status_code == 400 and "at most 3" in too_many.json()["detail"]
    limits = client.get("/api/v1/markets/US/sectors", headers=headers).json()["custom_comparison"]
    assert limits == {"min_companies": 2, "max_companies": 3}

    # Malformed body is still a 422.
    assert client.post("/api/v1/markets/US/sectors/technology/comparison",
                       json={"companies": "MSFT"}, headers=headers).status_code == 422
    # No snapshot was written for rejected selections.
    from app.services import document_store

    saved = isolated_runtime.root / "reports" / "sector_comparisons"
    assert not document_store.list_paths(saved)


def test_custom_comparison_refreshes_only_selected_companies(isolated_runtime, fake_ai):
    from app.services import sector_comparison

    clients = us_clients()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=True, clients=clients,
                                                  companies=["NVDA", "ORCL"])
    touched = {call[1] for c in clients.values() for call in c.calls}
    assert touched == {"NVDA", "ORCL"}
    assert set(snapshot["refresh"]["outcomes"]) == {"NVDA", "ORCL"}
    # No news calls: a comparison needs profile, quote and statements only.
    assert not any(call[0] == "news" for call in clients["finnhub"].calls)


def test_custom_comparison_via_api_refreshes_only_selection(api, fake_ai, monkeypatch):
    from app.services import market_ingestion

    client, headers = api
    clients = us_clients()
    monkeypatch.setattr(market_ingestion, "default_clients", lambda market: (clients, {}))
    response = _custom(client, headers, ["AAPL", "MSFT"], refresh=True)
    assert response.status_code == 200, response.text
    assert {call[1] for c in clients.values() for call in c.calls} == {"AAPL", "MSFT"}


def test_latest_sector_comparison_ignores_custom_snapshots(api, fake_ai):
    client, headers = api
    seed_us_tech()
    sector = client.post("/api/v1/markets/US/sectors/technology/comparison",
                         json={"refresh": False}, headers=headers).json()
    custom = _custom(client, headers, ["MSFT", "ORCL"]).json()
    latest = client.get("/api/v1/markets/US/sectors/technology/comparison", headers=headers).json()
    assert latest["comparison_id"] == sector["comparison_id"]
    assert latest["comparison_type"] == "sector"
    history = client.get("/api/v1/markets/US/sectors/technology/comparisons", headers=headers).json()
    types = {h["comparison_id"]: h["comparison_type"] for h in history["comparisons"]}
    assert types == {sector["comparison_id"]: "sector", custom["comparison_id"]: "custom"}
    entry = next(h for h in history["comparisons"] if h["comparison_type"] == "custom")
    assert entry["selected_companies"] == ["MSFT", "ORCL"]
    # The saved custom snapshot can be fetched by ID.
    fetched = client.get(f"/api/v1/sector-comparisons/{custom['comparison_id']}", headers=headers).json()
    assert fetched["comparison_type"] == "custom"


def test_custom_comparison_reports(api, fake_ai):
    client, headers = api
    seed_us_tech()
    custom = _custom(client, headers, ["MSFT", "ORCL", "NVDA"]).json()
    base = f"/api/v1/sector-comparisons/{custom['comparison_id']}/report"
    pdf = client.get(base + "?format=pdf", headers=headers)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert "_technology_custom_" in pdf.headers["content-disposition"]
    docx = client.get(base + "?format=docx", headers=headers)
    assert docx.status_code == 200
    with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Custom comparison: Technology, 3 companies" in xml
    assert "Selected companies:" in xml
    for symbol in ("MSFT", "ORCL", "NVDA"):
        assert symbol in xml
    assert "Scores are relative to the selected companies and may differ from the full sector ranking." in xml
    assert "AAPL" not in xml


def test_old_snapshots_without_type_are_sector_comparisons(isolated_runtime, fake_ai):
    from app.services import sector_comparison, sector_report

    seed_us_tech()
    snapshot = sector_comparison.build_comparison("US", "technology", refresh=False)
    legacy = {k: v for k, v in snapshot.items() if k != "comparison_type"}
    assert sector_comparison.snapshot_type(legacy) == "sector"
    model = sector_report.report_model(legacy)
    assert model["title"].startswith("Technology sector comparison") and not model["custom"]
