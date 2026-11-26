"""
24-hour company-data cache (table company_data_cache) on an isolated database.

Time is simulated by passing ``now``; providers are counting test fakes
(tests/market_fixtures.py). No network is used.
"""
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from market_fixtures import FakeFinnhub, FakeFmp, us_clients
from research_v2_support import db_rows, isolated_runtime  # noqa: F401

T0 = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
DAY = timedelta(hours=24)


@pytest.fixture(autouse=True)
def clean_state():
    from app.services import company_data

    company_data._INFLIGHT.clear()
    company_data._LAST_FORCE.clear()
    yield
    company_data._INFLIGHT.clear()
    company_data._LAST_FORCE.clear()


def calls(clients):
    return len(clients["fmp"].calls) + len(clients["finnhub"].calls)


def get(symbol="ORCL", mode="auto", clients=None, now=T0, **kw):
    from app.services.company_data import get_company_data

    return get_company_data("US", symbol, mode=mode, clients=clients, now=now, **kw)


def cache_row(symbol="ORCL"):
    from app.services import company_cache

    return company_cache.get_row(company_cache.COMPANY, company_cache.company_key("US", symbol))


# ---------------------------------------------------------- lifecycle

def test_first_search_fetches_and_stores_with_a_24_hour_expiry(isolated_runtime):
    clients = us_clients()
    data = get(clients=clients)
    assert calls(clients) > 0
    info = data["data_refresh"]
    assert info["decision"] == "auto_refresh_new" and info["refreshed"] is True
    assert info["cache"]["status"] == "stored"
    row = cache_row()
    assert row["status"] == "ready"
    assert row["fetched_at"] == T0 and row["expires_at"] == T0 + DAY
    assert row["fetched_at"].tzinfo is not None and row["expires_at"].utcoffset() == timedelta(0)


def test_repeat_within_24_hours_makes_no_provider_call_and_never_extends_expiry(isolated_runtime, monkeypatch):
    get(clients=us_clients())
    # Even the database rebuild of the payload is skipped on a cache hit.
    from app.services import company_data

    monkeypatch.setattr(company_data, "build_payload", lambda *a, **k: pytest.fail("cache hit rebuilt data"))
    for offset in (timedelta(minutes=1), timedelta(hours=12), DAY - timedelta(seconds=1)):
        clients = us_clients()
        data = get(clients=clients, now=T0 + offset)
        assert calls(clients) == 0
        assert data["data_refresh"]["decision"] == "served_from_cache"
        assert data["data_refresh"]["cache"]["status"] == "hit"
        assert data["data_refresh"]["cache"]["expires_at"] == (T0 + DAY).isoformat()
    assert cache_row()["expires_at"] == T0 + DAY  # unchanged by the hits


def test_exactly_24_hours_later_the_data_is_expired_and_refetched(isolated_runtime):
    get(clients=us_clients())
    clients = us_clients()
    later = T0 + DAY
    data = get(clients=clients, now=later)
    assert calls(clients) > 0
    assert data["data_refresh"]["decision"] == "auto_refresh_stale"
    row = cache_row()
    assert row["fetched_at"] == later and row["expires_at"] == later + DAY


def test_failed_refresh_is_not_cached_and_is_shown_as_stale(isolated_runtime):
    get(clients=us_clients())
    failing = {"fmp": FakeFmp(fail={"ORCL"}), "finnhub": FakeFinnhub()}
    data = get(clients=failing, now=T0 + DAY + timedelta(hours=1))
    info = data["data_refresh"]
    assert info["stale"] is True and info["refreshed"] is False and info["refresh_error"]
    assert info["cache"]["status"] == "not_cached"
    row = cache_row()
    assert row["expires_at"] == T0 + DAY  # the expired entry was not renewed
    assert row["refresh_owner"] is None and row["last_error"]
    # The next request still sees expired data and tries again.
    clients = us_clients()
    get(clients=clients, now=T0 + DAY + timedelta(hours=2))
    assert calls(clients) > 0


def test_first_fetch_failure_stores_nothing_valid(isolated_runtime):
    from app.services import company_cache
    from app.services.company_data import NoCompanyData
    from research_v2_support import open_db

    con = open_db(isolated_runtime.db)
    try:
        for table in ("market_quotes", "market_statements", "market_companies"):
            con.execute(f"DELETE FROM {table} WHERE symbol = 'ADBE'")
        con.commit()
    finally:
        con.close()
    with pytest.raises(NoCompanyData):
        get(symbol="ADBE", clients={})
    assert company_cache.get_valid(company_cache.COMPANY, company_cache.company_key("US", "ADBE"), T0) is None


