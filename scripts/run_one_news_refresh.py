"""Trigger exactly ONE news refresh through the local InsightIQ API (same route as the UI button).

The access key is read from .env inside this process and never printed.
A marker file prevents accidental repetition.
Usage: .venv\\Scripts\\python.exe scripts\\run_one_news_refresh.py MSFT
"""
import json
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
marker = ROOT / "logs" / f"live_news_refresh_{ticker}.json"

if marker.exists():
    sys.exit(f"Refusing: a live refresh was already run ({marker.name}). Delete it only with approval.")

key = (dotenv_values(ROOT / ".env").get("API_ACCESS_KEY") or "").strip()
headers = {"Authorization": f"Bearer {key}"}

with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120.0, trust_env=False) as client:
    before = client.get(f"/api/v1/news/status/{ticker}", headers=headers).json()
    print("before:", json.dumps(before))
    marker.write_text(json.dumps({"ticker": ticker, "started_at": time.time()}), encoding="utf-8")
    started = time.perf_counter()
    response = client.post("/api/v1/news/refresh", json={"ticker": ticker}, headers=headers)
    print(f"refresh: HTTP {response.status_code} in {time.perf_counter() - started:.1f}s")
    print("response:", json.dumps(response.json()))
    after = client.get(f"/api/v1/news/status/{ticker}", headers=headers).json()
    print("after:", json.dumps(after))
