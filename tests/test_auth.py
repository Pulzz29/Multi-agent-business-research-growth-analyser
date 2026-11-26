"""Email/password login, sessions, CSRF, and Activity report access.

Runs against an isolated PostgreSQL schema (no real accounts, reports or
provider calls). Test passwords are random per run; none is stored in code.
"""
import json
import os
import secrets
import uuid
from datetime import timedelta

import pytest

from research_v2_support import isolated_runtime  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(os.environ.get("DB_BACKEND") != "postgres",
                                reason="accounts and sessions need PostgreSQL")

EMAIL = "Tester@Example.com"


@pytest.fixture
def web(isolated_runtime):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.core.security import LIMITER
    from app.database.migrations import run_migrations
    from app.main import _LOGIN_LIMITER, app
    from app.services import auth

    run_migrations()
    LIMITER.reset()
    _LOGIN_LIMITER.reset()
    password = secrets.token_urlsafe(12)
    assert auth.create_user(EMAIL, password) == "created"
    with TestClient(app) as client:
        yield client, password
    LIMITER.reset()
    _LOGIN_LIMITER.reset()


def _login(client, password, email=EMAIL):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


# ------------------------------------------------------------------ accounts

def test_passwords_are_hashed_and_provisioning_is_idempotent(web):
    from app.database import db
    from app.services import auth

    _, password = web
    with db.connect() as conn:
        rows = conn.execute("SELECT email, password_hash, status FROM users WHERE email = ?",
                            (EMAIL.lower(),)).fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "active"
    stored = rows[0]["password_hash"]
    assert stored.startswith("scrypt$") and password not in stored
    # Same email in another case / with spaces: no duplicate and no silent reset.
    assert auth.create_user("  tester@EXAMPLE.com ", "another-password-1") == "exists"
    assert auth.authenticate(EMAIL, password) is not None
    assert auth.authenticate(EMAIL, "another-password-1") is None
    with pytest.raises(auth.AuthError):
        auth.create_user("not-an-email", "long-enough-pw")
    with pytest.raises(auth.AuthError):
        auth.create_user("short@example.com", "short")


# ------------------------------------------------------------------ login flow

def test_login_me_logout(web):
    client, password = web
    assert client.get("/api/v1/auth/me").status_code == 401

    r = _login(client, password, email=" tester@example.COM ")
    assert r.status_code == 200 and r.json()["user"]["email"] == "tester@example.com"
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("insightiq_session="))
    csrf_cookie = next(c for c in cookies if c.startswith("insightiq_csrf="))
    assert "HttpOnly" in session_cookie and "samesite=strict" in session_cookie.lower()
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" not in session_cookie  # plain http test server; https covered below
    assert password not in r.text

    assert client.get("/api/v1/auth/me").json() == {"user": {"email": "tester@example.com"}}
    # Private APIs accept the session (no API key).
    assert client.get("/api/v1/research/runs").status_code == 200

    # Logout needs the CSRF token, then revokes the session server-side.
    token = client.cookies.get("insightiq_session")
    assert client.post("/api/v1/auth/logout").status_code == 403
    csrf = r.json()["csrf_token"]
    assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    client.cookies.set("insightiq_session", token)  # replaying the old cookie fails
    assert client.get("/api/v1/research/runs").status_code == 401


def test_invalid_credentials_get_one_generic_error(web):
    client, password = web
    wrong_password = _login(client, password + "x")
    unknown_email = _login(client, password, email="nobody@example.com")
    for r in (wrong_password, unknown_email):
        assert r.status_code == 401 and r.json()["detail"] == "Invalid email or password."
        assert "insightiq_session" not in r.headers.get("set-cookie", "")


def test_login_rate_limit(web, monkeypatch):
    client, password = web
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_15_MINUTES", "3")
    for _ in range(3):
        assert _login(client, "wrong-password").status_code == 401
    blocked = _login(client, password)  # even the right password, until the window passes
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0


