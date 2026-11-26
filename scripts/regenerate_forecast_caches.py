"""
Recompute forecast caches whose source fingerprint no longer matches the
stored SEC data or forecasting code (local SARIMAX; no network, no AI).

Uses the application's own invalidation path
(forecast_cache.forecast_financial_metric_cached): a cache whose fingerprint
matches is left untouched; a stale one is recomputed, verified and stored.

Usage: .venv\\Scripts\\python.exe scripts\\regenerate_forecast_caches.py [--tickers AAPL,MSFT] [--env-file PATH]
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(PROJECT_ROOT / ".env"))
    parser.add_argument("--tickers", default="AAPL,AMZN,GOOGL,META,MSFT,NVDA")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=False)
    os.environ.setdefault("INSIGHTIQ_PROJECT_ROOT", str(PROJECT_ROOT))

    from app.services import forecast_cache

    failures = 0
    for ticker in [t.strip().upper() for t in args.tickers.split(",") if t.strip()]:
        for metric in sorted(forecast_cache.SUPPORTED_METRICS):
            fingerprint = forecast_cache.source_fingerprint(ticker, metric)
            envelope = forecast_cache.read_envelope(ticker, metric)
            if envelope and envelope.get("fingerprint") == fingerprint:
                print(f"{ticker} {metric}: current")
                continue
            try:
                outcome = forecast_cache.forecast_financial_metric_cached(ticker, metric, force_refresh=True)
                print(f"{ticker} {metric}: regenerated ({outcome['elapsed_seconds']} s)")
            except Exception as exc:
                failures += 1
                print(f"{ticker} {metric}: FAILED {type(exc).__name__}: {str(exc)[:200]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
