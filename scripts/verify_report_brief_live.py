"""
Live verification of the research-report upgrade (uses Azure OpenAI tokens:
one AI summary + thorough research generation per company given).

For each "TICKER:RUN_ID": opens the latest report in Microsoft Edge (the page
starts the generation automatically), waits for the validated result, records
response ID / tokens / validation, reloads to confirm the cached version is
reused (no second generation), and downloads the PDF through the button.
Evidence: docs/validation_evidence/live_research/brief_<TICKER>_<run8>.json

Usage (backend and frontend running):
  .venv\\Scripts\\python.exe scripts\\verify_report_brief_live.py MSFT:<run_id> ITC.NS:<run_id>
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:8000"
WEB = "http://127.0.0.1:5173"
EVIDENCE = ROOT / "docs" / "validation_evidence" / "live_research"
KEY = dotenv_values(ROOT / ".env")["API_ACCESS_KEY"]


def api(path):
    request = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def verify(page, ticker, run_id):
    started = time.monotonic()
    before = api(f"/api/v1/companies/{ticker}/reports/{run_id}/executive-summary")
    page.goto(f"{WEB}/#/companies/{ticker}/reports/{run_id}")
    page.wait_for_selector("#summary", timeout=120000)
    state = None
    for _ in range(360):  # up to 30 minutes
        state = api(f"/api/v1/companies/{ticker}/reports/{run_id}/executive-summary")
        if state["status"] != "generating" and (state.get("is_current") or state["job_status"] == "failed"):
            break
        time.sleep(5)
    elapsed = round(time.monotonic() - started, 1)
    record = state.get("summary") or {}
    page.wait_for_timeout(6000)  # the page polls every 5 s
    page.locator("#summary").screenshot(path=str(EVIDENCE / f"brief_{ticker}_{run_id[:8]}_summary.png"))

    # Reopen: the cached brief must be reused (no new generation).
    page.reload()
    page.wait_for_selector("#summary .summary-section", timeout=120000)
    page.wait_for_timeout(8000)
    after = api(f"/api/v1/companies/{ticker}/reports/{run_id}/executive-summary")
    cache_hit = (after.get("generated_at_utc") == state.get("generated_at_utc")
                 and after["status"] == "completed" and after.get("is_current") is True)

    # PDF through the button.
    with page.expect_download(timeout=120000) as info:
        page.get_by_role("button", name="Download report").click()
    download = info.value
    pdf_path = EVIDENCE / download.suggested_filename
    download.save_as(str(pdf_path))
    pdf = pdf_path.read_bytes()

    diagnostics = record.get("diagnostics") or {}
    out = {
        "ticker": ticker,
        "run_id": run_id,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "before": {k: before.get(k) for k in ("status", "job_status", "is_current", "generated_at_utc")},
        "result": {k: state.get(k) for k in ("status", "job_status", "is_current", "generated_at_utc",
                                             "data_as_of", "error")},
        "seconds_from_open_to_result": elapsed,
        "azure": {k: diagnostics.get(k) for k in ("provider", "deployment", "model", "response_id",
                                                 "input_tokens", "output_tokens", "reasoning_tokens",
                                                 "total_tokens", "elapsed_seconds", "provider_requests",
                                                 "context_level", "failure_reason")},
        "billed_all_attempts": diagnostics.get("billed_tokens_all_attempts"),
        "summary_words": record.get("word_count"),
        "summary_sections": [s["section"] for s in record.get("sections") or []],
        "research_status": (record.get("research") or {}).get("status"),
        "research_sections": [s["section"] for s in (record.get("research") or {}).get("sections") or []],
        "research_withheld": (record.get("research") or {}).get("withheld"),
        "sources": len(record.get("sources") or []),
        "attempt": record.get("attempt"),
        "previous_versions_kept": len(record.get("previous_versions") or []),
        "cache_hit_on_reopen": cache_hit,
        "pdf": {"file": pdf_path.name, "bytes": len(pdf), "is_pdf": pdf.startswith(b"%PDF")},
    }
    path = EVIDENCE / f"brief_{ticker}_{run_id[:8]}.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("ticker", "result", "azure", "research_status",
                                          "cache_hit_on_reopen", "pdf")}, default=str))


def main(pairs):
    from playwright.sync_api import sync_playwright

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
        context.add_init_script("window.sessionStorage.setItem('insightiq-token', %s);" % json.dumps(KEY))
        page = context.new_page()
        for pair in pairs:
            ticker, run_id = pair.split(":", 1)
            verify(page, ticker, run_id)
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
