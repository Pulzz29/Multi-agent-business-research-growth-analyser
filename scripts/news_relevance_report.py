"""
Apply the current strict relevance rules to every stored article (read-only)
and record, per company, what is shown and what is hidden and why.
Nothing is deleted or modified. Output:
docs/validation_evidence/news_relevance_stored.json
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def main():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))
    from app.database.sqlite_manager import connect
    from app.services.company_identity import identity_for
    from app.services.news_relevance import RELEVANCE_VERSION, filter_company_news

    with connect() as conn:
        tickers = [r[0] for r in conn.execute("SELECT DISTINCT company_ticker FROM news_articles ORDER BY 1")]
        report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                  "relevance_version": RELEVANCE_VERSION, "companies": {}}
        totals = Counter()
        for ticker in tickers:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, company_ticker, provider, title, description, article_url, published_at "
                "FROM news_articles WHERE company_ticker = ?", (ticker,))]
            try:
                identity = identity_for(ticker)
            except Exception as exc:
                report["companies"][ticker] = {"error": f"identity unavailable: {exc}"}
                continue
            # The coverage window is reported separately from relevance here.
            accepted, rejected = filter_company_news(rows, identity, max_age_days=3650)
            reasons = Counter(r["reason"] for r in rejected)
            totals.update({"stored": len(rows), "accepted": len(accepted), "hidden": len(rejected)})
            totals.update({f"hidden:{k}": v for k, v in reasons.items()})
            report["companies"][ticker] = {
                "stored": len(rows),
                "accepted": len(accepted),
                "hidden": len(rejected),
                "hidden_by_reason": dict(reasons),
                "accepted_examples": [
                    {"id": r["id"], "title": r["title"], "event_type": r["relevance"]["event_type"],
                     "reason": r["relevance"]["reason"]} for r in accepted[:5]],
                "hidden_examples": [
                    {"id": r["row"]["id"], "title": r["row"]["title"], "reason": r["reason"]}
                    for r in rejected[:6]],
            }
    report["totals"] = dict(totals)
    out = PROJECT_ROOT / "docs" / "validation_evidence" / "news_relevance_stored.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["totals"], indent=1))


if __name__ == "__main__":
    main()