def test_https_cookies_are_secure(web):
    from fastapi.testclient import TestClient

    from app.main import app

    _, password = web
    with TestClient(app, base_url="https://testserver") as client:
        r = _login(client, password)
    assert r.status_code == 200
    assert all("Secure" in c for c in r.headers.get_list("set-cookie"))


def test_production_proxy_chain_and_forced_secure_cookies(web, monkeypatch):
    """AKS: ingress -> nginx -> API. With both hops trusted (as uvicorn's proxy
    headers middleware is configured by TRUSTED_PROXY_IPS), HTTPS is detected
    and rate limits are per real client, not per proxy."""
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.main import _LOGIN_LIMITER, app

    _, password = web
    behind_proxy = TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"))
    forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.7, 10.244.0.5"}
    r = behind_proxy.post("/api/v1/auth/login", json={"email": EMAIL, "password": password}, headers=forwarded)
    assert r.status_code == 200 and all("Secure" in c for c in r.headers.get_list("set-cookie"))
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_15_MINUTES", "2")
    _LOGIN_LIMITER.reset()
    for _ in range(2):
        behind_proxy.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "x"},
                          headers={**forwarded, "X-Forwarded-For": "203.0.113.7"})
    other_client = behind_proxy.post("/api/v1/auth/login", json={"email": EMAIL, "password": password},
                                     headers={**forwarded, "X-Forwarded-For": "198.51.100.9"})
    assert other_client.status_code == 200  # one client's failures do not lock out others

    # SESSION_COOKIE_SECURE=true: Secure even if the proxy header is missing.
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    _LOGIN_LIMITER.reset()
    plain = TestClient(app).post("/api/v1/auth/login", json={"email": EMAIL, "password": password})
    assert plain.status_code == 200 and all("Secure" in c for c in plain.headers.get_list("set-cookie"))


def test_password_rotation_with_reset_revokes_sessions(web):
    import subprocess
    import sys

    from app.services import auth
    from conftest import PROJECT_ROOT

    _, password = web
    user = auth.authenticate(EMAIL, password)
    token, _, _ = auth.create_session(user)
    new_password = secrets.token_urlsafe(14)
    env = {**os.environ, "INSIGHTIQ_PROJECT_ROOT": os.environ["INSIGHTIQ_PROJECT_ROOT"]}
    script = str(PROJECT_ROOT / "scripts" / "provision_users.py")
    # Without --reset an existing account is never changed.
    kept = subprocess.run([sys.executable, script, EMAIL, "--password-stdin", "--env-file", os.devnull],
                          input=new_password + "\n", capture_output=True, text=True, env=env, timeout=120)
    assert kept.returncode == 0 and "exists" in kept.stdout and new_password not in kept.stdout + kept.stderr
    assert auth.authenticate(EMAIL, password) is not None
    rotated = subprocess.run([sys.executable, script, EMAIL, "--reset", "--password-stdin", "--env-file", os.devnull],
                             input=new_password + "\n", capture_output=True, text=True, env=env, timeout=120)
    assert rotated.returncode == 0 and "reset" in rotated.stdout and new_password not in rotated.stdout + rotated.stderr
    assert auth.authenticate(EMAIL, password) is None
    assert auth.authenticate(EMAIL, new_password) is not None
    assert auth.validate_session(token) is None  # signed out everywhere


