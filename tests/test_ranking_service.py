"""Unit tests for the deterministic sector ranking model (pure, offline)."""
import copy
import json

import pytest

from app.services import ranking_service as rs


def config(weights=None, min_coverage=0.6, overrides=None):
    base = rs.load_ranking_config()
    cfg = copy.deepcopy(base)
    if weights is not None:
        cfg["weights"]["default"] = {m: 0.0 for m in cfg["metrics"]}
        cfg["weights"]["default"].update(weights)
    if overrides is not None:
        cfg["weights"]["sector_overrides"] = overrides
    cfg["min_weight_coverage"] = min_coverage
    return cfg


def co(symbol, **metrics):
    return {"symbol": symbol, "name": f"Company {symbol}", "metrics": metrics}


def by_symbol(result):
    return {c["symbol"]: c for c in result["companies"]}


def test_shipped_config_is_valid():
    cfg = rs.load_ranking_config()
    assert rs.validate_config(cfg)
    assert sum(rs.weights_for(cfg, "technology").values()) > 0
    assert rs.weights_for(cfg, "banking")["debt_to_equity"] == 0.0


def test_min_max_normalisation_and_direction():
    cfg = config({"net_margin_pct": 1.0, "debt_to_equity": 1.0})
    result = rs.rank_companies([
        co("A", net_margin_pct=30, debt_to_equity=0.5),
        co("B", net_margin_pct=20, debt_to_equity=1.0),
        co("C", net_margin_pct=10, debt_to_equity=2.0),
    ], "technology", cfg)
    rows = by_symbol(result)
    margin = {s: next(b for b in rows[s]["breakdown"] if b["metric"] == "net_margin_pct") for s in "ABC"}
    debt = {s: next(b for b in rows[s]["breakdown"] if b["metric"] == "debt_to_equity") for s in "ABC"}
    assert [margin[s]["normalized_score"] for s in "ABC"] == [100.0, 50.0, 0.0]
    # Lower debt is better: A (lowest) scores 100, C (highest) 0.
    assert debt["A"]["normalized_score"] == 100.0 and debt["C"]["normalized_score"] == 0.0
    assert [c["symbol"] for c in result["companies"]] == ["A", "B", "C"]
    assert [c["rank"] for c in result["companies"]] == [1, 2, 3]


def test_weights_are_applied_and_breakdown_sums_to_score():
    cfg = config({"net_margin_pct": 3.0, "revenue_growth_pct": 1.0})
    result = rs.rank_companies([
        co("A", net_margin_pct=30, revenue_growth_pct=0),
        co("B", net_margin_pct=10, revenue_growth_pct=50),
    ], "technology", cfg)
    rows = by_symbol(result)
    assert result["method"]["weights_applied"] == {"revenue_growth_pct": 0.25, "net_margin_pct": 0.75}
    assert rows["A"]["score"] == 75.0 and rows["B"]["score"] == 25.0
    for row in rows.values():
        assert abs(sum(b["contribution"] for b in row["breakdown"]) - row["score"]) < 0.01


def test_equal_values_score_fifty():
    cfg = config({"net_margin_pct": 1.0, "roe_pct": 1.0})
    result = rs.rank_companies([
        co("A", net_margin_pct=10, roe_pct=5),
        co("B", net_margin_pct=10, roe_pct=15),
    ], "technology", cfg)
    rows = by_symbol(result)
    assert all(b["normalized_score"] == 50.0
               for r in rows.values() for b in r["breakdown"] if b["metric"] == "net_margin_pct")


def test_ties_share_rank_with_competition_ranking():
    cfg = config({"net_margin_pct": 1.0})
    result = rs.rank_companies([
        co("C", net_margin_pct=5),
        co("A", net_margin_pct=20),
        co("B", net_margin_pct=20),
    ], "technology", cfg)
    assert [(c["symbol"], c["rank"]) for c in result["companies"]] == [("A", 1), ("B", 1), ("C", 3)]
    assert result["ties"] == [["A", "B"]]


def test_missing_values_are_imputed_with_median_and_flagged():
    cfg = config({"net_margin_pct": 1.0, "roe_pct": 1.0}, min_coverage=0.4)
    result = rs.rank_companies([
        co("A", net_margin_pct=30, roe_pct=20),
        co("B", net_margin_pct=10, roe_pct=None),
        co("C", net_margin_pct=20, roe_pct=10),
    ], "technology", cfg)
    b = by_symbol(result)["B"]
    roe = next(x for x in b["breakdown"] if x["metric"] == "roe_pct")
    assert roe["imputed"] is True and roe["raw_value"] is None and roe["value_used"] == 15
    assert b["imputed_metrics"] == ["roe_pct"]
    assert b["coverage"] == 0.5
    assert "sector median" in b["explanation"]


