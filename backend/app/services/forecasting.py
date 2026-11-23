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


def backtest(values):
    """
    Two fixed rolling-origin evaluations:
    train through N-8, forecast next four;
    train through N-4, forecast next four.

    No test observation is used to train its own forecast.
    """
    n = len(values)

    cutoffs = (n - 2 * HORIZON, n - HORIZON)

    if cutoffs[0] < MIN_TRAIN:
        raise ForecastingError(
            "Not enough observations for rolling-origin backtests."
        )

    results = {
        "seasonal_naive": {
            "folds": [],
            "errors": [],
        },
    }

    for index, specification in enumerate(CANDIDATES):
        results[f"sarimax_{index + 1}"] = {
            "specification": specification,
            "folds": [],
            "errors": [],
            "failures": [],
        }

    for cutoff in cutoffs:
        train = values[:cutoff]
        actual = values[cutoff:cutoff + HORIZON]

        baseline = seasonal_naive(train)

        baseline_score = mae(actual, baseline)

        results["seasonal_naive"]["folds"].append({
            "train_length": len(train),
            "horizon": HORIZON,
            "mae_billion_usd": baseline_score,
        })

        results["seasonal_naive"]["errors"].extend(
            (actual - baseline).tolist()
        )

        for index, specification in enumerate(CANDIDATES):
            key = f"sarimax_{index + 1}"

            try:
                fitted, fit_diagnostics = fit_sarimax(
                    train,
                    specification,
                )

                predicted, _ = predict_sarimax(fitted)

                score = mae(actual, predicted)

                results[key]["folds"].append({
                    "train_length": len(train),
                    "horizon": HORIZON,
                    "mae_billion_usd": score,
                    "fit_diagnostics": fit_diagnostics,
                })

                results[key]["errors"].extend(
                    (actual - predicted).tolist()
                )

            except Exception as exc:
                results[key]["failures"].append({
                    "train_length": len(train),
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    )[:500],
                })

    baseline_mae = float(np.mean([
        fold["mae_billion_usd"]
        for fold in results["seasonal_naive"]["folds"]
    ]))

    results["seasonal_naive"]["mean_mae_billion_usd"] = (
        baseline_mae
    )

    for index in range(len(CANDIDATES)):
        key = f"sarimax_{index + 1}"
        model_result = results[key]

        if len(model_result["folds"]) == len(cutoffs):
            model_result["mean_mae_billion_usd"] = float(
                np.mean([
                    fold["mae_billion_usd"]
                    for fold in model_result["folds"]
                ])
            )
        else:
            model_result["mean_mae_billion_usd"] = None

    return results


def choose_candidate(backtest_results):
    baseline = backtest_results[
        "seasonal_naive"
    ]["mean_mae_billion_usd"]

    eligible = []

    for index in range(len(CANDIDATES)):
        key = f"sarimax_{index + 1}"
        score = backtest_results[key][
            "mean_mae_billion_usd"
        ]

        if score is not None and score < baseline:
            eligible.append((score, key, CANDIDATES[index]))

    if not eligible:
        return "seasonal_naive", None

    _, name, specification = min(
        eligible,
        key=lambda item: item[0],
    )

    return name, specification


def forecast_financial_metric(ticker, metric):
    if metric not in {
        "revenue",
        "operating_cash_flow",
    }:
        raise ValueError("Unsupported financial metric.")

    financials = normalized_quarterly_financials(ticker)

    history = contiguous_recent_history(
        financials[metric]
    )

    values = validate_history(history)

    backtest_results = backtest(values)

    selected, specification = choose_candidate(
        backtest_results
    )

    selection_reason = (
        "SARIMAX beat the seasonal-naive baseline "
        "on both rolling-origin folds in aggregate."
        if specification is not None
        else
        "Seasonal-naive baseline performed at least "
        "as well as the eligible SARIMAX candidates, "
        "or SARIMAX failed diagnostics."
    )

    final_diagnostics = None
    intervals = None

    if specification is not None:
        try:
            fitted, final_diagnostics = fit_sarimax(
                values,
                specification,
            )

            predicted, intervals = predict_sarimax(
                fitted,
                include_intervals=True,
            )

        except Exception as exc:
            selected = "seasonal_naive"
            specification = None
            selection_reason = (
                "SARIMAX final fit failed: "
                f"{type(exc).__name__}: {exc}. "
                "Used observed seasonal baseline."
            )
            final_diagnostics = {
                "fallback_error": str(exc)[:500]
            }
            predicted = seasonal_naive(values)

    else:
        predicted = seasonal_naive(values)

    # A negative revenue forecast is economically invalid.
    # Fallback is disclosed rather than silently clipping.
    if metric == "revenue" and np.any(predicted < 0):
        selected = "seasonal_naive"
        specification = None
        predicted = seasonal_naive(values)
        intervals = None
        selection_reason = (
            "SARIMAX predicted negative revenue; "
            "seasonal-naive fallback applied."
        )

    forecast = []

    for index, value in enumerate(predicted):
        row = {
            "fiscal_quarter_ahead": index + 1,
            "predicted_billion_usd": float(value),
            "lower_95_billion_usd": None,
            "upper_95_billion_usd": None,
        }

        if intervals is not None:
            row["lower_95_billion_usd"] = float(
                intervals[index, 0]
            )
            row["upper_95_billion_usd"] = float(
                intervals[index, 1]
            )

        forecast.append(row)

    return {
        "ticker": ticker,
        "metric": metric,
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
        "horizon_quarters": HORIZON,
        "history_quarters": len(history),
        "last_observed_period_end": history[-1][
            "period_end"
        ],
        "last_eight_sec_evidence": [
            {
                "period_end": item["period_end"],
                "value_usd": item["value"],
                "method": item["method"],
                "source_fact_ids": [
                    source["sec_fact_id"]
                    for source in item["evidence"]
                ],
            }
            for item in history[-8:]
        ],
        "selected_model": selected,
        "specification": specification,
        "selection_reason": selection_reason,
        "backtesting": backtest_results,
        "final_fit_diagnostics": final_diagnostics,
        "forecast": forecast,
        "interval_method": (
            "sarimax_model_based_95_percent"
            if intervals is not None
            else "unavailable"
        ),
        "caveats": [
            "Forecasts are estimates, not SEC reported facts.",
            "Two backtesting folds provide limited uncertainty evidence.",
            "Model-based intervals are not empirically calibrated.",
            "Historical normalization diagnostics remain documented.",
            "No missing financial quarters were interpolated.",
        ],
    }
