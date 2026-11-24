"""Growth analysis: definitions, edge cases and the stored-data adapters."""

import math

import pytest

from research_v2_support import isolated_runtime  # noqa: F401  (fixture)

from app.services import growth_analysis as g


def q(end, value, fq=None):
    return {"period_end": end, "value": str(value), **({"fiscal_quarter": fq} if fq else {})}


QUARTERS = [  # calendar-year filer, two full years plus one quarter
    q("2024-03-31", 100, 1), q("2024-06-30", 110, 2), q("2024-09-30", 120, 3), q("2024-12-31", 130, 4),
    q("2025-03-31", 115, 1), q("2025-06-30", 121, 2), q("2025-09-30", 138, 3), q("2025-12-31", 156, 4),
    q("2026-03-31", 138, 1),
]


# ------------------------------------------------------------------ change

def test_normal_growth():
    out = g.change(120, 100)
    assert out == {"value_pct": 20.0, "status": "ok", "absolute_change": 20.0, "reason": None}


@pytest.mark.parametrize("current,base", [(50, 0), (0, 0)])
def test_zero_base_is_not_meaningful(current, base):
    out = g.change(current, base)
    assert out["value_pct"] is None and out["status"] == "not_meaningful" and "zero" in out["reason"]


@pytest.mark.parametrize("current,base", [(-681, 26) , (-752.6, -756.7), (26, -681)])
def test_negative_base_or_sign_change(current, base):
    out = g.change(current, base)
    if base < 0:
        # Growth from a loss (or loss to loss) is never a percentage.
        assert out["value_pct"] is None and out["status"] == "not_meaningful"
        assert "loss" in out["reason"] and out["absolute_change"] == pytest.approx(current - base)
    else:
        # A fall from a positive base into a loss is a real (large) decline.
        assert out["status"] == "ok" and out["value_pct"] < -100


def test_no_nan_or_infinity_anywhere():
    for current, base in [(1, 0), (0, 0), ("nan", 1), (1, "inf"), (None, 1), (1, None)]:
        out = g.change(current, base)
        assert out["value_pct"] is None or math.isfinite(out["value_pct"])


# ---------------------------------------------------- period matching / history

def test_yoy_uses_same_quarter_last_year_not_four_rows_back():
    # Remove 2025-06-30: "four rows back" from 2026-03-31 would now be 2024-12-31.
    gappy = [p for p in QUARTERS if p["period_end"] != "2025-06-30"]
    out = g.yoy_quarter("revenue_yoy", gappy)
    assert out["status"] == "ok"
    assert out["base"]["period_end"] == "2025-03-31"
    assert out["value_pct"] == pytest.approx(20.0)


def test_yoy_fiscal_quarter_must_match_when_known():
    points = [q("2024-06-30", 100, 1), q("2024-09-30", 1, 2), q("2024-12-31", 1, 3),
              q("2025-03-31", 1, 4), q("2025-06-29", 130, 2)]  # 52/53-week calendar, wrong quarter number
    out = g.yoy_quarter("revenue_yoy", points)
    assert out["status"] == "unavailable" and "matching quarter" in out["reason"]


def test_missing_matching_quarter_is_reported():
    points = [p for p in QUARTERS if p["period_end"] != "2025-03-31"]
    out = g.yoy_quarter("revenue_yoy", points)
    assert out["status"] == "unavailable" and out["value_pct"] is None


def test_qoq_requires_adjacent_quarter():
    assert g.qoq("revenue_qoq", QUARTERS)["value_pct"] == pytest.approx((138 - 156) / 156 * 100, abs=0.01)
    gap = [p for p in QUARTERS if p["period_end"] != "2025-12-31"]
    out = g.qoq("revenue_qoq", gap)
    assert out["status"] == "unavailable" and "preceding quarter" in out["reason"]


@pytest.mark.parametrize("count", [0, 1, 4])
def test_quarterly_yoy_needs_five_quarters(count):
    out = g.yoy_quarter("revenue_yoy", QUARTERS[:count])
    assert out["status"] == "insufficient_history"
    assert out["reason"] == f"Insufficient history (need 5 quarters, have {count})."


def test_annual_yoy_and_fiscal_year_gap():
    years = [q("2022-03-31", 100), q("2023-03-31", 110), q("2024-03-31", 121)]
    assert g.yoy_annual("revenue_annual_yoy", years)["value_pct"] == pytest.approx(10.0)
    gap = [q("2021-12-31", 100), q("2022-12-31", 110), q("2025-03-31", 150)]  # fiscal-year change + gap
    out = g.yoy_annual("revenue_annual_yoy", gap)
    assert out["status"] == "unavailable" and "gap" in out["reason"]


