"""
Controlled live verification of the external providers (one bounded call each
for Finnhub, FMP and Yahoo Finance), combined with the live evidence already
recorded for Azure OpenAI (verify_azure.py), SEC EDGAR (onboard_sec_universe.py)
and NewsData.io (onboard_market_data.py). Writes
docs/validation_evidence/provider_verification.json. No credentials are printed.

Usage: .venv\\Scripts\\python.exe scripts\\verify_providers.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
EVIDENCE = PROJECT_ROOT / "docs" / "validation_evidence"


def timed(label, fn):
    started = time.monotonic()
    out = {"provider": label, "checked_at_utc": datetime.now(timezone.utc).isoformat(), "mode": "live_request"}
    try:
        data = fn()
        out.update(outcome="success", data=data)
    except Exception as exc:
        safe = getattr(exc, "safe_message", None) or str(exc)
        out.update(outcome="failed", error_type=type(exc).__name__, error=str(safe)[:240])
    out["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return out


def load(name):
    path = EVIDENCE / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def main():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))
    from app.services.market_ingestion import default_clients

    us, us_missing = default_clients("US")
    india, _ = default_clients("IN")
    results = {}

    def finnhub_quote():
        if "finnhub" not in us:
            raise RuntimeError(us_missing.get("finnhub", "not configured"))
        q = us["finnhub"].quote("MSFT")
        return {"company": "MSFT", "type": "quote", "fields": sorted(k for k, v in q.items() if v is not None)[:12],
                "as_of": q.get("as_of"), "price_present": q.get("price") is not None}

    def fmp_profile():
        if "fmp" not in us:
            raise RuntimeError(us_missing.get("fmp", "not configured"))
        p = us["fmp"].profile("MSFT")
        return {"company": "MSFT", "type": "profile", "name": p.get("name"), "exchange": p.get("exchange"),
                "currency": p.get("currency"), "sector": p.get("sector")}

    def yfinance_quote():
        profile, quote = india["yfinance"].profile_and_quote("TCS.NS")
        return {"company": "TCS.NS", "type": "profile+quote", "name": profile.get("name"),
                "exchange": profile.get("exchange"), "currency": quote.get("currency") or profile.get("currency"),
                "price_present": quote.get("price") is not None, "as_of": quote.get("as_of")}

    results["finnhub"] = timed("Finnhub", finnhub_quote)
    results["fmp"] = timed("Financial Modeling Prep", fmp_profile)
    results["yfinance"] = timed("Yahoo Finance (yfinance)", yfinance_quote)

    # Newest connectivity evidence (timestamped files; the older fixed name last).
    stamped = sorted(EVIDENCE.glob("azure_connectivity_*.json"))
    azure = load(stamped[-1].name if stamped else "azure_connectivity.json") or {}
    results["azure_openai"] = {
        "provider": "Azure OpenAI", "mode": "live_request (scripts/verify_azure.py)",
        "outcome": azure.get("outcome"), "checked_at_utc": azure.get("finished_at_utc"),
        "deployment": (azure.get("diagnostics") or {}).get("deployment"),
        "model_served": (azure.get("diagnostics") or {}).get("model"),
        "response_id": (azure.get("diagnostics") or {}).get("response_id"),
        "tokens": {k: (azure.get("diagnostics") or {}).get(k)
                   for k in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")},
        "structured_output_correct": azure.get("structured_output_correct"),
    }
    sec = load("sec_onboarding.json") or {}
    statuses = [((c.get("refresh") or {}).get("status")) for c in (sec.get("companies") or {}).values()]
    results["sec_edgar"] = {
        "provider": "SEC EDGAR", "mode": "live_request (scripts/onboard_sec_universe.py)",
        "checked_at_utc": sec.get("finished_at_utc"),
        "companies": len(sec.get("companies") or {}),
        "refresh_status_counts": {s: statuses.count(s) for s in set(statuses)},
        "validation_summary": sec.get("summary"),
    }
    market = load("market_onboarding.json") or {}
    news = market.get("news") or {}
    news_status = [v.get("status") for v in news.values()]
    results["newsdata"] = {
        "provider": "NewsData.io", "mode": "live_request (scripts/onboard_market_data.py)",
        "checked_at_utc": market.get("finished_at_utc"),
        "status_counts": {s: news_status.count(s) for s in set(news_status)},
        "articles_fetched": sum((v.get("records_fetched") or 0) for v in news.values()),
        "articles_accepted": sum((v.get("records_relevant") or 0) for v in news.values()),
        "articles_inserted": sum((v.get("records_inserted") or 0) for v in news.values()),
        "stopped_reason": market.get("news_stopped_reason"),
    }
    results["market_data_load"] = {
        "status_counts": market.get("market_status_counts"),
        "fmp_statement_plan_rejections_http_402": [f["ticker"] for f in market.get("fmp_statement_failures", [])],
    }

    out = EVIDENCE / "provider_verification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    for key, value in results.items():
        print(f"{key:16s} {json.dumps({k: v for k, v in value.items() if k not in ('data',)}, default=str)[:230]}")


if __name__ == "__main__":
    main()
