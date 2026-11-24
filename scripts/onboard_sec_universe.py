"""
Load genuine SEC EDGAR financial facts for the US universe and validate them.

For every US universe company (or the tickers given on the command line):
  1. resolve the company's CIK from SEC's company_tickers.json (downloaded once);
  2. load its SEC companyfacts through the normal ingestion pipeline
     (refresh_sec_company: provenance, duplicate prevention, verified backup);
  3. validate it (sec_coverage.validate_company): quarters must reconcile to
     10-K annual totals, with enough contiguous quarters - otherwise the
     company is recorded as "unavailable" with the exact reason.

Requests are sequential (about one company per second, ~2 requests each),
identified by SEC_USER_AGENT, well within SEC's fair-access limit. Evidence is
written to docs/validation_evidence/sec_onboarding.json. No credentials are
printed.

Usage: .venv\\Scripts\\python.exe scripts\\onboard_sec_universe.py [TICKER ...]
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


async def main(selected):
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    import httpx

    from app.core.config import get_settings
    from app.database.sqlite_manager import transaction
    from app.services import market_registry as registry
    from app.services import sec_coverage
    from app.database import sqlite_manager
    from app.services import sec_ingestion
    from app.services.sec_ingestion import SecClient, refresh_sec_company

    # One verified snapshot + recovery point for the whole batch instead of a
    # full timestamped backup per company (each write is still transactional).
    real_snapshot, real_recovery = sec_ingestion.sync_snapshot_to_drive, sec_ingestion.create_recovery_point
    sec_ingestion.sync_snapshot_to_drive = lambda *a, **k: {"integrity_ok": True, "deferred": True}
    sec_ingestion.create_recovery_point = lambda *a, **k: {"verified": True, "deferred": True}

    sec_coverage.ensure_table()
    universe = [
        c["symbol"]
        for sector in registry.market_config("US")["sectors"].values()
        for c in sector["companies"]
    ]
    tickers = [t.upper() for t in selected] if selected else universe
    evidence = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "companies": {}}

    settings = get_settings()
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds, connect=15.0),
                                 follow_redirects=False) as http:
        sec_registry = await SecClient().fetch_registry(http)
    evidence["sec_registry_entries"] = len(sec_registry)

    for ticker in tickers:
        started = time.monotonic()
        record = {"ticker": ticker}
        entry = sec_registry.get(ticker)
        if not entry:
            record.update(status="not_in_sec_registry")
            evidence["companies"][ticker] = record
            print(f"{ticker:6s} not in SEC registry")
            continue
        record.update(cik=entry["cik"], sec_name=entry["name"])
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO companies(ticker, name, cik, created_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET cik = excluded.cik
                WHERE companies.cik IS NULL OR companies.cik = ''
                """,
                (ticker, entry["name"], entry["cik"], datetime.now(timezone.utc).isoformat()),
            )
        try:
            result = await refresh_sec_company(ticker)
            record["refresh"] = {k: result.get(k) for k in (
                "status", "records_fetched", "records_inserted", "existing_records", "last_refreshed_at")}
        except Exception as exc:
            record["refresh"] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        validation = sec_coverage.validate_company(ticker)
        record["validation"] = {k: validation[k] for k in (
            "status", "reason", "revenue_quarters", "ocf_quarters", "latest_period_end", "diagnostics_count")}
        record["elapsed_seconds"] = round(time.monotonic() - started, 2)
        evidence["companies"][ticker] = record
        print(f"{ticker:6s} refresh={record['refresh'].get('status'):8s} "
              f"validation={validation['status']:11s} rev_q={validation['revenue_quarters']:>2} "
              f"ocf_q={validation['ocf_quarters']:>2} {validation['reason'][:80]}")
        await asyncio.sleep(max(0.0, 1.0 - (time.monotonic() - started)))

    sec_ingestion.sync_snapshot_to_drive, sec_ingestion.create_recovery_point = real_snapshot, real_recovery
    snapshot = sqlite_manager.sync_snapshot_to_drive(create_timestamped_backup=False)
    recovery = sqlite_manager.create_recovery_point("sec_universe_onboarding")
    evidence["snapshot_integrity_ok"] = bool(snapshot.get("integrity_ok"))
    evidence["recovery_point_verified"] = bool(recovery.get("verified"))
    evidence["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    statuses = [c.get("validation", {}).get("status") for c in evidence["companies"].values()]
    evidence["summary"] = {s: statuses.count(s) for s in set(statuses)}
    out = PROJECT_ROOT / "docs" / "validation_evidence" / "sec_onboarding.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print("summary:", evidence["summary"], "| evidence:", out.relative_to(PROJECT_ROOT))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
