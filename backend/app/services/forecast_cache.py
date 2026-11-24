import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.core.config import get_settings
from app.services import financial_normalization as fin
from app.services import forecasting


CACHE_VERSION = 1
# Legacy default kept for readers of older envelopes. The active TTL
# comes from settings (INSIGHTIQ_FORECAST_CACHE_TTL_HOURS); the input
# fingerprint is what invalidates a forecast when SEC data changes.
CACHE_TTL_SECONDS = 24 * 60 * 60


def cache_ttl_seconds():
    try:
        return int(get_settings().forecast_cache_ttl_hours) * 3600
    except Exception:
        return CACHE_TTL_SECONDS
SUPPORTED_METRICS = frozenset({
    "revenue",
    "operating_cash_flow",
})


class ForecastCacheError(RuntimeError):
    pass


def canonical_json(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(value):
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()


def sha256_file(path):
    path = Path(path)

    if not path.is_file():
        raise ForecastCacheError(
            f"Required source file missing: {path.name}"
        )

    return hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def project_root():
    settings = get_settings()
    root = Path(settings.project_root).resolve()

    if not root.is_dir():
        raise ForecastCacheError(
            "Active project root does not exist."
        )

    return root


def cache_directory():
    root = project_root()
    target = root / "data" / "forecast_cache_v1"
    target.mkdir(parents=True, exist_ok=True)
    return target


def financial_review_digest():
    review_path = (
        project_root()
        / "reports"
        / "financial_evidence_reconciliation_final.json"
    )

    if not review_path.is_file():
        raise ForecastCacheError(
            "Cell 12B financial review is missing."
        )

    review = json.loads(
        review_path.read_text(encoding="utf-8")
    )

    if review.get("critical_issues"):
        raise ForecastCacheError(
            "Financial reconciliation contains critical issues."
        )

    return sha256_file(review_path)


def source_fingerprint(ticker, metric):
    if metric not in SUPPORTED_METRICS:
        raise ValueError("Unsupported financial metric.")

    financials = fin.normalized_quarterly_financials(
        ticker
    )

    history = forecasting.contiguous_recent_history(
        financials[metric]
    )

    # Also validates continuity and minimum history.
    forecasting.validate_history(history)

    # Include every observation the model trains on,
    # not merely the latest eight quarters.
    historical_inputs = [
        {
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "fiscal_quarter": row["fiscal_quarter"],
            "concept": row["concept"],
            "value": row["value"],
            "method": row["method"],
            "evidence": row["evidence"],
        }
        for row in history
    ]

    module_root = (
        project_root() / "backend" / "app" / "services"
    )

    identity = {
        "cache_version": CACHE_VERSION,
        "ticker": ticker,
        "metric": metric,
        "horizon_quarters": forecasting.HORIZON,
        "minimum_training_quarters": forecasting.MIN_TRAIN,
        "maximum_history_quarters": forecasting.MAX_HISTORY,
        "sarimax_candidates": forecasting.CANDIDATES,
        "historical_inputs": historical_inputs,
        "financial_normalization_code_sha256": sha256_file(
            module_root / "financial_normalization.py"
        ),
        "forecast_code_sha256": sha256_file(
            module_root / "forecasting.py"
        ),
        "financial_review_sha256": financial_review_digest(),
    }

    return sha256_text(canonical_json(identity))


def cache_path(ticker, metric):
    if metric not in SUPPORTED_METRICS:
        raise ValueError("Unsupported financial metric.")

    # Explicit symbol allowlist prevents unsafe filenames.
    supported = {
        "AAPL", "AMZN", "GOOGL",
        "META", "MSFT", "NVDA",
    }

    if ticker not in supported:
        raise ValueError("Unsupported company ticker.")

    return (
        cache_directory()
        / f"{ticker}_{metric}.json"
    )


def validate_cached_result(result, ticker, metric):
    if not isinstance(result, dict):
        return False

    if (
        result.get("ticker") != ticker
        or result.get("metric") != metric
        or result.get("horizon_quarters")
            != forecasting.HORIZON
    ):
        return False

    forecast = result.get("forecast")

    if not isinstance(forecast, list):
        return False

    if len(forecast) != forecasting.HORIZON:
        return False

    if not isinstance(
        result.get("backtesting"), dict
    ):
        return False

    if len(
        result.get("last_eight_sec_evidence", [])
    ) != 8:
        return False

    return True


def read_cached_forecast(ticker, metric, fingerprint):
    path = cache_path(ticker, metric)

    if not path.is_file():
        return None, "cache_miss"

    try:
        envelope = json.loads(
            path.read_text(encoding="utf-8")
        )

        if envelope["version"] != CACHE_VERSION:
            return None, "cache_version_changed"

        if envelope["fingerprint"] != fingerprint:
            return None, "source_or_code_changed"

        generated = datetime.fromisoformat(
            envelope["cached_at_utc"]
        )

        if generated.tzinfo is None:
            return None, "invalid_cache_timestamp"

        age = (
            datetime.now(timezone.utc)
            - generated.astimezone(timezone.utc)
        ).total_seconds()

        if age < 0 or age > cache_ttl_seconds():
            return None, "cache_expired"

        result = envelope["result"]

        if not validate_cached_result(
            result, ticker, metric
        ):
            return None, "invalid_cached_forecast"

        actual_hash = sha256_text(
            canonical_json(result)
        )

        if actual_hash != envelope["result_sha256"]:
            return None, "cache_integrity_mismatch"

        return result, "cache_hit"

    except (
        OSError, ValueError, TypeError,
        KeyError, json.JSONDecodeError,
    ):
        return None, "cache_unreadable"


def write_cached_forecast(
    ticker, metric, fingerprint, result
):
    if not validate_cached_result(
        result, ticker, metric
    ):
        raise ForecastCacheError(
            "Refusing to persist invalid forecast."
        )

    path = cache_path(ticker, metric)

    envelope = {
        "version": CACHE_VERSION,
        "ticker": ticker,
        "metric": metric,
        "fingerprint": fingerprint,
        "cached_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "ttl_seconds": cache_ttl_seconds(),
        "result_sha256": sha256_text(
            canonical_json(result)
        ),
        "result": result,
    }

    content = canonical_json(envelope)

    # Unique temporary file avoids filename collisions.
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}."
        f"{time.time_ns()}.tmp"
    )

    try:
        with temporary.open(
            "x", encoding="utf-8"
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, path)

    finally:
        if temporary.exists():
            temporary.unlink()

    # Verify that the actual persisted result is readable.
    restored, status = read_cached_forecast(
        ticker, metric, fingerprint
    )

    if status != "cache_hit" or canonical_json(restored) != canonical_json(result):
        raise ForecastCacheError(
            "Persisted forecast integrity verification failed."
        )

    return path