def test_data_fetched_before_the_cache_existed_is_served_until_its_own_expiry(isolated_runtime):
    from app.services import company_cache

    get(clients=us_clients())
    # Simulate data migrated from SQLite: stored and fresh, but no cache row.
    con_rows = db_rows(isolated_runtime.db, "SELECT COUNT(*) AS n FROM company_data_cache")
    assert con_rows[0]["n"] == 1
    from research_v2_support import open_db

    con = open_db(isolated_runtime.db)
    try:
        con.execute("DELETE FROM company_data_cache")
        con.commit()
    finally:
        con.close()
    clients = us_clients()
    data = get(clients=clients, now=T0 + timedelta(hours=3))
    assert calls(clients) == 0 and data["data_refresh"]["decision"] == "served_from_database"
    assert cache_row()["expires_at"] == T0 + DAY  # last_refreshed_at + TTL, not now + TTL
    assert company_cache.get_valid(company_cache.COMPANY, "US:ORCL", T0 + DAY) is None


# ---------------------------------------------------------- concurrency

def test_ten_simultaneous_searches_fetch_once(isolated_runtime):
    from app.services.company_data import get_company_data
    from app.services.market_ingestion import refresh_company

    rounds = []
    gate = threading.Event()

    def slow_refresh(*args, **kwargs):
        rounds.append(1)
        gate.wait(10)
        return refresh_company(*args, **kwargs)

    results = []
    errors = []

    def call():
        try:
            results.append(get_company_data("US", "ORCL", clients=us_clients(), now=T0,
                                            refresh_fn=slow_refresh))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(10)]
    for thread in threads:
        thread.start()
    threading.Event().wait(1.0)
    gate.set()
    for thread in threads:
        thread.join(60)
    assert not errors and len(results) == 10
    assert len(rounds) == 1
    assert all(r["quote"] for r in results)


def test_a_fetch_in_another_instance_is_awaited_not_repeated(isolated_runtime):
    """Another instance holds the fetch lease (row lock in the database):
    this request waits for its stored result and calls no provider."""
    from app.services import company_cache, company_data

    get(clients=us_clients())  # data exists
    key = company_cache.company_key("US", "ORCL")
    other = "other-instance"
    assert company_cache.acquire_lease(company_cache.COMPANY, key, "US", "ORCL", other)
    payload = company_data.build_payload("US", "ORCL", T0 + DAY)

    def other_instance_finishes():
        threading.Event().wait(1.0)
        company_cache.store(company_cache.COMPANY, key, "US", "ORCL", payload, owner=other,
                            fetched_at=company_cache.utc_now())

    finisher = threading.Thread(target=other_instance_finishes)
    finisher.start()
    clients = us_clients()
    data = get(clients=clients, now=T0 + DAY + timedelta(minutes=5))
    finisher.join(10)
    assert calls(clients) == 0
    assert data["data_refresh"]["joined_in_progress_refresh"] is True
    assert data["data_refresh"]["cache"]["status"] == "joined"


def test_lease_rules(isolated_runtime):
    from app.services import company_cache as c

    now = c.utc_now()
    key = c.company_key("US", "LEASE")
    assert c.acquire_lease(c.COMPANY, key, "US", "LEASE", "a", now=now, lease_seconds=60)
    assert not c.acquire_lease(c.COMPANY, key, "US", "LEASE", "b", now=now)
    assert c.acquire_lease(c.COMPANY, key, "US", "LEASE", "a", now=now, lease_seconds=60)  # owner renews
    # A crashed owner's lease can be taken over after it expires.
    assert c.acquire_lease(c.COMPANY, key, "US", "LEASE", "b", now=now + timedelta(seconds=61))
    assert c.get_valid(c.COMPANY, key, now) is None  # a pending row is never valid


# ---------------------------------------------------- keys and duplicates

def test_keys_are_normalised_and_distinct_companies_stay_distinct(isolated_runtime):
    from app.services import company_cache as c

    assert c.company_key(" us ", " orcl ") == c.company_key("US", "ORCL") == "US:ORCL"
    assert c.company_key("US", "ORCL") != c.company_key("IN", "ORCL")
    assert c.company_key("US", "BRK.A") != c.company_key("US", "BRK.B")
    assert c.search_key("US", "  Apple   Inc ") == c.search_key("us", "apple inc")

    get(clients=us_clients())
    get(clients=us_clients(), now=T0 + timedelta(minutes=1))
    get(clients=us_clients(), now=T0 + DAY + timedelta(minutes=1))
    rows = db_rows(isolated_runtime.db, "SELECT cache_key FROM company_data_cache")
    assert [r["cache_key"] for r in rows] == ["US:ORCL"]  # one row, updated in place


def test_invalid_results_are_never_cached(isolated_runtime):
    from app.services import company_cache as c

    with pytest.raises(ValueError):
        c.store(c.COMPANY, "US:X", "US", "X", None)


