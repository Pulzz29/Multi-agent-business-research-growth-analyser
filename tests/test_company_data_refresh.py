"""
Server-side freshness rules for company pages (offline, isolated DB).

The clock is mocked by passing ``now``; providers are test-only fakes that
count calls (tests/market_fixtures.py). No network is used.
"""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from market_fixtures import FakeFinnhub, FakeFmp, us_clients
from research_v2_support import isolated_runtime  # noqa: F401

T0 = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_state():
    from app.services import company_data

    company_data._INFLIGHT.clear()
    company_data._LAST_FORCE.clear()
    yield
    company_data._INFLIGHT.clear()
    company_data._LAST_FORCE.clear()


def purge_market_rows(db, symbol):
    """Remove one company's market data from the ISOLATED test copy only."""
    from research_v2_support import open_db

    con = open_db(db)
    try:
        for table in ("company_data_cache",):
            con.execute(f"DELETE FROM {table} WHERE symbol = ?", (symbol,))
        for table in ("market_quotes", "market_statements", "market_companies"):
            con.execute(f"DELETE FROM {table} WHERE symbol = ?", (symbol,))
        con.commit()
    finally:
        con.close()


def provider_calls(clients):
    return len(clients["fmp"].calls) + len(clients["finnhub"].calls)


def get(symbol="ORCL", mode="auto", clients=None, now=T0, **kw):
    from app.services.company_data import get_company_data

    return get_company_data("US", symbol, mode=mode, clients=clients, now=now, **kw)


# ------------------------------------------------------------ needs_refresh

