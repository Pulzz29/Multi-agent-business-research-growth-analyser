"""
Forecasting and backtesting integrity on the isolated runtime (real stored SEC
facts from the snapshot; no network): point-in-time training data, separated
model selection and held-out evaluation, empirical interval gate, minimum
history, and honest unavailability. Never writes to the real project.
"""
import json
import warnings

import numpy as np
import pytest

from research_v2_support import isolated_runtime  # noqa: F401  (fixture import)

warnings.filterwarnings("ignore")


def test_point_in_time_training_never_uses_later_filings(isolated_runtime):
    from app.services import forecasting as f
    from app.services.financial_normalization import normalized_quarterly_financials

    history = f.contiguous_recent_history(normalized_quarterly_financials("MSFT")["revenue"])
    origin = len(history) - 8
    cutoff = history[origin - 1]["period_end"]
    as_of = f.first_reported_date("MSFT", "revenue", cutoff)
    series = normalized_quarterly_financials("MSFT", as_of=as_of)["revenue"]
    assert series, "point-in-time series is empty"
    for row in series:
        for evidence in row["evidence"]:
            assert evidence["filed_at"] <= as_of, (row["period_end"], evidence["filed_at"], as_of)
    values, used_as_of = f.point_in_time_training("MSFT", "revenue", history, origin, {})
    assert used_as_of == as_of and len(values) >= f.MIN_TRAIN


def test_selection_and_evaluation_are_separate_and_ordered(isolated_runtime):
    from app.services import forecasting as f
    from app.services.financial_normalization import normalized_quarterly_financials

    history = f.contiguous_recent_history(normalized_quarterly_financials("AMZN")["revenue"])
    values = f.validate_history(history)
    backtesting, folds = f.walk_forward("AMZN", "revenue", history, values)
    selection_ends = [fold["seasonal_naive"]["origin_period_end"] for fold in folds["selection"]]
    evaluation_end = folds["evaluation"][0]["seasonal_naive"]["origin_period_end"]
    assert selection_ends == sorted(selection_ends) and max(selection_ends) < evaluation_end
    # The selection summary is built only from selection folds.
    for model, summary in backtesting.items():
        assert all(fold["origin_period_end"] in selection_ends for fold in summary["folds"])
    # Each fold forecasts quarters strictly after its training data.
    n = len(values)
    for offset, fold in zip(f.SELECTION_OFFSETS + (f.EVALUATION_OFFSET,), folds["selection"] + folds["evaluation"]):
        assert fold["seasonal_naive"]["origin_period_end"] == history[n - offset - 1]["period_end"]


def test_forecast_reports_held_out_validation(isolated_runtime):
    from app.services import forecasting as f

    result = f.forecast_financial_metric("META", "revenue")
    v = result["validation"]
    assert v["method"] == "point_in_time_walk_forward"
    assert v["evaluation_available"] is True and v["holdout_quarters"] == 4
    assert v["baseline_mae_billion_usd"] is not None and v["selected_mae_billion_usd"] is not None
    if result["selected_model"] != "seasonal_naive":
        assert v["beats_baseline"] in (True, False)
    for row in result["forecast"]:
        if v["interval_display_allowed"]:
            assert row["lower_95_billion_usd"] <= row["predicted_billion_usd"] <= row["upper_95_billion_usd"]
        else:
            assert row["lower_95_billion_usd"] is None and row["upper_95_billion_usd"] is None


def _fold(hits, points=4):
    return {"seasonal_naive": {"mae_billion_usd": 2.0, "origin_period_end": "2025-03-31"},
            "sarimax_1": {"mae_billion_usd": 1.0, "interval_hits": hits, "interval_points": points}}


@pytest.mark.parametrize("hits,allowed,status", [
    ((4, 4, 4), True, "validated_limited_sample"),
    ((3, 3, 3), True, "validated_limited_sample"),   # 9/12 = 0.75
    ((3, 2, 3), False, "not_validated"),              # 8/12
    ((1, 0, 2), False, "not_validated"),
])
def test_interval_gate_requires_empirical_coverage(hits, allowed, status):
    from app.services.forecasting import validation_summary

    folds = {"selection": [_fold(hits[0]), _fold(hits[1])], "evaluation": [_fold(hits[2])]}
    v = validation_summary("sarimax_1", folds)
    assert v["interval_display_allowed"] is allowed and v["interval_status"] == status
    assert v["interval_points"] == 12 and v["interval_hits"] == sum(hits)


def test_interval_gate_needs_enough_points():
    from app.services.forecasting import validation_summary

    folds = {"selection": [_fold(4)], "evaluation": [{"origin_period_end": "x", "unavailable": "no filing"}]}
    v = validation_summary("sarimax_1", folds)
    assert v["interval_points"] == 4 and v["interval_display_allowed"] is False
    assert v["evaluation_available"] is False and v["evaluation_unavailable_reason"] == "no filing"


def test_seasonal_naive_never_has_intervals():
    from app.services.forecasting import validation_summary

    v = validation_summary("seasonal_naive", {"selection": [_fold(4), _fold(4)], "evaluation": [_fold(4)]})
    assert v["interval_display_allowed"] is False and v["interval_status"] == "unavailable"


def test_short_history_is_unavailable_not_padded(isolated_runtime, monkeypatch):
    from app.services import forecasting as f

    real = f.normalized_quarterly_financials

    def short(ticker, as_of=None):
        data = real(ticker, as_of=as_of)
        return {**data, "revenue": data["revenue"][-20:]}

    monkeypatch.setattr(f, "normalized_quarterly_financials", short)
    with pytest.raises(f.ForecastingError, match="At least 28"):
        f.forecast_financial_metric("MSFT", "revenue")


def test_provider_forecasts_are_honestly_unavailable(isolated_runtime):
    from app.agents.provider_financial import provider_forecast

    forecast, reason = provider_forecast("TCS.NS", "revenue")
    assert forecast is None and ("filing dates" in reason or "quarters" in reason)


def test_old_cache_versions_are_never_shown_as_current(isolated_runtime):
    from app.services import company_profile, forecast_cache
    from research_v2_support import make_cache_stale

    # An entry written by an earlier method version, wherever it is found,
    # is never presented as a current forecast.
    if forecast_cache.read_envelope("MSFT", "revenue") is None:
        forecast_cache.forecast_financial_metric_cached("MSFT", "revenue")
    make_cache_stale("MSFT", "revenue")
    shown = company_profile.read_forecast("MSFT", "revenue")
    assert shown["available"] is False and "earlier validation method" in shown["reason"]


def test_cached_forecast_is_recomputed_and_fingerprint_covers_all_facts(isolated_runtime):
    from app.services import forecast_cache

    from research_v2_support import make_cache_stale

    make_cache_stale("MSFT", "operating_cash_flow")
    first = forecast_cache.forecast_financial_metric_cached("MSFT", "operating_cash_flow")
    assert first["cache_status"] == "miss" and first["forecast"]["validation"]
    again = forecast_cache.forecast_financial_metric_cached("MSFT", "operating_cash_flow")
    assert again["cache_status"] == "hit"
    assert np.isfinite(first["forecast"]["forecast"][0]["predicted_billion_usd"])
