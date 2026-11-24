"""
Source freshness, SEC / NewsData.io refresh and concurrency (offline).

Provider HTTP calls are replaced by test-only coroutines; everything runs on
an isolated copy of the database. Real providers are never contacted (the
network guard would block them anyway).
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from research_v2_support import db_rows, isolated_runtime  # noqa: F401

SECRET = "gsk_TEST_SECRET_IN_ERROR_MESSAGE"


def insert_refresh(db, ticker, source, status, completed_at, started_at=None):
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO source_refreshes(company_ticker, source, started_at, completed_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticker, source, started_at or completed_at, completed_at, status),
        )
        con.commit()
    finally:
        con.close()


def iso(delta_hours):
    return (datetime.now(timezone.utc) - timedelta(hours=delta_hours)).isoformat()


# ---------------------------------------------------------------- freshness

def test_freshness_states_follow_policy(isolated_runtime):
    from app.services.source_refresh import freshness_snapshot

    insert_refresh(isolated_runtime.db, "AAPL", "SEC", "success", iso(1))
    insert_refresh(isolated_runtime.db, "AAPL", "NEWSDATA", "success", iso(30))
    snap = freshness_snapshot("AAPL")
    assert snap["sec"]["state"] == "fresh"            # 1h < 24h policy
    assert snap["news"]["state"] == "stale"           # 30h > 6h policy
    assert snap["sec"]["latest_periodic_filing"]["form"] in ("10-Q", "10-K", "10-Q/A", "10-K/A")
    assert snap["sec"]["latest_financial_period_end"]["revenue"]
    assert snap["news"]["article_count"] > 0
    assert set(snap["forecasts"]) == {"revenue", "operating_cash_flow"}


def test_freshness_reports_failures_and_never_refreshed(isolated_runtime):
    import sqlite3

    from app.services.source_refresh import freshness_snapshot

    con = sqlite3.connect(str(isolated_runtime.db))
    con.execute("DELETE FROM source_refreshes WHERE company_ticker='NVDA'")  # isolated copy only
    con.commit()
    con.close()
    insert_refresh(isolated_runtime.db, "NVDA", "NEWSDATA", "failed", iso(0.5))
    snap = freshness_snapshot("NVDA")
    assert snap["sec"]["state"] == "never"
    assert snap["news"]["state"] == "never"
    assert snap["news"]["last_attempt_failed"] is True


def test_freshness_is_read_only(isolated_runtime, monkeypatch):
    from app.services import source_refresh

    def forbidden(*a, **k):
        raise AssertionError("provider call")

    monkeypatch.setattr(source_refresh, "_run_ingestion", forbidden)
    before = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM source_refreshes")
    source_refresh.freshness_snapshot("MSFT")
    after = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM source_refreshes")
    assert before == after


# ------------------------------------------------------------ refresh_one

@pytest.mark.parametrize("provider_status,expected", [
    ("success", "refreshed"),
    ("cached", "cached_fresh"),
    ("no_new_filings", "no_new_filings"),
])
def test_status_mapping(isolated_runtime, provider_status, expected):
    from app.services.source_refresh import refresh_one

    result = refresh_one(
        "MSFT", "sec",
        runner=lambda t, s, f: {"status": provider_status, "records_fetched": 3,
                                "records_inserted": 1 if expected == "refreshed" else 0},
    )
    assert result["status"] == expected
    assert result["provider_called"] is (expected != "cached_fresh")


def test_failure_is_reported_without_leaking_messages(isolated_runtime):
    from app.services.sec_ingestion import SecIngestionError
    from app.services.source_refresh import refresh_one

    def boom(t, s, f):
        raise SecIngestionError(f"upstream said {SECRET}")

    result = refresh_one("MSFT", "sec", runner=boom)
    assert result["status"] == "failed"
    assert result["error_category"] == "SecIngestionError"
    assert SECRET not in str(result)


def test_concurrent_refresh_is_blocked(isolated_runtime):
    from app.services.source_refresh import refresh_one

    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(t, s, f):
        calls.append(1)
        entered.set()
        release.wait(5)
        return {"status": "success"}

    worker = threading.Thread(target=lambda: refresh_one("AMZN", "news", runner=slow))
    worker.start()
    entered.wait(5)
    second = refresh_one("AMZN", "news", runner=slow)
    release.set()
    worker.join(5)
    assert second["status"] == "busy"
    assert len(calls) == 1


def test_running_row_from_another_process_blocks(isolated_runtime):
    from app.services.source_refresh import refresh_one

    insert_refresh(isolated_runtime.db, "META", "SEC", "running", None, started_at=iso(0.05))
    result = refresh_one("META", "sec", runner=lambda *a: pytest.fail("must not call"))
    assert result["status"] == "busy"

    # A stale 'running' row (crashed process) does not block forever.
    insert_refresh(isolated_runtime.db, "GOOGL", "SEC", "running", None, started_at=iso(3))
    ok = refresh_one("GOOGL", "sec", runner=lambda *a: {"status": "success"})
    assert ok["status"] == "refreshed"


def test_manual_cooldown_skips_provider(isolated_runtime):
    from app.services.source_refresh import refresh_one

    insert_refresh(isolated_runtime.db, "AAPL", "NEWSDATA", "success", iso(0.01))
    result = refresh_one("AAPL", "news", force=True, cooldown_seconds=600,
                         runner=lambda *a: pytest.fail("must not call"))
    assert result["status"] == "recently_refreshed"


def test_prepare_sources_policy(isolated_runtime):
    from app.services.source_refresh import SourceRefreshError, prepare_sources_for_research

    insert_refresh(isolated_runtime.db, "MSFT", "SEC", "success", iso(1))
    calls = []

    def runner(t, s, f):
        calls.append(s)
        raise RuntimeError("down")

    prep = prepare_sources_for_research("MSFT", "if_stale", "continue_with_cached", runner=runner)
    assert calls == ["news"]  # SEC is fresh: no call
    assert prep["outcomes"]["sec"]["evidence_state"] == "cached_within_policy"
    assert prep["outcomes"]["news"]["evidence_state"] == "refresh_failed_using_cached"

    skipped = prepare_sources_for_research("MSFT", "skip", runner=runner)
    assert {o["status"] for o in skipped["outcomes"].values()} == {"skipped_by_request"}
    assert calls == ["news"]

    with pytest.raises(SourceRefreshError):
        prepare_sources_for_research("MSFT", "if_stale", "fail", runner=runner)


# ---------------------------------------------------- SEC ingestion (mocked)

def submissions(accession, form="10-Q", filed="2026-10-28"):
    return {"filings": {"recent": {
        "form": ["8-K", form],
        "accessionNumber": ["0000000000-26-000001", accession],
        "filingDate": ["2026-11-01", filed],
        "reportDate": ["", "2026-09-30"],
    }}}


def companyfacts(observations):
    return {"cik": 789019, "facts": {"us-gaap": {"Revenues": {"units": {"USD": observations}}}}}


NEW_FACT = {
    "start": "2026-10-01", "end": "2026-12-31", "val": 81234000000,
    "accn": "0001193125-27-000001", "fy": 2027, "fp": "Q2", "form": "10-Q",
    "filed": "2027-01-28", "frame": "CY2026Q4",
}


def patch_sec(monkeypatch, routes):
    from app.services import sec_ingestion

    requested = []

    async def fake_get_json(self, client, url):
        requested.append(url)
        for fragment, payload in routes.items():
            if fragment in url:
                return payload
        raise AssertionError("unexpected SEC URL " + url)

    monkeypatch.setattr(sec_ingestion.SecClient, "get_json", fake_get_json)
    return requested


def test_sec_no_new_filings_skips_companyfacts(isolated_runtime, monkeypatch):
    import asyncio

    from app.services.sec_ingestion import refresh_sec_company

    # Make stored SEC evidence older than the 24h policy (isolated copy only).
    insert_refresh(isolated_runtime.db, "MSFT", "SEC", "success", iso(48))

    latest = db_rows(isolated_runtime.db, "SELECT accession FROM sec_facts WHERE company_ticker='MSFT' "
                     "AND form='10-Q' ORDER BY filed_at DESC LIMIT 1")[0]["accession"]
    requested = patch_sec(monkeypatch, {"submissions": submissions(latest)})
    before = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM sec_facts")

    result = asyncio.run(refresh_sec_company("MSFT", force_refresh=False))
    assert result["status"] == "no_new_filings"
    assert result["latest_filing"]["accession"] == latest
    assert all("companyfacts" not in u for u in requested)
    assert all("company_tickers" not in u for u in requested)  # stored CIK reused
    assert db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM sec_facts") == before
    last = db_rows(isolated_runtime.db, "SELECT status, records_inserted FROM source_refreshes "
                   "WHERE company_ticker='MSFT' AND source='SEC' ORDER BY id DESC LIMIT 1")[0]
    assert last == {"status": "success", "records_inserted": 0}


def test_sec_new_filing_ingests_and_deduplicates(isolated_runtime, monkeypatch):
    import asyncio

    from app.services.sec_ingestion import refresh_sec_company

    # Make stored SEC evidence older than the 24h policy (isolated copy only).
    insert_refresh(isolated_runtime.db, "MSFT", "SEC", "success", iso(48))

    patch_sec(monkeypatch, {
        "submissions": submissions(NEW_FACT["accn"], filed="2027-01-28"),
        "companyfacts": companyfacts([NEW_FACT]),
        # force_refresh re-verifies the ticker->CIK mapping from the registry
        "company_tickers": {"0": {"ticker": "MSFT", "cik_str": 789019,
                                  "title": "MICROSOFT CORP"}},
    })
    first = asyncio.run(refresh_sec_company("MSFT", force_refresh=False))
    assert first["status"] == "success" and first["records_inserted"] == 1

    second = asyncio.run(refresh_sec_company("MSFT", force_refresh=True))
    assert second["records_inserted"] == 0  # same fact is never stored twice

    rows = db_rows(isolated_runtime.db, "SELECT value_text, accession, form, filed_at FROM sec_facts "
                   "WHERE company_ticker='MSFT' AND accession=?", (NEW_FACT["accn"],))
    assert rows == [{"value_text": "81234000000", "accession": NEW_FACT["accn"],
                     "form": "10-Q", "filed_at": "2027-01-28"}]


def test_sec_failure_preserves_existing_facts(isolated_runtime, monkeypatch):
    import asyncio

    from app.services import sec_ingestion

    async def failing(self, client, url):
        raise sec_ingestion.SecIngestionError("SEC returned HTTP 503 after bounded retries.")

    monkeypatch.setattr(sec_ingestion.SecClient, "get_json", failing)
    before = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM sec_facts")
    with pytest.raises(sec_ingestion.SecIngestionError):
        asyncio.run(sec_ingestion.refresh_sec_company("MSFT", force_refresh=True))
    assert db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM sec_facts") == before
    last = db_rows(isolated_runtime.db, "SELECT status FROM source_refreshes WHERE "
                   "company_ticker='MSFT' AND source='SEC' ORDER BY id DESC LIMIT 1")[0]
    assert last["status"] == "failed"


# ---------------------------------------------------- NewsData.io (mocked)

def test_news_refresh_inserts_and_deduplicates_canonical_urls(isolated_runtime, monkeypatch):
    import asyncio

    from app.services import news_ingestion

    article = {
        "article_id": "test-article-1",
        "title": "Microsoft expands cloud data center capacity in Spain",
        "link": "https://example.com/msft-spain?utm_source=feed",
        "description": "Microsoft said the expansion supports enterprise customers.",
        "source_name": "Example Wire",
        "pubDate": "2026-11-23 09:00:00",
    }
    variant = dict(article, article_id="test-article-2",
                   link="https://example.com/msft-spain?utm_source=other")

    pages = [{"status": "success", "results": [article], "nextPage": None},
             {"status": "success", "results": [variant], "nextPage": None}]

    async def fake_page(self, client, params):
        return pages.pop(0)

    monkeypatch.setattr(news_ingestion.NewsDataClient, "get_page", fake_page)
    first = asyncio.run(news_ingestion.refresh_news_company("MSFT", 30, 2, force_refresh=True))
    assert first["records_inserted"] == 1
    second = asyncio.run(news_ingestion.refresh_news_company("MSFT", 30, 2, force_refresh=True))
    assert second["records_inserted"] == 0  # same story, different query string

    stored = db_rows(isolated_runtime.db, "SELECT title, article_url, source_name, published_at "
                     "FROM news_articles WHERE provider_article_id='test-article-1'")
    assert stored[0]["article_url"] == article["link"]
    assert stored[0]["source_name"] == "Example Wire"


def test_news_failure_does_not_corrupt_stored_news(isolated_runtime, monkeypatch):
    import asyncio

    from app.services import news_ingestion

    async def failing(self, client, params):
        raise news_ingestion.NewsIngestionError("NewsData.io network request failed.")

    monkeypatch.setattr(news_ingestion.NewsDataClient, "get_page", failing)
    before = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM news_articles")
    with pytest.raises(news_ingestion.NewsIngestionError):
        asyncio.run(news_ingestion.refresh_news_company("AAPL", 30, 2, force_refresh=True))
    assert db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM news_articles") == before


# ------------------------------------------------------------ forecast cache

def test_forecast_cache_hit_without_recompute_and_invalidation_on_input_change(
    isolated_runtime, monkeypatch,
):
    from app.services import forecast_cache, forecasting

    def no_training(*a, **k):
        raise AssertionError("forecast was recomputed")

    monkeypatch.setattr(forecasting, "forecast_financial_metric", no_training)
    hit = forecast_cache.forecast_financial_metric_cached("MSFT", "revenue")
    assert hit["cache_status"] == "hit"
    assert forecast_cache.cache_ttl_seconds() == 720 * 3600

    original = forecast_cache.fin.normalized_quarterly_financials

    def changed(ticker):
        data = original(ticker)
        data["revenue"][-1] = dict(data["revenue"][-1], value=str(int(data["revenue"][-1]["value"]) + 1))
        return data

    fingerprint = forecast_cache.source_fingerprint("MSFT", "revenue")
    monkeypatch.setattr(forecast_cache.fin, "normalized_quarterly_financials", changed)
    assert forecast_cache.source_fingerprint("MSFT", "revenue") != fingerprint
    _, status = forecast_cache.read_cached_forecast(
        "MSFT", "revenue", forecast_cache.source_fingerprint("MSFT", "revenue"))
    assert status == "source_or_code_changed"


# ------------------------------------------------------------------- API

@pytest.fixture
def api(isolated_runtime):
    from fastapi.testclient import TestClient

    import os

    from app.main import app

    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        yield client, headers


def test_freshness_endpoint_requires_auth_and_is_read_only(api, monkeypatch):
    from app.services import source_refresh

    client, headers = api
    monkeypatch.setattr(source_refresh, "_run_ingestion", lambda *a: pytest.fail("provider call"))
    assert client.get("/api/v1/companies/MSFT/freshness").status_code == 401
    response = client.get("/api/v1/companies/MSFT/freshness", headers=headers)
    assert response.status_code == 200
    assert response.json()["sec"]["policy_max_age_hours"] == 24
    assert client.get("/api/v1/companies/XYZ/freshness", headers=headers).status_code == 404


def test_refresh_endpoint_reports_each_source(api, monkeypatch):
    from app.services import source_refresh

    client, headers = api

    def runner(ticker, source, force):
        if source == "sec":
            return {"status": "no_new_filings", "latest_filing": {"accession": "x"}}
        raise RuntimeError("down")

    monkeypatch.setattr(source_refresh, "_run_ingestion", runner)
    response = client.post("/api/v1/companies/MSFT/refresh",
                           json={"sources": ["sec", "news"], "force": False}, headers=headers)
    assert response.status_code == 200
    results = response.json()["results"]
    sec_status = results["sec"]["status"]
    assert sec_status in ("no_new_filings", "cached_fresh")
    assert results["news"]["status"] in ("failed", "cached_fresh")
    assert client.post("/api/v1/companies/MSFT/refresh", json={"sources": ["ftp"]},
                       headers=headers).status_code == 422


def test_duplicate_research_job_rejected(api, isolated_runtime):
    from app.services import research_jobs

    client, headers = api
    research_jobs.initialize_job_storage()
    research_jobs.create_job(isolated_runtime.root, "NVDA")
    response = client.post("/api/v1/jobs", json={"ticker": "NVDA"}, headers=headers)
    assert response.status_code == 409
    bad = client.post("/api/v1/jobs", json={"ticker": "NVDA", "refresh": "always"}, headers=headers)
    assert bad.status_code == 422
    extra = client.post("/api/v1/jobs", json={"ticker": "NVDA", "foo": 1}, headers=headers)
    assert extra.status_code == 422