def test_needs_refresh_rules_and_boundary():
    from app.services.data_freshness import needs_refresh

    ttl = 6
    assert needs_refresh(None, T0, ttl)[:2] == (True, "never_refreshed")
    assert needs_refresh("not-a-date", T0, ttl)[:2] == (True, "never_refreshed")
    assert needs_refresh((T0 - timedelta(hours=5, minutes=59)).isoformat(), T0, ttl)[:2] == (False, "fresh")
    # Invalid exactly TTL after the fetch (requirement: 24 h cache expires at 24:00:00).
    assert needs_refresh((T0 - timedelta(hours=5, minutes=59, seconds=59)).isoformat(), T0, ttl)[:2] == (False, "fresh")
    assert needs_refresh((T0 - timedelta(hours=6)).isoformat(), T0, ttl)[:2] == (True, "stale")
    assert needs_refresh((T0 - timedelta(hours=6, minutes=1)).isoformat(), T0, ttl)[:2] == (True, "stale")
    # Naive timestamps are UTC; other offsets are converted to UTC.
    naive = (T0 - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert needs_refresh(naive, T0, ttl)[:2] == (False, "fresh")
    ist = (T0 - timedelta(hours=7)).astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    assert needs_refresh(ist, T0, ttl)[:2] == (True, "stale")
    with pytest.raises(ValueError):
        needs_refresh(None, T0.replace(tzinfo=None), ttl)


def test_ttl_comes_from_environment(monkeypatch):
    from app.core.config import get_settings
    from app.services.data_freshness import configured_ttl_hours

    monkeypatch.setenv("DATA_REFRESH_TTL_HOURS", "0.5")
    get_settings.cache_clear()
    try:
        assert configured_ttl_hours() == 0.5
    finally:
        monkeypatch.delenv("DATA_REFRESH_TTL_HOURS")
        get_settings.cache_clear()
    assert configured_ttl_hours() == 24.0  # CACHE_TTL_HOURS default
    monkeypatch.setenv("CACHE_TTL_HOURS", "12")
    get_settings.cache_clear()
    try:
        assert configured_ttl_hours() == 12.0
    finally:
        monkeypatch.delenv("CACHE_TTL_HOURS")
        get_settings.cache_clear()


# ----------------------------------------------------------- auto decisions

def test_never_fetched_refreshes_and_records_utc_timestamp(isolated_runtime):
    clients = us_clients()
    data = get(clients=clients)
    info = data["data_refresh"]
    assert info["decision"] == "auto_refresh_new" and info["refreshed"] is True
    assert info["last_refreshed_at"] == T0.isoformat() and data["last_refreshed_at"].endswith("+00:00")
    assert provider_calls(clients) > 0
    assert data["quote"] and data["annual_statements"]


def test_recent_data_is_served_without_provider_calls(isolated_runtime):
    get(clients=us_clients())
    for offset in (timedelta(hours=23, minutes=59), timedelta(hours=23, minutes=59, seconds=59)):
        clients = us_clients()
        data = get(clients=clients, now=T0 + offset)
        assert data["data_refresh"]["decision"] == "served_from_cache"
        assert data["data_refresh"]["refreshed"] is False
        assert provider_calls(clients) == 0
        assert data["last_refreshed_at"] == T0.isoformat()


def test_older_than_ttl_refreshes(isolated_runtime):
    get(clients=us_clients())
    clients = us_clients()
    later = T0 + timedelta(hours=24)
    data = get(clients=clients, now=later)
    assert data["data_refresh"]["decision"] == "auto_refresh_stale"
    assert provider_calls(clients) > 0 and data["last_refreshed_at"] == later.isoformat()


# ------------------------------------------------------------------- force

def test_force_refreshes_fresh_data_then_cooldown(isolated_runtime):
    get(clients=us_clients())
    clients = us_clients()
    forced = get(mode="force", clients=clients, now=T0 + timedelta(minutes=10))
    assert forced["data_refresh"]["decision"] == "forced" and forced["data_refresh"]["refreshed"]
    assert provider_calls(clients) > 0

    again = us_clients()
    cooled = get(mode="force", clients=again, now=T0 + timedelta(minutes=10, seconds=20))
    info = cooled["data_refresh"]
    assert info["decision"] == "force_cooldown" and info["cooldown_seconds_remaining"] == 40
    assert "refresh it again" in info["message"] and provider_calls(again) == 0

    after = us_clients()
    ok = get(mode="force", clients=after, now=T0 + timedelta(minutes=11, seconds=5))
    assert ok["data_refresh"]["decision"] == "forced" and provider_calls(after) > 0


# ---------------------------------------------------------------- failures

def test_failure_with_cached_data_returns_stale_and_keeps_timestamp(isolated_runtime):
    get(clients=us_clients())
    failing = {"fmp": FakeFmp(fail={"ORCL"}), "finnhub": FakeFinnhub()}
    data = get(clients=failing, now=T0 + timedelta(hours=25))
    info = data["data_refresh"]
    assert info["stale"] is True and info["refreshed"] is False
    assert "HTTP 503" in info["refresh_error"]
    assert data["last_refreshed_at"] == T0.isoformat()  # not updated on failure
    assert data["quote"] and data["annual_statements"]  # cached data still served


def test_failure_without_any_data_is_a_clear_error(isolated_runtime):
    from app.services.company_data import NoCompanyData

    purge_market_rows(isolated_runtime.db, "ADBE")
    with pytest.raises(NoCompanyData, match="No data could be loaded for ADBE"):
        get(symbol="ADBE", clients={})  # no provider configured


def test_unknown_company_requires_search_name(isolated_runtime):
    from app.services.company_data import UnknownCompany

    with pytest.raises(UnknownCompany):
        get(symbol="ZZZZ", clients=us_clients())


def test_news_failure_is_only_a_warning(isolated_runtime):
    from app.services.market_providers.common import ProviderError

    class NoNews(FakeFinnhub):
        def company_news(self, symbol, days=14):
            raise ProviderError("FINNHUB is busy or unavailable (HTTP 503).")

    data = get(clients={"fmp": FakeFmp(), "finnhub": NoNews()})
    info = data["data_refresh"]
    assert info["refreshed"] is True and info["stale"] is False
    assert info["warnings"] and "News could not be refreshed" in info["warnings"][0]
    assert data["last_refreshed_at"] == T0.isoformat()


# ------------------------------------------------------------- concurrency

def test_concurrent_requests_share_one_refresh(isolated_runtime):
    from app.services.company_data import get_company_data
    from app.services.market_ingestion import refresh_company

    started, release = threading.Event(), threading.Event()
    rounds = []

    def slow_refresh(*args, **kwargs):
        rounds.append(1)
        started.set()
        release.wait(10)
        return refresh_company(*args, **kwargs)

    results = {}

    def call(key):
        results[key] = get_company_data("US", "ORCL", clients=us_clients(), now=T0,
                                        refresh_fn=slow_refresh)

    first = threading.Thread(target=call, args=("first",))
    first.start()
    started.wait(10)
    second = threading.Thread(target=call, args=("second",))
    second.start()
    second.join(0.3)  # still waiting for the first refresh
    release.set()
    first.join(10)
    second.join(10)

    assert len(rounds) == 1
    assert results["first"]["data_refresh"]["joined_in_progress_refresh"] is False
    assert results["second"]["data_refresh"]["joined_in_progress_refresh"] is True
    assert results["second"]["data_refresh"]["refreshed"] is True


# --------------------------------------------------------------------- API

def test_company_data_api(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import market_ingestion

    monkeypatch.setattr(market_ingestion, "default_clients", lambda market: (us_clients(), {}))
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        url = "/api/v1/markets/US/companies/ORCL/data"
        assert client.post(url, json={"mode": "auto"}).status_code == 401
        assert client.post(url, json={"mode": "sometimes"}, headers=headers).status_code == 422
        first = client.post(url, json={}, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["data_refresh"]["decision"] == "auto_refresh_new"
        assert client.post(url, json={"mode": "auto"}, headers=headers).json()["data_refresh"]["decision"] == "served_from_cache"

        # GET stays read-only and shows the timestamp.
        monkeypatch.setattr(market_ingestion, "default_clients",
                            lambda market: pytest.fail("GET must not call providers"))
        body = client.get("/api/v1/markets/US/companies/ORCL", headers=headers).json()
        assert body["last_refreshed_at"] and body["refresh_due"] is False

        monkeypatch.setattr(market_ingestion, "default_clients",
                            lambda market: ({}, {"fmp": "FMP_API_KEY is not configured."}))
        purge_market_rows(isolated_runtime.db, "ADBE")
        missing = client.post("/api/v1/markets/US/companies/ADBE/data", json={}, headers=headers)
        assert missing.status_code == 502 and "No data could be loaded" in missing.json()["detail"]
        unknown = client.post("/api/v1/markets/US/companies/ZZZZ/data", json={}, headers=headers)
        assert unknown.status_code == 404
