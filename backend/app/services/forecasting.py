import warnings
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
from statsmodels.tsa.statespace.sarimax import SARIMAX

from app.services.financial_normalization import (
    normalized_quarterly_financials,
)


HORIZON = 4
SEASON_LENGTH = 4
MIN_TRAIN = 16
MAX_HISTORY = 48

# Candidate model specifications, not hardcoded predictions.
CANDIDATES = (
    {
        "order": (1, 1, 0),
        "seasonal_order": (0, 1, 1, 4),
    },
    {
        "order": (0, 1, 1),
        "seasonal_order": (1, 0, 0, 4),
    },
)


class ForecastingError(RuntimeError):
    pass


def consecutive_quarters(previous, current):
    from datetime import date, timedelta

    previous_end = date.fromisoformat(previous["period_end"])
    current_start = date.fromisoformat(current["period_start"])

    return current_start == previous_end + timedelta(days=1)


def contiguous_recent_history(quarters):
    """
    Only use the most recent consecutive quarterly series.
    Never interpolate or invent missing SEC quarters.
    """
    ordered = sorted(
        quarters,
        key=lambda row: row["period_end"],
    )

    if not ordered:
        return []

    contiguous = [ordered[-1]]

    for row in reversed(ordered[:-1]):
        if consecutive_quarters(row, contiguous[0]):
            contiguous.insert(0, row)
        else:
            break

    return contiguous[-MAX_HISTORY:]


def validate_history(history):
    if len(history) < MIN_TRAIN + HORIZON:
        raise ForecastingError(
            f"Insufficient continuous quarters: {len(history)}. "
            f"At least {MIN_TRAIN + HORIZON} required."
        )

    values = np.array(
        [
            float(
                Decimal(row["value"])
                / Decimal("1000000000")
            )
            for row in history
        ],
        dtype=np.float64,
    )

    if not np.all(np.isfinite(values)):
        raise ForecastingError(
            "Nonfinite financial values in input series."
        )

    for previous, current in zip(history, history[1:]):
        if not consecutive_quarters(previous, current):
            raise ForecastingError(
                "Financial history contains missing quarters."
            )

    return values


def seasonal_naive(train, horizon=HORIZON):
    if len(train) < SEASON_LENGTH:
        raise ForecastingError(
            "Seasonal baseline needs four quarters."
        )

    cycle = np.asarray(
        train[-SEASON_LENGTH:],
        dtype=np.float64,
    )

    return np.resize(cycle, horizon)


def mae(actual, predicted):
    actual = np.asarray(actual, dtype=np.float64)
    predicted = np.asarray(predicted, dtype=np.float64)

    return float(np.mean(np.abs(actual - predicted)))


def fit_sarimax(train, specification):
    """
    Failed or nonconverged fits must not be used.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        model = SARIMAX(
            np.asarray(train, dtype=np.float64),
            order=specification["order"],
            seasonal_order=specification["seasonal_order"],
            trend="n",
            enforce_stationarity=False,
            enforce_invertibility=False,
        )

        fitted = model.fit(
            disp=False,
            maxiter=70,
        )

    optimizer = getattr(
        fitted, "mle_retvals", {}
    ) or {}

    converged = bool(
        optimizer.get("converged", False)
    )

    diagnostic = {
        "converged": converged,
        "aic": (
            float(fitted.aic)
            if np.isfinite(fitted.aic) else None
        ),
        "iterations": optimizer.get("iterations"),
        "warnings": [
            str(warning.message)[:300]
            for warning in caught
        ][:10],
    }

    if not converged:
        raise ForecastingError(
            "SARIMAX optimizer did not converge."
        )

    return fitted, diagnostic


def predict_sarimax(
    fitted,
    horizon=HORIZON,
    include_intervals=False,
):
    prediction = fitted.get_forecast(steps=horizon)

    values = np.asarray(
        prediction.predicted_mean,
        dtype=np.float64,
    ).reshape(-1)

    if (
        len(values) != horizon
        or not np.all(np.isfinite(values))
    ):
        raise ForecastingError(
            "SARIMAX produced invalid forecast values."
        )

    intervals = None

    if include_intervals:
        raw_intervals = np.asarray(
            prediction.conf_int(alpha=0.05),
            dtype=np.float64,
        )

        if (
            raw_intervals.shape == (horizon, 2)
            and np.all(np.isfinite(raw_intervals))
        ):
            intervals = raw_intervals

    return values, intervals


# ------------------------------------------------- point-in-time walk-forward
#
# Origins (n = contiguous quarters available):
#   selection folds : train on quarters [0, n-12) and [0, n-8), forecast 4
#   evaluation fold : train on quarters [0, n-4), forecast the last 4
# The model is chosen on the selection folds only; its accuracy and the
# baseline's are then reported on the later evaluation fold, which played no
# part in the choice. Every fold trains on the series exactly as it could have
# been built from SEC filings available at that time (facts filed on or before
# the first filing that reported the fold's last training quarter), so later
# restatements never leak into a backtest. Actuals are today's values.

SELECTION_OFFSETS = (3 * HORIZON, 2 * HORIZON)
EVALUATION_OFFSET = HORIZON
MIN_HISTORY = MIN_TRAIN + 3 * HORIZON  # 28 contiguous quarters
INTERVAL_MIN_POINTS = 8
INTERVAL_MIN_COVERAGE = 0.75


def first_reported_date(ticker, metric, period_end):
    """Earliest SEC filing date of any fact ending on ``period_end`` for the metric."""
    from app.database.sqlite_manager import connect
    from app.services import financial_normalization as fin

    concepts = (fin.OCF_CONCEPT,) if metric == "operating_cash_flow" else fin.REVENUE_CONCEPTS
    placeholders = ",".join("?" for _ in concepts)
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT MIN(filed_at) FROM sec_facts
            WHERE company_ticker = ? AND unit = 'USD' AND period_end = ?
              AND concept IN ({placeholders}) AND filed_at IS NOT NULL
            """,
            (ticker, period_end, *concepts),
        ).fetchone()
    return row[0] if row and row[0] else None