def test_coverage_threshold_excludes_company_from_ranking():
    cfg = config({"net_margin_pct": 1.0, "roe_pct": 1.0, "roa_pct": 1.0}, min_coverage=0.6)
    result = rs.rank_companies([
        co("A", net_margin_pct=30, roe_pct=20, roa_pct=5),
        co("B", net_margin_pct=10),
        co("C", net_margin_pct=20, roe_pct=10, roa_pct=3),
    ], "technology", cfg)
    rows = by_symbol(result)
    assert rows["B"]["ranked"] is False and rows["B"]["rank"] is None
    assert "covers 33%" in rows["B"]["not_ranked_reason"]
    assert [c["symbol"] for c in result["companies"] if c["ranked"]] == ["A", "C"]
    assert result["companies"][-1]["symbol"] == "B"  # unranked listed last


def test_metric_without_enough_data_is_dropped_and_weights_renormalised():
    cfg = config({"net_margin_pct": 1.0, "dividend_yield_pct": 1.0, "beta": 1.0})
    result = rs.rank_companies([
        co("A", net_margin_pct=30, dividend_yield_pct=2.0),
        co("B", net_margin_pct=10),
    ], "technology", cfg)
    dropped = {d["metric"]: d["reason"] for d in result["method"]["metrics_dropped"]}
    assert "beta" in dropped and "no data" in dropped["beta"]
    assert "fewer than two" in dropped["dividend_yield_pct"]
    assert result["method"]["weights_applied"] == {"net_margin_pct": 1.0}


def test_non_meaningful_values_are_missing():
    cfg = config({"pe_ratio": 1.0, "net_margin_pct": 1.0}, min_coverage=0.4)
    result = rs.rank_companies([
        co("A", pe_ratio=-12.0, net_margin_pct=-5),
        co("B", pe_ratio=20.0, net_margin_pct=10),
        co("C", pe_ratio=30.0, net_margin_pct=12),
    ], "technology", cfg)
    pe = next(b for b in by_symbol(result)["A"]["breakdown"] if b["metric"] == "pe_ratio")
    assert pe["imputed"] is True


@pytest.mark.parametrize("companies", [[], [co("A", net_margin_pct=10)]])
def test_fewer_than_two_companies(companies):
    result = rs.rank_companies(companies, "technology", config({"net_margin_pct": 1.0}))
    assert result["status"] == "insufficient_companies"
    assert all(c["rank"] is None for c in result["companies"])


def test_no_comparable_metrics():
    result = rs.rank_companies(
        [co("A"), co("B")], "technology", config({"net_margin_pct": 1.0}))
    assert result["status"] == "no_comparable_metrics"


def test_banking_override_removes_leverage_metrics():
    cfg = rs.load_ranking_config()
    result = rs.rank_companies([
        co("A", roe_pct=15, debt_to_equity=9.0, pb_ratio=1.2, net_margin_pct=25),
        co("B", roe_pct=10, debt_to_equity=1.0, pb_ratio=2.0, net_margin_pct=20),
    ], "banking", cfg)
    used = result["method"]["metrics_used"]
    assert "debt_to_equity" not in used and "pb_ratio" in used


def test_deterministic_and_json_serialisable():
    cfg = rs.load_ranking_config()
    data = [co("A", net_margin_pct=12, roe_pct=18, market_cap=3e11),
            co("B", net_margin_pct=20, roe_pct=11, market_cap=9e11),
            co("C", net_margin_pct=8, roe_pct=25, market_cap=1e11)]
    first = rs.rank_companies(data, "technology", cfg)
    second = rs.rank_companies(list(reversed(data)), "technology", cfg)
    assert first == second
    json.dumps(first, allow_nan=False)


def test_duplicate_symbols_rejected():
    with pytest.raises(ValueError):
        rs.rank_companies([co("A"), co("A")], "technology", config({"net_margin_pct": 1.0}))


def test_invalid_config_rejected():
    cfg = rs.load_ranking_config()
    bad = copy.deepcopy(cfg)
    bad["weights"]["default"]["net_margin_pct"] = -1
    with pytest.raises(rs.RankingConfigError):
        rs.validate_config(bad)
    bad = copy.deepcopy(cfg)
    bad["weights"]["default"]["unknown_metric"] = 0.5
    with pytest.raises(rs.RankingConfigError):
        rs.validate_config(bad)


# --- Custom comparison: ranking over a selected subset (universe="selected") ---

def _sector():
    return [
        co("A", net_margin_pct=40, revenue_growth_pct=5),
        co("B", net_margin_pct=30, revenue_growth_pct=10),
        co("C", net_margin_pct=20, revenue_growth_pct=30),
        co("D", net_margin_pct=10, revenue_growth_pct=20),
    ]


