"""B/C/G. Live backend API, authentication, news status and security checks (read-only).

Never calls POST /api/v1/jobs or /api/v1/news/refresh with a valid ticker and a valid key,
so no research job or provider request can start.
"""
import time
import uuid

import pytest

from conftest import TICKERS

PROTECTED_GET = [
    "/api/v1/companies",
    "/api/v1/dashboard/overview",
    "/api/v1/system/backup-health",
    "/api/v1/observability/metrics",
    "/api/v1/observability/reliability",
    "/api/v1/news/status/MSFT",
    f"/api/v1/jobs/{uuid.UUID(int=1)}",
]


def test_health_live(client):
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json()["status"] == "alive"


def test_health_ready(client):
    body = client.get("/health/ready").json()
    assert body["status"] == "ready" and body["database"] == "available"


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_missing_key_rejected(client, path):
    r = client.get(path)
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_invalid_key_rejected(client, path):
    assert client.get(path, headers={"Authorization": "Bearer wrong-key"}).status_code == 401


def test_wrong_scheme_rejected(client, api_key):
    assert client.get("/api/v1/companies", headers={"Authorization": f"Basic {api_key}"}).status_code == 401


def test_post_routes_require_key(client):
    assert client.post("/api/v1/jobs", json={"ticker": "MSFT"}).status_code == 401
    assert client.post("/api/v1/news/refresh", json={"ticker": "MSFT"}).status_code == 401
    assert client.post("/api/v1/jobs", json={"ticker": "MSFT"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_job_submission_validation(client, auth):
    # Schema validation runs before a job is created: no research starts.
    assert client.post("/api/v1/jobs", json={"ticker": "TSLA"}, headers=auth).status_code == 422
    assert client.post("/api/v1/jobs", json={"ticker": "MSFT", "x": 1}, headers=auth).status_code == 422
    assert client.post("/api/v1/jobs", json={}, headers=auth).status_code == 422


def test_news_refresh_rejects_unsupported_ticker(client, auth):
    for bad in ("TSLA", "", "../MSFT"):
        assert client.post("/api/v1/news/refresh", json={"ticker": bad}, headers=auth).status_code == 422


def test_docs_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_companies(client, auth):
    body = client.get("/api/v1/companies", headers=auth).json()
    assert body["supported_tickers"] == list(TICKERS) and body["count"] == 6


def test_dashboard_overview(client, auth):
    r = client.get("/api/v1/dashboard/overview", headers=auth)
    assert r.status_code == 200
    assert client.get("/api/v1/dashboard/overview?recent_limit=0", headers=auth).status_code == 422
    assert client.get("/api/v1/dashboard/overview?recent_limit=26", headers=auth).status_code == 422


def test_backup_health(client, auth):
    from app.database.sqlite_manager import working_database_path
    import sqlite3
    con = sqlite3.connect(working_database_path().resolve().as_uri() + "?mode=ro", uri=True)
    completed = con.execute("SELECT COUNT(*) FROM research_execution_runs WHERE status='completed'").fetchone()[0]
    con.close()
    body = client.get("/api/v1/system/backup-health", headers=auth).json()
    assert body["status"] == "healthy"
    assert body["completed_jobs_checked"] == completed >= 5 and body["unverified_backup_count"] == 0


def test_observability(client, auth):
    assert client.get("/api/v1/observability/metrics", headers=auth).status_code == 200
    assert client.get("/api/v1/observability/reliability", headers=auth).status_code == 200


def test_job_and_report_retrieval(client, auth, baseline):
    runs = baseline["primary_detail"]["research_runs"]
    assert len(runs) == 6
    for run in runs:
        job = client.get(f"/api/v1/jobs/{run['run_id']}", headers=auth)
        assert job.status_code == 200
        assert job.json()["status"] == run["status"]
        report = client.get(f"/api/v1/reports/{run['run_id']}", headers=auth)
        if run["status"] == "completed":
            assert job.json()["report_available"] is True
            assert report.status_code == 200, run["run_id"]
            doc = report.json()
            assert doc["run_id"] == run["run_id"] and doc["ticker"] == run["ticker"]
            assert set(doc["agent_statuses"].values()) == {"completed"}
        else:
            assert report.status_code == 409


def test_job_id_validation(client, auth):
    assert client.get("/api/v1/jobs/not-a-uuid", headers=auth).status_code == 422
    assert client.get(f"/api/v1/jobs/{str(uuid.uuid4()).upper()}", headers=auth).status_code == 422
    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=auth).status_code == 404
    assert client.get(f"/api/v1/reports/{uuid.uuid4()}", headers=auth).status_code == 404


def test_traces_for_saved_run(client, auth, baseline):
    run = next(r for r in baseline["primary_detail"]["research_runs"] if r["status"] == "completed")
    assert client.get(f"/api/v1/observability/runs/{run['run_id']}/traces", headers=auth).status_code in (200, 404)


@pytest.mark.parametrize("ticker", TICKERS)
def test_news_status(client, auth, ticker, baseline):
    r = client.get(f"/api/v1/news/status/{ticker}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == ticker
    import sqlite3
    from app.database.sqlite_manager import working_database_path
    con = sqlite3.connect(working_database_path().resolve().as_uri() + "?mode=ro", uri=True)
    stored = con.execute("SELECT COUNT(*) FROM news_articles WHERE company_ticker = ?", (ticker,)).fetchone()[0]
    con.close()
    assert body["article_count"] == stored >= baseline["primary_detail"]["news_by_ticker"].get(ticker, 0)


def test_news_status_unsupported(client, auth):
    assert client.get("/api/v1/news/status/TSLA", headers=auth).status_code in (404, 422)


def test_error_responses_do_not_leak_internals(client, auth):
    for r in (client.get("/api/v1/jobs/not-a-uuid", headers=auth),
              client.get(f"/api/v1/reports/{uuid.uuid4()}", headers=auth),
              client.get("/api/v1/companies")):
        text = r.text.lower()
        assert "traceback" not in text and "sqlite" not in text and "c:\\" not in text


def test_latency_repeated_requests(client, auth):
    timings = []
    for _ in range(20):
        start = time.perf_counter()
        assert client.get("/api/v1/dashboard/overview", headers=auth).status_code == 200
        timings.append(time.perf_counter() - start)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    print(f"dashboard overview median={timings[10]*1000:.1f}ms p95={p95*1000:.1f}ms")
    assert p95 < 2.0
