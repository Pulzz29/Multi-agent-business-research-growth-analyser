import math


class ForecastQualityError(ValueError):
    pass


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def assess_holdout(evaluation):
    observations = evaluation.get("observations", [])

    if len(observations) != 4:
        raise ForecastQualityError(
            "Expected four independent holdout observations."
        )

    absolute_errors = []
    baseline_errors = []
    interval_hits = []
    interval_details = []

    for observation in observations:
        actual = observation["actual_billion_usd"]
        predicted = observation["selected_prediction_billion_usd"]
        baseline = observation[
            "seasonal_naive_prediction_billion_usd"
        ]

        if not all(finite(x) for x in (
            actual, predicted, baseline
        )):
            raise ForecastQualityError(
                "Nonfinite holdout observations."
            )

        if not observation.get("actual_sec_fact_ids"):
            raise ForecastQualityError(
                "Holdout observation lacks SEC evidence."
            )

        absolute_errors.append(abs(actual - predicted))
        baseline_errors.append(abs(actual - baseline))

        lower = observation.get("lower_95_billion_usd")
        upper = observation.get("upper_95_billion_usd")

        if (lower is None) != (upper is None):
            raise ForecastQualityError(
                "Incomplete prediction interval."
            )

        if lower is not None:
            if (
                not finite(lower)
                or not finite(upper)
                or lower > upper
            ):
                raise ForecastQualityError(
                    "Invalid prediction interval bounds."
                )

            covered = lower <= actual <= upper

            interval_hits.append(covered)
            interval_details.append({
                "period_end": observation["period_end"],
                "actual_billion_usd": actual,
                "lower_95_billion_usd": lower,
                "upper_95_billion_usd": upper,
                "covered": covered,
            })

    calculated_mae = sum(absolute_errors) / 4
    calculated_baseline_mae = sum(baseline_errors) / 4

    if not math.isclose(
        calculated_mae,
        evaluation["holdout_selected_mae_billion_usd"],
        rel_tol=1e-9,
        abs_tol=1e-8,
    ):
        raise ForecastQualityError(
            "Saved selected-model holdout MAE does not reconcile."
        )

    if not math.isclose(
        calculated_baseline_mae,
        evaluation["holdout_baseline_mae_billion_usd"],
        rel_tol=1e-9,
        abs_tol=1e-8,
    ):
        raise ForecastQualityError(
            "Saved baseline holdout MAE does not reconcile."
        )

    intervals_available = (
        len(interval_hits) == len(observations)
    )

    if interval_hits and not intervals_available:
        raise ForecastQualityError(
            "Only some holdout observations have intervals."
        )

    actual_coverage = (
        sum(interval_hits) / len(interval_hits)
        if intervals_available else None
    )

    expected_coverage = evaluation.get(
        "interval_coverage"
    )

    if actual_coverage is None:
        if expected_coverage is not None:
            raise ForecastQualityError(
                "Saved interval coverage is inconsistent."
            )
    elif (
        expected_coverage is None
        or not math.isclose(
            actual_coverage,
            expected_coverage,
            abs_tol=1e-12,
        )
    ):
        raise ForecastQualityError(
            "Saved interval coverage does not reconcile."
        )

    # This is a conservative product-display policy,
    # not a claim of statistically calibrated coverage.
    if actual_coverage is None:
        status = "interval_unavailable"
    elif actual_coverage < 0.50:
        status = "poor_observed_coverage"
    else:
        status = "limited_validation"

    interval_display_allowed = (
        status == "limited_validation"
    )

    return {
        "ticker": evaluation["ticker"],
        "metric": evaluation["metric"],
        "selected_model": evaluation["selected_model"],
        "holdout_quarters": len(observations),
        "selected_mae_billion_usd": calculated_mae,
        "baseline_mae_billion_usd": calculated_baseline_mae,
        "beats_baseline": (
            calculated_mae < calculated_baseline_mae
        ),
        "interval_coverage": actual_coverage,
        "interval_hits": (
            sum(interval_hits)
            if intervals_available else None
        ),
        "interval_status": status,
        "interval_display_allowed": interval_display_allowed,
        "interval_details": interval_details,
        "interval_warning": (
            "Model-based intervals have not been "
            "statistically calibrated. Four holdout "
            "quarters are insufficient to establish "
            "reliable 95% coverage."
        ),
    }


def assess_all_holdouts(report, tickers, metrics):
    assessments = {}
    failures = []

    for ticker in tickers:
        assessments[ticker] = {}

        for metric in metrics:
            try:
                evaluation = report["results"][ticker][metric]

                if (
                    evaluation["ticker"] != ticker
                    or evaluation["metric"] != metric
                ):
                    raise ForecastQualityError(
                        "Company or metric mismatch."
                    )

                assessments[ticker][metric] = (
                    assess_holdout(evaluation)
                )

            except (
                KeyError, ValueError, TypeError,
                ForecastQualityError,
            ) as exc:
                failures.append({
                    "ticker": ticker,
                    "metric": metric,
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                })

    return assessments, failures