def test_selected_universe_normalises_over_the_subset_only():
    cfg = config({"net_margin_pct": 1.0, "revenue_growth_pct": 1.0})
    full = by_symbol(rs.rank_companies(_sector(), "technology", cfg))
    subset = [c for c in _sector() if c["symbol"] in ("B", "D")]
    result = rs.rank_companies(subset, "technology", cfg, universe="selected")
    rows = by_symbol(result)
    assert set(rows) == {"B", "D"}
    margin = {s: next(b for b in rows[s]["breakdown"] if b["metric"] == "net_margin_pct") for s in rows}
    # Within {B, D} B has the highest margin (100) and D the lowest (0);
    # within the full sector B scores 66.67 on margin.
    assert margin["B"]["normalized_score"] == 100.0 and margin["D"]["normalized_score"] == 0.0
    full_margin = next(b for b in full["B"]["breakdown"] if b["metric"] == "net_margin_pct")
    assert full_margin["normalized_score"] == pytest.approx(66.67)
    assert rows["B"]["score"] != full["B"]["score"]
    # Same scoring function: subset results equal a sector-universe run on the same input.
    same = rs.rank_companies(subset, "technology", cfg)
    assert [(c["symbol"], c["score"], c["rank"]) for c in same["companies"]] == [
        (c["symbol"], c["score"], c["rank"]) for c in result["companies"]
    ]


def test_selected_universe_order_and_method_note():
    cfg = config({"net_margin_pct": 1.0})
    subset = [co("X", net_margin_pct=5), co("Y", net_margin_pct=25), co("Z", net_margin_pct=15)]
    result = rs.rank_companies(subset, "technology", cfg, universe="selected")
    assert [c["symbol"] for c in result["companies"]] == ["Y", "Z", "X"]
    assert [c["rank"] for c in result["companies"]] == [1, 2, 3]
    assert result["method"]["universe"] == "selected"
    assert result["method"]["relative_note"] == rs.RELATIVE_NOTE
    assert "selected companies" in result["method"]["normalization"]
    assert "within the selected companies" in result["companies"][0]["explanation"]
    sector = rs.rank_companies(subset, "technology", cfg)
    assert sector["method"]["universe"] == "sector"
    assert "relative_note" not in sector["method"]


def test_selected_universe_zero_variance_does_not_crash():
    cfg = config({"net_margin_pct": 1.0, "revenue_growth_pct": 1.0})
    subset = [co("A", net_margin_pct=20, revenue_growth_pct=10), co("B", net_margin_pct=20, revenue_growth_pct=10)]
    result = rs.rank_companies(subset, "technology", cfg, universe="selected")
    assert result["status"] == "ranked"
    assert all(c["score"] == 50.0 and c["rank"] == 1 for c in result["companies"])
    assert result["ties"] == [["A", "B"]]


def test_selected_universe_missing_metric_is_flagged():
    cfg = config({"net_margin_pct": 1.0, "revenue_growth_pct": 1.0}, min_coverage=0.4)
    subset = [
        co("A", net_margin_pct=30, revenue_growth_pct=10),
        co("B", net_margin_pct=10, revenue_growth_pct=None),
        co("C", net_margin_pct=20, revenue_growth_pct=30),
    ]
    rows = by_symbol(rs.rank_companies(subset, "technology", cfg, universe="selected"))
    assert rows["B"]["imputed_metrics"] == ["revenue_growth_pct"]
    growth = next(b for b in rows["B"]["breakdown"] if b["metric"] == "revenue_growth_pct")
    assert growth["imputed"] is True and growth["value_used"] == 20
    assert "median of the selected companies" in rows["B"]["explanation"]


def test_selected_universe_fewer_than_two_valid_companies():
    cfg = config({"net_margin_pct": 1.0, "revenue_growth_pct": 1.0})
    one = rs.rank_companies([co("A", net_margin_pct=10)], "technology", cfg, universe="selected")
    assert one["status"] == "insufficient_companies"
    assert one["companies"][0]["not_ranked_reason"] == "fewer than two companies among the selected companies"
    # Two selected, but only one has enough data to be ranked.
    sparse = rs.rank_companies(
        [co("A", net_margin_pct=10, revenue_growth_pct=5), co("B", net_margin_pct=20, revenue_growth_pct=None),
         co("C", net_margin_pct=None, revenue_growth_pct=9)],
        "technology", config({"net_margin_pct": 1.0, "revenue_growth_pct": 1.0}, min_coverage=0.9),
        universe="selected",
    )
    assert sparse["status"] == "insufficient_companies"
    assert not any(c["ranked"] for c in sparse["companies"])


def test_invalid_universe_rejected():
    with pytest.raises(ValueError):
        rs.rank_companies([co("A", net_margin_pct=1), co("B", net_margin_pct=2)], "technology", universe="custom")