def point_in_time_training(ticker, metric, history, origin, cache):
    """Training values for a fold origin, built only from filings available then."""
    cutoff_end = history[origin - 1]["period_end"]
    as_of = first_reported_date(ticker, metric, cutoff_end)
    if not as_of:
        raise ForecastingError(f"No filing date for {cutoff_end}.")
    if as_of not in cache:
        try:
            cache[as_of] = normalized_quarterly_financials(ticker, as_of=as_of)[metric]
        except Exception as exc:
            raise ForecastingError(f"Point-in-time normalisation failed: {type(exc).__name__}") from None
    series = [row for row in cache[as_of] if row["period_end"] <= cutoff_end]
    series = contiguous_recent_history(series)
    if not series or series[-1]["period_end"] != cutoff_end:
        raise ForecastingError(f"Quarter ending {cutoff_end} was not yet derivable from filings.")
    if len(series) < MIN_TRAIN:
        raise ForecastingError("Too few point-in-time training quarters.")
    values = np.array([float(Decimal(r["value"]) / Decimal("1000000000")) for r in series], dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise ForecastingError("Nonfinite point-in-time values.")
    return values, as_of


def _run_fold(train, actual, origin_info):
    """Baseline and every SARIMAX candidate on one fold (with 95% intervals)."""
    out = {}
    baseline = seasonal_naive(train)
    out["seasonal_naive"] = {
        **origin_info,
        "train_length": len(train),
        "horizon": HORIZON,
        "mae_billion_usd": mae(actual, baseline),
        "errors": (actual - baseline).tolist(),
    }
    for index, specification in enumerate(CANDIDATES):
        key = f"sarimax_{index + 1}"
        try:
            fitted, fit_diagnostics = fit_sarimax(train, specification)
            predicted, intervals = predict_sarimax(fitted, include_intervals=True)
            covered = None
            if intervals is not None:
                covered = int(np.sum((actual >= intervals[:, 0]) & (actual <= intervals[:, 1])))
            out[key] = {
                **origin_info,
                "train_length": len(train),
                "horizon": HORIZON,
                "mae_billion_usd": mae(actual, predicted),
                "errors": (actual - predicted).tolist(),
                "interval_points": int(len(actual)) if intervals is not None else 0,
                "interval_hits": covered,
                "fit_diagnostics": fit_diagnostics,
            }
        except Exception as exc:
            out[key] = {**origin_info, "train_length": len(train),
                        "failure": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def walk_forward(ticker, metric, history, values, point_in_time=True):
    n = len(values)
    cache = {}
    models = ["seasonal_naive"] + [f"sarimax_{i + 1}" for i in range(len(CANDIDATES))]
    folds = {"selection": [], "evaluation": []}

    for kind, offsets in (("selection", SELECTION_OFFSETS), ("evaluation", (EVALUATION_OFFSET,))):
        for offset in offsets:
            origin = n - offset
            info = {"origin_period_end": history[origin - 1]["period_end"]}
            try:
                if point_in_time:
                    train, as_of = point_in_time_training(ticker, metric, history, origin, cache)
                    info["training_data_as_of"] = as_of
                else:
                    train = values[:origin]
                    info["training_data_as_of"] = "current_restated_values"
            except ForecastingError as exc:
                folds[kind].append({"origin_period_end": info["origin_period_end"], "unavailable": str(exc)})
                continue
            actual = values[origin:origin + HORIZON]
            folds[kind].append(_run_fold(train, actual, info))

    # Per-model selection summary (shape kept compatible with earlier caches).
    backtesting = {}
    for model in models:
        entries = [f[model] for f in folds["selection"] if model in f]
        ok = [e for e in entries if "mae_billion_usd" in e]
        backtesting[model] = {
            "specification": CANDIDATES[int(model[-1]) - 1] if model != "seasonal_naive" else None,
            "folds": [{k: v for k, v in e.items() if k != "errors"} for e in ok],
            "errors": [x for e in ok for x in e["errors"]],
            "failures": [{"origin_period_end": e["origin_period_end"], "error": e["failure"]}
                         for e in entries if "failure" in e],
            "mean_mae_billion_usd": (
                float(np.mean([e["mae_billion_usd"] for e in ok]))
                if len(ok) == len(SELECTION_OFFSETS) else None
            ),
        }
    return backtesting, folds


def choose_candidate(backtest_results):
    baseline = backtest_results["seasonal_naive"]["mean_mae_billion_usd"]
    if baseline is None:
        return "seasonal_naive", None
    eligible = []
    for index in range(len(CANDIDATES)):
        key = f"sarimax_{index + 1}"
        score = backtest_results[key]["mean_mae_billion_usd"]
        if score is not None and score < baseline:
            eligible.append((score, key, CANDIDATES[index]))
    if not eligible:
        return "seasonal_naive", None
    _, name, specification = min(eligible, key=lambda item: item[0])
    return name, specification


def validation_summary(selected, folds):
    """Held-out accuracy and empirical interval coverage (computed, never assumed)."""
    evaluation = next((f for f in folds["evaluation"] if "seasonal_naive" in f), None)
    summary = {
        "method": "point_in_time_walk_forward",
        "selection_folds": sum(1 for f in folds["selection"] if "seasonal_naive" in f),
        "evaluation_origin_period_end": folds["evaluation"][0].get("origin_period_end") if folds["evaluation"] else None,
        "evaluation_available": evaluation is not None,
        "evaluation_unavailable_reason": (
            None if evaluation is not None
            else next((f.get("unavailable") for f in folds["evaluation"] if f.get("unavailable")),
                      "No evaluation fold could be run.")
        ),
        "selected_model": selected,
        "selected_mae_billion_usd": None,
        "baseline_mae_billion_usd": None,
        "beats_baseline": None,
        "holdout_quarters": HORIZON if evaluation is not None else 0,
        "interval_points": 0,
        "interval_hits": 0,
        "interval_coverage": None,
        "interval_status": "unavailable",
        "interval_display_allowed": False,
        "interval_warning": None,
    }
    if evaluation is not None:
        baseline = evaluation["seasonal_naive"]["mae_billion_usd"]
        chosen = evaluation.get(selected) or {}
        selected_mae = chosen.get("mae_billion_usd")
        if selected == "seasonal_naive":
            selected_mae = baseline
        summary.update({"baseline_mae_billion_usd": baseline, "selected_mae_billion_usd": selected_mae})
        if selected != "seasonal_naive" and selected_mae is not None:
            summary["beats_baseline"] = bool(selected_mae < baseline)

    if selected == "seasonal_naive":
        summary["interval_warning"] = "The seasonal-naive baseline has no statistical prediction interval."
        return summary

    points = hits = 0
    for fold in folds["selection"] + folds["evaluation"]:
        entry = fold.get(selected) or {}
        if entry.get("interval_hits") is not None:
            points += entry["interval_points"]
            hits += entry["interval_hits"]
    summary["interval_points"] = points
    summary["interval_hits"] = hits
    if points:
        coverage = hits / points
        summary["interval_coverage"] = round(coverage, 4)
        if points >= INTERVAL_MIN_POINTS and coverage >= INTERVAL_MIN_COVERAGE:
            summary["interval_status"] = "validated_limited_sample"
            summary["interval_display_allowed"] = True
            summary["interval_warning"] = (
                f"Nominal 95% interval covered {hits} of {points} out-of-sample quarters; "
                "a small sample, so treat the interval as indicative."
            )
        else:
            summary["interval_status"] = "not_validated"
            summary["interval_warning"] = (
                f"Nominal 95% interval covered only {hits} of {points} out-of-sample quarters; "
                "the interval is withheld."
            )
    else:
        summary["interval_status"] = "not_validated"
        summary["interval_warning"] = "No out-of-sample interval could be evaluated; the interval is withheld."
    return summary


def forecast_financial_metric(ticker, metric, point_in_time=True):
    if metric not in {"revenue", "operating_cash_flow"}:
        raise ValueError("Unsupported financial metric.")

    financials = normalized_quarterly_financials(ticker)
    history = contiguous_recent_history(financials[metric])
    values = validate_history(history)
    if len(values) < MIN_HISTORY:
        raise ForecastingError(
            f"Insufficient continuous quarters: {len(values)}. At least {MIN_HISTORY} required "
            "for model selection plus an independent held-out evaluation."
        )

    backtest_results, folds = walk_forward(ticker, metric, history, values, point_in_time)
    selected, specification = choose_candidate(backtest_results)
    selection_reason = (
        "SARIMAX beat the seasonal-naive baseline on the point-in-time model-selection folds."
        if specification is not None else
        "Seasonal-naive baseline performed at least as well as the SARIMAX candidates on the "
        "model-selection folds, or SARIMAX failed diagnostics."
    )

    final_diagnostics = None
    intervals = None
    if specification is not None:
        try:
            fitted, final_diagnostics = fit_sarimax(values, specification)
            predicted, intervals = predict_sarimax(fitted, include_intervals=True)
        except Exception as exc:
            selected, specification = "seasonal_naive", None
            selection_reason = (
                f"SARIMAX final fit failed: {type(exc).__name__}: {exc}. Used observed seasonal baseline."
            )
            final_diagnostics = {"fallback_error": str(exc)[:500]}
            predicted = seasonal_naive(values)
    else:
        predicted = seasonal_naive(values)

    if metric == "revenue" and np.any(predicted < 0):
        selected, specification = "seasonal_naive", None
        predicted = seasonal_naive(values)
        intervals = None
        selection_reason = "SARIMAX predicted negative revenue; seasonal-naive fallback applied."

    validation = validation_summary(selected, folds)
    if intervals is not None and not validation["interval_display_allowed"]:
        intervals = None  # unvalidated intervals are never published

    forecast = []
    for index, value in enumerate(predicted):
        row = {
            "fiscal_quarter_ahead": index + 1,
            "predicted_billion_usd": float(value),
            "lower_95_billion_usd": None,
            "upper_95_billion_usd": None,
        }
        if intervals is not None:
            row["lower_95_billion_usd"] = float(intervals[index, 0])
            row["upper_95_billion_usd"] = float(intervals[index, 1])
        forecast.append(row)

    return {
        "ticker": ticker,
        "metric": metric,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "horizon_quarters": HORIZON,
        "history_quarters": len(history),
        "last_observed_period_end": history[-1]["period_end"],
        "last_eight_sec_evidence": [
            {
                "period_end": item["period_end"],
                "value_usd": item["value"],
                "method": item["method"],
                "source_fact_ids": [source["sec_fact_id"] for source in item["evidence"]],
            }
            for item in history[-8:]
        ],
        "selected_model": selected,
        "specification": specification,
        "selection_reason": selection_reason,
        "backtesting": backtest_results,
        "validation": validation,
        "final_fit_diagnostics": final_diagnostics,
        "forecast": forecast,
        "interval_method": (
            "sarimax_model_based_95_percent_empirically_checked"
            if intervals is not None else "unavailable"
        ),
        "caveats": [
            "Forecasts are statistical estimates, not SEC-reported facts.",
            "The model is chosen on two point-in-time folds and evaluated on a later held-out fold.",
            "Backtests use only SEC filings available at each fold's origin (no later restatements).",
            "Prediction intervals are shown only when their out-of-sample coverage passed the gate.",
            "No missing financial quarters were interpolated.",
        ],
    }