def test_csrf_required_for_state_changes(web):
    client, password = web
    csrf = _login(client, password).json()["csrf_token"]
    body = {"ticker": "ZZZZ"}  # rejected by validation, so no job can start
    assert client.post("/api/v1/jobs", json=body).status_code == 403
    assert client.post("/api/v1/jobs", json=body, headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert client.post("/api/v1/jobs", json=body, headers={"X-CSRF-Token": csrf}).status_code == 422


def test_session_expiry(web):
    from app.services import auth

    _, password = web
    user = auth.authenticate(EMAIL, password)
    token, _, expires = auth.create_session(user)
    assert auth.validate_session(token) is not None
    _, idle = auth.session_lifetimes()
    assert auth.validate_session(token, now=auth.utc_now() + idle + timedelta(minutes=1)) is None
    assert auth.validate_session(token, now=expires + timedelta(seconds=1)) is None


def test_protected_routes_reject_anonymous_and_keep_server_key(web):
    client, _ = web
    for path in ("/api/v1/research/runs", "/api/v1/research/companies", "/api/v1/dashboard/overview"):
        assert client.get(path).status_code == 401
        client.cookies.set("insightiq_session", "forged-session-token")
        assert client.get(path).status_code == 401
        client.cookies.clear()
    key = {"Authorization": "Bearer " + os.environ["API_ACCESS_KEY"]}
    assert client.get("/api/v1/research/companies", headers=key).status_code == 200


# ------------------------------------------------ Activity: companies + reports

def _run(ticker, status, report, created):
    from app.database import db
    from app.services import report_history
    from research_v2_support import put_doc

    run_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO research_execution_runs(run_id, ticker, status, created_at_utc, updated_at_utc, "
            "agent_statuses_json) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, ticker, status, created, created, json.dumps({"financial": status})))
    if report:
        put_doc(report_history.reports_directory() / f"{run_id}.json", json.dumps({
            "run_id": run_id, "ticker": ticker, "status": "completed", "generated_at_utc": created,
            "agent_statuses": {"financial": "completed"}}))
    return run_id


def test_research_companies_lists_only_completed_runs_with_saved_reports(web, monkeypatch):
    from app.services import market_storage

    client, password = web
    _login(client, password)
    for symbol, market, name in (("TESTIQ.NS", "IN", "Test Industries"), ("NOREP.NS", "IN", "No Report Ltd"),
                                 ("FAILIQ", "US", "Failed Only Inc")):
        market_storage.register_company(symbol=symbol, market=market, name=name)
    before = {c["ticker"] for c in client.get("/api/v1/research/companies").json()["companies"]}

    first = _run("TESTIQ.NS", "completed", True, "2026-11-20T10:00:00+00:00")
    second = _run("TESTIQ.NS", "completed", True, "2026-11-21T10:00:00+00:00")
    _run("NOREP.NS", "completed", False, "2026-11-21T11:00:00+00:00")   # report missing
    _run("FAILIQ", "failed", False, "2026-11-21T12:00:00+00:00")
    _run("FAILIQ", "running", False, "2026-11-21T13:00:00+00:00")

    companies = client.get("/api/v1/research/companies").json()["companies"]
    tickers = [c["ticker"] for c in companies]
    assert len(tickers) == len(set(tickers))  # no duplicates
    added = {c["ticker"]: c for c in companies if c["ticker"] not in before}
    assert set(added) == {"TESTIQ.NS"}
    assert added["TESTIQ.NS"]["name"] == "Test Industries" and added["TESTIQ.NS"]["market"] == "IN"
    assert added["TESTIQ.NS"]["report_count"] == 2 and added["TESTIQ.NS"]["sec_validated"] is False

    # Failed and incomplete runs stay in the run history.
    runs = client.get("/api/v1/research/runs").json()["runs"]
    statuses = {(r["ticker"], r["status"], r["report_available"]) for r in runs}
    assert {("FAILIQ", "failed", False), ("FAILIQ", "running", False), ("NOREP.NS", "completed", False)} <= statuses

    # Each "View report" opens that run's own stored report, without AI calls.
    from app.services import ai_provider

    def no_ai(*a, **k):
        raise AssertionError("opening a saved report must not call an AI provider")

    monkeypatch.setattr(ai_provider, "default_client", no_ai)
    for run_id in (first, second):
        r = client.get(f"/api/v1/companies/TESTIQ.NS/reports/{run_id}")
        assert r.status_code == 200
        assert r.json()["report"]["run_id"] == run_id and r.json()["report"]["ticker"] == "TESTIQ.NS"
