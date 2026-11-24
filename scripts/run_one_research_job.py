"""Submit exactly ONE research job to the local InsightIQ API and wait for it to finish.

The access key is read from .env inside this process and is never printed.
A marker file prevents accidental re-submission.
Usage: .venv\\Scripts\\python.exe scripts\\run_one_research_job.py MSFT
"""
import json
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
marker = ROOT / "logs" / f"live_research_submitted_{ticker}.json"

if marker.exists():
    sys.exit(f"Refusing: a live job was already submitted ({marker.name}). Delete it only with approval.")

key = (dotenv_values(ROOT / ".env").get("API_ACCESS_KEY") or "").strip()
if not key:
    sys.exit("API_ACCESS_KEY missing in .env")
headers = {"Authorization": f"Bearer {key}"}

with httpx.Client(base_url=BASE, timeout=30.0, trust_env=False) as client:
    ready = client.get("/health/ready").json()
    assert ready.get("status") == "ready", ready

    response = client.post("/api/v1/jobs", json={"ticker": ticker}, headers=headers)
    print("submit:", response.status_code)
    if response.status_code != 202:
        print(response.text[:300])
        sys.exit(1)
    submitted = response.json()
    run_id = submitted["run_id"]
    marker.write_text(json.dumps({"run_id": run_id, "ticker": ticker, "submitted_at": time.time()}), encoding="utf-8")
    print("run_id:", run_id)

    deadline = time.time() + 600
    last = None
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{run_id}", headers=headers).json()
        state = (job["status"], json.dumps(job["agent_statuses"], sort_keys=True))
        if state != last:
            print(f"[{time.strftime('%H:%M:%S')}] status={job['status']} agents={job['agent_statuses']}")
            last = state
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(3)

    print("final job record:", json.dumps({k: job[k] for k in (
        "run_id", "ticker", "status", "started_at_utc", "finished_at_utc",
        "agent_statuses", "report_available", "error_code")}))

    if job["status"] == "completed":
        report = client.get(f"/api/v1/reports/{run_id}", headers=headers)
        print("report:", report.status_code)
        doc = report.json()
        news_diag = (doc["news"].get("ai_diagnostics") or doc["news"].get("groq_diagnostics") or {})
        summary = {
            "agent_statuses": doc["agent_statuses"],
            "agent_timings_seconds": doc.get("agent_timings_seconds"),
            "total_elapsed_seconds": doc.get("total_elapsed_seconds"),
            "financial_verified_claims": len(doc["financial"]["verified_claims"]),
            "news_selected_articles": len(doc["news"]["selected_articles"]),
            "news_selected_ids": [a["evidence_id"] for a in doc["news"]["selected_articles"]],
            "strategic_priorities": len(doc["strategic"]["research_priorities"]),
            "news_request_budget": news_diag.get("request_budget"),
            "news_request_body_bytes": news_diag.get("request_body_bytes"),
            "news_prompt_tokens_actual": news_diag.get("prompt_tokens"),
            "news_completion_tokens_actual": news_diag.get("completion_tokens"),
            "news_total_tokens_actual": news_diag.get("total_tokens"),
            "news_disclosures": doc["news"].get("disclosures"),
        }
        print("report summary:", json.dumps(summary, indent=1))
    elif job["status"] == "failed":
        print("Job failed; see logs/backend_live.log for sanitized diagnostics (ai_call entries).")