# ------------------------------------------------------------- cleanup

def test_cleanup_deletes_only_expired_cache_rows(isolated_runtime):
    from app.services import cache_cleanup, company_cache as c

    get(clients=us_clients())
    fresh_key = c.company_key("US", "FRESH")
    c.store(c.COMPANY, fresh_key, "US", "FRESH", {"x": 1}, fetched_at=c.utc_now())
    counts = {t: db_rows(isolated_runtime.db, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
              for t in ("companies", "market_companies", "market_statements", "market_quotes",
                        "news_articles", "sec_facts", "research_execution_runs")}

    # ORCL was fetched at T0 (simulated past): expired now, deleted; FRESH stays.
    deleted = c.cleanup_expired()
    assert deleted == 1
    keys = [r["cache_key"] for r in db_rows(isolated_runtime.db, "SELECT cache_key FROM company_data_cache")]
    assert keys == [fresh_key]
    for table, before in counts.items():
        assert db_rows(isolated_runtime.db, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == before, table
    assert cache_cleanup.run_once() == 0  # idempotent; safe from several instances


def test_cleanup_interval_is_configurable(monkeypatch):
    from app.core.config import get_settings
    from app.services import cache_cleanup

    monkeypatch.setenv("CACHE_CLEANUP_INTERVAL_MINUTES", "0.5")
    get_settings.cache_clear()
    try:
        assert cache_cleanup.interval_seconds() == 30.0
    finally:
        monkeypatch.delenv("CACHE_CLEANUP_INTERVAL_MINUTES")
        get_settings.cache_clear()
    assert cache_cleanup.interval_seconds() == 300.0


def test_cleanup_thread_runs_periodically(isolated_runtime, monkeypatch):
    from app.services import cache_cleanup

    passes = []
    monkeypatch.setattr(cache_cleanup, "interval_seconds", lambda: 0.05)
    monkeypatch.setattr(cache_cleanup, "run_once", lambda: passes.append(1))
    cache_cleanup.start()
    try:
        threading.Event().wait(0.4)
    finally:
        cache_cleanup.stop()
    assert len(passes) >= 3


# ---------------------------------------------- persistence and outages

def test_cache_survives_an_application_restart(isolated_runtime):
    from app.database import db

    get(clients=us_clients())
    db.close_pools()  # what a restart does to connections
    clients = us_clients()
    data = get(clients=clients, now=T0 + timedelta(hours=1))
    assert calls(clients) == 0 and data["data_refresh"]["cache"]["status"] == "hit"


def test_database_outage_is_a_clear_error(isolated_runtime, monkeypatch):
    from app.core.config import get_settings
    from app.database import db

    if not db.is_postgres():
        pytest.skip("PostgreSQL connection settings only")
    real_port = os.environ.get("DB_PORT")
    monkeypatch.setenv("DB_PORT", "1")  # nothing listens there
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    db.close_pools()
    try:
        with pytest.raises(db.DatabaseUnavailable, match="database is unavailable"):
            get(clients=us_clients())
    finally:
        # Restore only what this test changed (the isolation fixture's own
        # settings stay in place for its teardown).
        db.close_pools()
        if real_port is None:
            monkeypatch.delenv("DB_PORT", raising=False)
        else:
            monkeypatch.setenv("DB_PORT", real_port)
        monkeypatch.delenv("DB_POOL_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("DB_CONNECT_TIMEOUT_SECONDS", raising=False)
        get_settings.cache_clear()


def test_api_reports_a_database_outage_as_503(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    from app.database import db
    from app.main import app
    from app.services import company_data

    def down(*a, **k):
        raise db.DatabaseUnavailable("The database is unavailable.")

    monkeypatch.setattr(company_data, "get_company_data", down)
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/v1/markets/US/companies/ORCL/data", json={}, headers=headers)
    # A clear 503 (never a 500 or a fake success); the endpoint keeps its own wording.
    assert response.status_code == 503 and "unavailable" in response.json()["detail"]


# ------------------------------------------------------- provider search

def test_provider_search_is_cached_for_24_hours(isolated_runtime, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import market_ingestion

    clients = us_clients()
    searches = []
    real_search = clients["finnhub"].search
    clients["finnhub"].search = lambda q: searches.append(q) or real_search(q)
    monkeypatch.setattr(market_ingestion, "default_clients", lambda market: (clients, {}))
    headers = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    with TestClient(app) as client:
        url = "/api/v1/markets/US/companies/search"
        first = client.post(url, json={"q": "Oracle"}, headers=headers).json()
        second = client.post(url, json={"q": "  oracle "}, headers=headers).json()
    assert first["source"] == "provider" and second["source"] == "cache"
    assert second["results"] == first["results"] and len(searches) == 1