def test_cagr_exact_years_and_insufficient_history():
    years = [q(f"{y}-12-31", v) for y, v in [(2020, 100), (2021, 110), (2022, 121), (2023, 133.1)]]
    out = g.cagr("revenue_cagr_3y", years, 3)
    assert out["value_pct"] == pytest.approx(10.0, abs=0.01)
    assert out["base"]["period_end"] == "2020-12-31" and out["current"]["period_end"] == "2023-12-31"
    five = g.cagr("revenue_cagr_5y", years, 5)
    assert five["status"] == "insufficient_history"
    assert five["reason"] == "Insufficient history (need 6 fiscal years, have 4)."


def test_cagr_with_a_loss_at_either_end_is_not_meaningful():
    years = [q(f"{y}-12-31", v) for y, v in [(2020, -50), (2021, 10), (2022, 20), (2023, 40)]]
    out = g.cagr("net_income_cagr_3y", years, 3)
    assert out["status"] == "not_meaningful" and out["value_pct"] is None


def test_cagr_needs_a_year_exactly_n_years_back():
    years = [q("2019-12-31", 100), q("2021-12-31", 110), q("2022-12-31", 120), q("2023-12-31", 130)]
    # 3 years before 2023-12-31 is 2020-12-31, which is missing.
    out = g.cagr("revenue_cagr_3y", years, 3)
    assert out["status"] == "unavailable" and "exactly 3 years" in out["reason"]


# ----------------------------------------------------------------- trend

def _yoy(*values):
    return [{"period_end": f"2025-0{i + 1}-28", "value_pct": v, "status": "ok"} for i, v in enumerate(values)]


@pytest.mark.parametrize("values,label", [
    ((10.0, 12.0, 15.0), "accelerating"),    # 15 - 11 = +4
    ((15.0, 12.0, 10.0), "decelerating"),    # 10 - 13.5 = -3.5
    ((10.0, 12.0, 12.0), "stable"),          # 12 - 11 = +1
])
def test_trend_rule(values, label):
    out = g.trend(_yoy(*values), [])
    assert out["label"] == label and out["basis"] == "quarterly"
    assert "percentage points" in out["rule"]


def test_trend_falls_back_to_annual_then_insufficient():
    assert g.trend(_yoy(5.0), _yoy(1.0, 2.0, 9.0))["basis"] == "annual"
    out = g.trend(_yoy(5.0), _yoy(1.0))
    assert out["label"] == "insufficient_history" and "need 3" in out["reason"]


def test_trend_ignores_not_meaningful_points():
    series = _yoy(10.0, 12.0, 15.0) + [{"period_end": "2025-09-30", "value_pct": None, "status": "not_meaningful"}]
    assert g.trend(series, [])["label"] == "accelerating"


# ------------------------------------------------------------ full analysis

def test_analyze_short_provider_history_marks_what_cannot_be_computed():
    series = {
        "revenue": {"quarterly": QUARTERS[-4:], "annual": [q("2025-03-31", 400), q("2026-03-31", 450)]},
        "net_income": {"quarterly": [], "annual": [q("2025-03-31", -10), q("2026-03-31", 30)]},
    }
    out = g.analyze(series, as_of="2026-05-01", currency="INR")
    m = out["metrics"]
    assert m["revenue_yoy"]["reason"] == "Insufficient history (need 5 quarters, have 4)."
    assert m["revenue_qoq"]["status"] == "ok"
    assert m["revenue_annual_yoy"]["value_pct"] == pytest.approx(12.5)
    assert m["net_income_annual_yoy"]["status"] == "not_meaningful"
    assert m["revenue_cagr_3y"]["status"] == "insufficient_history"
    assert m["eps_yoy"]["status"] == "unavailable" and "not stored" in m["eps_yoy"]["reason"]
    assert out["trend"]["label"] == "insufficient_history"
    assert out["currency"] == "INR" and "12.5%" in out["readout"]


def test_staleness_is_flagged():
    out = g.analyze({"revenue": {"quarterly": QUARTERS}}, as_of="2027-06-30")
    assert out["staleness"]["stale_quarterly"] is True
    assert "2026-03-31" in out["staleness"]["notes"][0]
    assert "old" in out["readout"]


