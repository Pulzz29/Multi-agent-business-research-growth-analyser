"""
Evidence-backed readiness matrix for every registered company (US and India).

Reads stored data only (no provider or AI calls). With --compute-forecasts it
also runs the genuine statistical forecasts for SEC-validated companies (the
same fingerprinted cache the research agents use).

Writes docs/COMPANY_READINESS_MATRIX.csv and a JSON copy in
docs/validation_evidence/company_readiness.json.

Readiness levels (derived, never asserted):
  fully_ready     research-eligible, financial evidence, market quote,
                  verified company news, and at least one validated forecast
  ready_limited   research-eligible with financial evidence and a quote, but
                  without verified news and/or a forecast (reasons listed)
  pending         registered but missing financial evidence or a quote
  blocked         not research-eligible (reason listed)
"""

import csv
import json
import os
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
warnings.filterwarnings("ignore")

COLUMNS = [
    "market", "ticker", "name", "exchange", "currency", "identity_verification", "cik",
    "sec_status", "sec_reason", "financial_source", "financial_quarters_revenue",
    "financial_quarters_ocf", "latest_financial_period", "provider_statement_rows",
    "quote_provider", "quote_fetched_at", "news_verified", "news_stored", "news_hidden",
    "forecast_revenue", "forecast_ocf", "interval_revenue", "interval_ocf",
    "research_profile", "saved_reports", "readiness", "limitations",
]


def main():
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.database.sqlite_manager import connect
    from app.services import market_registry as registry
    from app.services import market_storage
    from app.services.company_identity import identity_for
    from app.services.news_relevance import load_company_news
    from app.services.sec_coverage import validation_status

    compute = "--compute-forecasts" in sys.argv
    rows = []
    universe = [
        (market, c["symbol"])
        for market in ("US", "IN")
        for s in registry.market_config(market)["sectors"].values()
        for c in s["companies"]
    ]
    with connect() as conn:
        report_counts = dict(conn.execute(
            "SELECT ticker, COUNT(*) FROM research_execution_runs WHERE status='completed' GROUP BY ticker").fetchall())

    for market, ticker in universe:
        limitations = []
        row = {c: "" for c in COLUMNS}
        row.update(market=market, ticker=ticker)
        try:
            identity = identity_for(ticker)
            row.update(name=identity.display_name, exchange=identity.exchange or "",
                       currency=identity.currency, identity_verification=identity.verification,
                       cik=identity.cik or "")
        except Exception as exc:
            row.update(readiness="blocked", limitations=f"identity unavailable: {exc}")
            rows.append(row)
            continue

        sec = validation_status(ticker)
        row.update(sec_status=sec.get("status"), sec_reason=sec.get("reason") or "")
        profile = registry.research_profile(ticker)
        row["research_profile"] = profile or "none"
        row["saved_reports"] = report_counts.get(ticker, 0)

        has_financials = False
        if sec.get("status") == "validated":
            row["financial_source"] = "SEC EDGAR"
            from app.services import forecasting
            from app.services.financial_normalization import normalized_quarterly_financials

            fin = normalized_quarterly_financials(ticker)
            rev = forecasting.contiguous_recent_history(fin["revenue"])
            ocf = forecasting.contiguous_recent_history(fin["operating_cash_flow"])
            row.update(financial_quarters_revenue=len(rev), financial_quarters_ocf=len(ocf),
                       latest_financial_period=rev[-1]["period_end"] if rev else "")
            has_financials = bool(rev)
        statements = market_storage.statement_values(ticker, "annual")
        quarterly = market_storage.statement_values(ticker, "quarterly")
        row["provider_statement_rows"] = sum(len(v) for v in statements.values()) + sum(len(v) for v in quarterly.values())
        if not has_financials and row["provider_statement_rows"]:
            row["financial_source"] = "provider statements"
            row["latest_financial_period"] = max(list(statements) + list(quarterly))
            has_financials = True
        if not has_financials:
            limitations.append("no financial evidence stored")

        quote = market_storage.latest_quote(ticker)
        if quote:
            row.update(quote_provider=quote["provider"], quote_fetched_at=quote["fetched_at"])
        else:
            limitations.append("no market quote stored")

        with connect() as conn:
            _, news = load_company_news(conn, ticker, limit=1000)
        row.update(news_verified=news["accepted"], news_stored=news["stored"], news_hidden=news["hidden"])
        if not news["accepted"]:
            limitations.append("no verified company-specific news in the coverage window")

        forecasts_ok = False
        if sec.get("status") == "validated":
            from app.services import company_profile, forecast_cache
            from app.services.forecasting import ForecastingError

            for metric, col, icol in (("revenue", "forecast_revenue", "interval_revenue"),
                                      ("operating_cash_flow", "forecast_ocf", "interval_ocf")):
                if compute:
                    try:
                        forecast_cache.forecast_financial_metric_cached(ticker, metric)
                    except (ForecastingError, ValueError) as exc:
                        row[col] = "unavailable: " + str(exc)[:90]
                        row[icol] = "n/a"
                        continue
                    except Exception as exc:
                        row[col] = f"error: {type(exc).__name__}"
                        continue
                shown = company_profile.read_forecast(ticker, metric)
                if shown.get("available"):
                    v = shown.get("validation") or {}
                    row[col] = shown.get("selected_model")
                    row[icol] = f"{v.get('interval_status')} ({v.get('interval_hits')}/{v.get('interval_points')})"
                    forecasts_ok = True
                elif not row[col]:
                    row[col] = "unavailable: " + str(shown.get("reason"))[:90]
            if not forecasts_ok:
                limitations.append("no validated forecast")
        else:
            row.update(forecast_revenue="n/a (not SEC-validated)", forecast_ocf="n/a (not SEC-validated)")
            limitations.append("forecasts need SEC-reconciled history with filing dates")

        if profile is None:
            row["readiness"] = "blocked"
            limitations.insert(0, "not research-eligible (company data not registered)")
        elif not has_financials or not quote:
            row["readiness"] = "pending"
        elif news["accepted"] and forecasts_ok:
            row["readiness"] = "fully_ready"
        else:
            row["readiness"] = "ready_limited"
        row["limitations"] = "; ".join(limitations)
        rows.append(row)
        print(f"{market} {ticker:14s} {row['readiness']:13s} {row['limitations'][:100]}")

    out_csv = PROJECT_ROOT / "docs" / "COMPANY_READINESS_MATRIX.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    summary = {}
    for r in rows:
        key = (r["market"], r["readiness"])
        summary[f"{key[0]}:{key[1]}"] = summary.get(f"{key[0]}:{key[1]}", 0) + 1
    out_json = PROJECT_ROOT / "docs" / "validation_evidence" / "company_readiness.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps({"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                                    "summary": summary, "companies": rows}, indent=2, default=str), encoding="utf-8")
    print("summary:", summary)


if __name__ == "__main__":
    main()
