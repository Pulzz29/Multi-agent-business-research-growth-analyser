"""
Load genuine market data and company news for the universe, within quotas.

* Market data (profile, quote, statements) for every US and Indian universe
  company through the normal ingestion path (market_ingestion.refresh_many):
  per-source freshness rules skip data that is still fresh, provider rate
  limiters apply, and the FMP daily request budget is enforced by the client.
* Company news from NewsData.io (headline search on each company's verified
  names, strict relevance rules on every article), at most one page (one
  credit) per company, for at most --news-limit companies. The run stops
  requesting news at the first quota or rate-limit error and records the
  remaining companies as "news pending".

Evidence: docs/validation_evidence/market_onboarding.json (no credentials).

Usage: .venv\\Scripts\\python.exe scripts\\onboard_market_data.py [--news-limit=90] [--skip-market] [--skip-news]
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def arg(name, default=None):
    return next((a.split("=", 1)[1] for a in sys.argv if a.startswith(f"--{name}=")), default)


def main():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.database import sqlite_manager
    from app.services import market_ingestion, market_registry as registry
    from app.services import news_ingestion
    from app.services.news_ingestion import NewsIngestionError, refresh_news_company

    # One verified snapshot + recovery point for the whole batch instead of a
    # full timestamped backup per company (each write is still transactional).
    news_ingestion.sync_snapshot_to_drive = lambda *a, **k: {"integrity_ok": True, "deferred": True}
    news_ingestion.create_recovery_point = lambda *a, **k: {"verified": True, "deferred": True}

    evidence = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "market": {}, "news": {}}
    universe = {
        market: [c["symbol"] for s in registry.market_config(market)["sectors"].values() for c in s["companies"]]
        for market in ("US", "IN")
    }

    if "--skip-market" not in sys.argv:
        for market, symbols in universe.items():
            started = time.monotonic()
            outcome = market_ingestion.refresh_many(
                market, symbols, ("profile", "quote", "statements"), max_companies=len(symbols),
            )
            per_source = {}
            for symbol, results in outcome["outcomes"].items():
                if not isinstance(results, dict):
                    continue
                for source, result in results.items():
                    status = (result or {}).get("status", "error") if isinstance(result, dict) else "error"
                    per_source.setdefault(source, {}).setdefault(status, []).append(symbol)
            evidence["market"][market] = {
                "companies": len(symbols),
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "status_by_source": {s: {k: sorted(v) for k, v in d.items()} for s, d in per_source.items()},
                "recovery_point": bool((outcome.get("recovery_point") or {}).get("verified")),
            }
            print(market, {s: {k: len(v) for k, v in d.items()} for s, d in per_source.items()})

    if "--skip-news" not in sys.argv:
        from app.services.sec_coverage import SEC_VALIDATED

        limit = int(arg("news-limit", "90"))
        queue = [t for t in universe["US"] if t in SEC_VALIDATED] + universe["IN"]
        stopped_reason = None
        for index, ticker in enumerate(queue):
            if index >= limit:
                evidence["news"][ticker] = {"status": "pending", "reason": "news onboarding limit reached"}
                continue
            if stopped_reason:
                evidence["news"][ticker] = {"status": "pending", "reason": stopped_reason}
                continue
            try:
                result = asyncio.run(refresh_news_company(
                    ticker, max_articles=30, max_pages=1, force_refresh=False,
                    country="in" if ticker.endswith(".NS") else None,
                ))
                evidence["news"][ticker] = {k: result.get(k) for k in (
                    "status", "records_fetched", "records_relevant", "records_inserted",
                    "records_irrelevant", "irrelevant_by_reason", "existing_records")}
            except NewsIngestionError as exc:
                text = str(exc)
                evidence["news"][ticker] = {"status": "failed", "error": text[:200]}
                if "429" in text or "rate" in text.lower() or "limit" in text.lower() or "credit" in text.lower():
                    stopped_reason = f"NewsData.io quota or rate limit reached ({text[:80]})"
            except Exception as exc:
                evidence["news"][ticker] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            item = evidence["news"][ticker]
            print(f"news {ticker:14s} {item.get('status')!s:8s} fetched={item.get('records_fetched')} "
                  f"relevant={item.get('records_relevant')} inserted={item.get('records_inserted')} {item.get('error', '')[:90]}")
        evidence["news_stopped_reason"] = stopped_reason

    evidence["snapshot_integrity_ok"] = bool(sqlite_manager.sync_snapshot_to_drive().get("integrity_ok"))
    evidence["recovery_point_verified"] = bool(sqlite_manager.create_recovery_point("market_onboarding").get("verified"))
    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    out = PROJECT_ROOT / "docs" / "validation_evidence" / "market_onboarding.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print("evidence:", out.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    main()