def test_eps_fourth_quarter_is_never_derived(isolated_runtime):
    # Only reported 80-100 day EPS periods are used, so no Q4 point is created
    # from annual minus nine months (share counts differ between periods).
    from app.services import financial_normalization as fin

    out = fin.sec_eps_series("MSFT")
    for point in out["quarterly"]:
        start, end = g._date(point["period_start"]), g._date(point["period_end"])
        assert 80 <= (end - start).days + 1 <= 100


# ---------------------------------------------------- stored-data adapters

def test_sec_adapter_real_company(isolated_runtime):
    result, series = g.company_growth("MSFT")
    assert result["source"].startswith("SEC") and result["currency"] == "USD"
    assert result["history"]["revenue"]["quarters"] >= 20
    assert result["metrics"]["revenue_yoy"]["status"] == "ok"
    assert result["metrics"]["net_income_yoy"]["status"] == "ok"
    # Annual values are sums of four reconciled quarters.
    assert series["revenue"]["annual"]


def _seed_provider(symbol, quarterly, annual, currency="INR"):
    """Test-only provider statement rows in the isolated database."""
    from app.services import market_storage

    market_storage.register_company(symbol=symbol, market="IN", name=symbol, currency=currency)
    rows = []
    for period_type, points in (("quarterly", quarterly), ("annual", annual)):
        for end, values in points:
            for metric, value in values.items():
                rows.append({"symbol": symbol, "provider": "YFINANCE", "statement": "income",
                             "period_type": period_type, "period_end": end, "fiscal_year": end[:4],
                             "metric": metric, "value_text": str(value), "currency": currency,
                             "source_field": f"test['{metric}']"})
    market_storage.insert_statement_rows(rows)


def test_provider_adapter_short_history_with_gap_and_loss(isolated_runtime):
    # Shaped like stored yfinance data: five quarters with 2025-09 missing, four
    # fiscal years, and a loss in the base year for net income.
    _seed_provider(
        "ITC.NS",
        quarterly=[("2025-06-30", {"revenue": 100, "net_income": 20}), ("2025-12-31", {"revenue": 110, "net_income": 22}),
                   ("2026-03-31", {"revenue": 112, "net_income": 23}), ("2026-06-30", {"revenue": 118, "net_income": 24}),
                   ("2026-09-30", {"revenue": 125, "net_income": 25})],
        annual=[("2023-03-31", {"revenue": 400, "net_income": -5}), ("2024-03-31", {"revenue": 420, "net_income": 30}),
                ("2025-03-31", {"revenue": 430, "net_income": 35}), ("2026-03-31", {"revenue": 450, "net_income": 40})],
    )
    result, _ = g.company_growth("ITC.NS")
    m = result["metrics"]
    assert result["currency"] == "INR" and result["source"].startswith("Provider")
    assert m["revenue_qoq"]["value_pct"] == pytest.approx((125 - 118) / 118 * 100, abs=0.01)
    # 2026-09-30 has no matching 2025-09-30 quarter: reported, not guessed.
    assert m["revenue_yoy"]["status"] == "unavailable" and "matching quarter" in m["revenue_yoy"]["reason"]
    assert m["revenue_annual_yoy"]["value_pct"] == pytest.approx(4.65, abs=0.01)
    assert m["revenue_cagr_3y"]["value_pct"] == pytest.approx(((450 / 400) ** (1 / 3) - 1) * 100, abs=0.01)
    assert m["revenue_cagr_5y"]["reason"] == "Insufficient history (need 6 fiscal years, have 4)."
    assert m["net_income_cagr_3y"]["status"] == "not_meaningful"  # loss at the start


def test_provider_point_in_time_excludes_rows_stored_later(isolated_runtime):
    _seed_provider("TCS.NS", quarterly=[], annual=[("2025-03-31", {"revenue": 100}), ("2026-03-31", {"revenue": 120})])
    now, _ = g.company_growth("TCS.NS")
    before, _ = g.company_growth("TCS.NS", as_of="2020-01-01T00:00:00+00:00")
    assert now["metrics"]["revenue_annual_yoy"]["value_pct"] == pytest.approx(20.0)
    assert before["history"]["revenue"]["fiscal_years"] == 0


def test_point_in_time_excludes_later_filings(isolated_runtime):
    later, _ = g.company_growth("MSFT")
    earlier, _ = g.company_growth("MSFT", as_of="2024-01-31")
    assert earlier["metrics"]["revenue_yoy"]["current"]["period_end"] < "2024-01-31"
    assert earlier["metrics"]["revenue_yoy"]["current"]["period_end"] < later["metrics"]["revenue_yoy"]["current"]["period_end"]