def forecast_financial_metric_cached(
    ticker, metric, force_refresh=False
):
    started = time.perf_counter()
    fingerprint_before = source_fingerprint(
        ticker, metric
    )

    invalidation_reason = (
        "force_refresh"
        if force_refresh else None
    )

    if not force_refresh:
        cached, status = read_cached_forecast(
            ticker, metric, fingerprint_before
        )

        if cached is not None:
            return {
                "forecast": cached,
                "cache_status": "hit",
                "cache_reason": status,
                "cache_fingerprint": fingerprint_before,
                "elapsed_seconds": round(
                    time.perf_counter() - started,
                    4,
                ),
            }

        invalidation_reason = status

    result = forecasting.forecast_financial_metric(
        ticker, metric
    )

    # Prevent saving a forecast if source evidence changes
    # between model preparation and cache persistence.
    fingerprint_after = source_fingerprint(
        ticker, metric
    )

    if fingerprint_after != fingerprint_before:
        raise ForecastCacheError(
            "Source evidence changed during forecasting."
        )

    path = write_cached_forecast(
        ticker, metric,
        fingerprint_after, result
    )

    return {
        "forecast": result,
        "cache_status": "miss",
        "cache_reason": invalidation_reason,
        "cache_fingerprint": fingerprint_after,
        "cache_file": str(path),
        "elapsed_seconds": round(
            time.perf_counter() - started,
            4,
        ),
    }
