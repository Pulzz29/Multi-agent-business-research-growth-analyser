"""Shared fixtures for the InsightIQ local migration test suite.

Safety: every test runs with outbound network access blocked (loopback only), so no
Groq, NewsData.io or SEC request can be made. API tests target the locally running
backend (default http://127.0.0.1:8000) started with the offline test profile.
"""
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"
ENV_FILE = Path(os.getenv("INSIGHTIQ_TEST_ENV_FILE")
                or (PROJECT_ROOT / ".env.test" if (PROJECT_ROOT / ".env.test").is_file() else PROJECT_ROOT / ".env"))
ORIGINAL_EXPORT = PROJECT_ROOT / "migration_inputs" / "drive_export_original" / "20261122T173459146790Z"
requires_original_export = __import__("pytest").mark.skipif(
    not ORIGINAL_EXPORT.is_dir(), reason="original Drive export not present (deployment package)")
BASE_URL = os.getenv("INSIGHTIQ_TEST_BASE_URL", "http://127.0.0.1:8000")
FRONTEND_URL = os.getenv("INSIGHTIQ_TEST_FRONTEND_URL", "http://127.0.0.1:5173")
TICKERS = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")
BASELINE = PROJECT_ROOT / "migration_reports" / "artifacts" / "baseline_original.json"

for key, value in dotenv_values(ENV_FILE).items():
    if value is not None:
        os.environ.setdefault(key, value)
os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))
sys.path.insert(0, str(BACKEND))

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else str(address)
    if host not in _LOOPBACK:
        raise ConnectionRefusedError(f"Test network guard blocked {host}")
    return _real_connect(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if host not in _LOOPBACK and host is not None:
        raise socket.gaierror(f"Test network guard blocked DNS for {host}")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)


@pytest.fixture(scope="session")
def api_key():
    key = os.getenv("API_ACCESS_KEY", "")
    assert key, "API_ACCESS_KEY missing from the test env file"
    return key


@pytest.fixture(scope="session")
def client():
    import httpx

    with httpx.Client(base_url=BASE_URL, timeout=30.0, trust_env=False) as c:
        yield c


@pytest.fixture(scope="session")
def auth(api_key):
    return {"Authorization": f"Bearer {api_key}"}


@pytest.fixture(scope="session")
def baseline():
    return json.loads(BASELINE.read_text(encoding="utf-8"))
