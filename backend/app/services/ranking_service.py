"""
Transparent, deterministic multi-criteria ranking for sector comparison.

Method (all parameters live in core/ranking_weights.json):

1. Clean: non-finite values are missing; a P/E <= 0 (loss-making) and a
   negative debt-to-equity are "not meaningful" and treated as missing.
2. Select metrics: metrics with weight > 0 that have real values for at
   least two companies. Others are dropped (with the reason recorded) and
   the remaining weights are re-normalised to sum to 1.
3. Impute: a company missing a used metric receives the sector median for
   that metric, flagged ``imputed`` in its breakdown.
4. Normalise: min-max within the sector to 0..100; inverted for
   lower-is-better metrics; if all values are equal every company gets 50.
5. Score: composite = sum(weight x normalised score). A company whose real
   data covers less than ``min_weight_coverage`` of the applied weight is
   listed but not ranked.
6. Rank: standard competition ranking on the score rounded to
   ``tie_decimals`` (1, 1, 3); ties are reported explicitly.

Universe: normalisation, medians and ranks are always computed over the
companies passed in. ``universe="sector"`` (default) means they are the whole
sector; ``universe="selected"`` means a user-chosen subset, so scores are
relative to that subset and the output carries ``RELATIVE_NOTE``.

No randomness, no I/O, no LLM. The same inputs always give the same output.
"""

import json
import math
from pathlib import Path
from statistics import median


CONFIG_PATH = Path(__file__).resolve().parents[1] / "core" / "ranking_weights.json"

UNIVERSES = ("sector", "selected")
RELATIVE_NOTE = (
    "Scores are relative to the selected companies and may differ from the full sector ranking."
)
# Wording that names the comparison set, per universe.
_SCOPE = {
    "sector": {"within": "within sector", "median": "sector median", "group": "in this sector",
               "middle": "the sector middle", "strengths": "within the sector"},
    "selected": {"within": "within the selected companies", "median": "median of the selected companies",
                 "group": "among the selected companies", "middle": "the middle of the selected companies",
                 "strengths": "within the selected companies"},
}


class RankingConfigError(ValueError):
    pass


def load_ranking_config(path=None):
    path = Path(path or CONFIG_PATH)
    config = json.loads(path.read_text(encoding="utf-8"))
    validate_config(config)
    return config


def validate_config(config):
    metrics = config.get("metrics") or {}
    if not metrics:
        raise RankingConfigError("No metrics are configured.")
    for key, spec in metrics.items():
        if spec.get("direction") not in ("higher_is_better", "lower_is_better"):
            raise RankingConfigError(f"Metric {key} has an invalid direction.")
    default = (config.get("weights") or {}).get("default") or {}
    for group in [default, *((config.get("weights") or {}).get("sector_overrides") or {}).values()]:
        for metric, weight in group.items():
            if metric not in metrics:
                raise RankingConfigError(f"Weight given for unknown metric {metric}.")
            if not isinstance(weight, (int, float)) or weight < 0 or not math.isfinite(weight):
                raise RankingConfigError(f"Weight for {metric} must be a non-negative number.")
    coverage = config.get("min_weight_coverage", 0.6)
    if not 0 < coverage <= 1:
        raise RankingConfigError("min_weight_coverage must be in (0, 1].")
    return True


def weights_for(config, sector_key):
    weights = dict((config.get("weights") or {}).get("default") or {})
    override = ((config.get("weights") or {}).get("sector_overrides") or {}).get(sector_key)
    if override:
        weights.update(override)
    return {m: float(w) for m, w in weights.items() if m in config["metrics"]}


def _clean(metric, value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if metric == "pe_ratio" and number <= 0:
        return None
    if metric == "debt_to_equity" and number < 0:
        return None
    return number


def _normalise(value, low, high, direction):
    if high == low:
        return 50.0
    score = (value - low) / (high - low) * 100.0
    return 100.0 - score if direction == "lower_is_better" else score


def competition_ranks(scores):
    """{key: score} -> {key: rank} with ties sharing the best rank (1,1,3)."""
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    ranks, previous, previous_rank = {}, None, 0
    for position, (key, score) in enumerate(ordered, start=1):
        if previous is not None and score == previous:
            ranks[key] = previous_rank
        else:
            ranks[key] = position
            previous, previous_rank = score, position
    return ranks


def rank_companies(companies, sector_key, config=None, universe="sector"):
    """
    companies: [{"symbol", "name", "metrics": {metric: value or None}}]
    universe: "sector" (the companies are the whole sector) or "selected"
    (a user-chosen subset). The arithmetic is identical; normalisation is
    always over ``companies``. The universe is recorded in ``method``.
    Returns a JSON-serialisable result with per-metric breakdowns.
    """
    if universe not in UNIVERSES:
        raise ValueError(f"universe must be one of {UNIVERSES}.")
    scope = _SCOPE[universe]
    config = config or load_ranking_config()
    specs = config["metrics"]
    decimals = int(config.get("tie_decimals", 2))
    min_coverage = float(config.get("min_weight_coverage", 0.6))
    requested = weights_for(config, sector_key)

    symbols = [c["symbol"] for c in companies]
    if len(set(symbols)) != len(symbols):
        raise ValueError("Duplicate company symbols in ranking input.")

    values = {
        c["symbol"]: {m: _clean(m, (c.get("metrics") or {}).get(m)) for m in specs}
        for c in companies
    }

    used, dropped = [], []
    for metric, weight in requested.items():
        if weight <= 0:
            continue
        available = [values[s][metric] for s in symbols if values[s][metric] is not None]
        if not available:
            dropped.append({"metric": metric, "reason": f"no data for any company {scope['group']}"})
        elif len(available) < 2:
            dropped.append({"metric": metric, "reason": "data for fewer than two companies"})
        else:
            used.append(metric)

    total = sum(requested[m] for m in used)
    applied = {m: requested[m] / total for m in used} if total > 0 else {}

    method = {
        "normalization": f"min-max {scope['within']}, 0-100; inverted for lower-is-better metrics; 50 when all values are equal",
        "missing_values": f"{scope['median']} imputation, flagged as imputed",
        "min_weight_coverage": min_coverage,
        "tie_rule": f"scores rounded to {decimals} decimals; equal scores share a rank (1, 1, 3)",
        "weights_requested": requested,
        "weights_applied": {m: round(w, 6) for m, w in applied.items()},
        "metrics_used": used,
        "metrics_dropped": dropped,
        "sector_key": sector_key,
        "universe": universe,
    }
    if universe == "selected":
        method["relative_note"] = RELATIVE_NOTE

    if len(companies) < 2 or not used:
        status = "insufficient_companies" if len(companies) < 2 else "no_comparable_metrics"
        return {
            "status": status,
            "method": method,
            "companies": [
                {
                    "symbol": c["symbol"],
                    "name": c.get("name"),
                    "rank": None,
                    "score": None,
                    "ranked": False,
                    "not_ranked_reason": (
                        f"fewer than two companies {scope['group']}"
                        if status == "insufficient_companies"
                        else "no metric has data for at least two companies"
                    ),
                    "coverage": None,
                    "breakdown": [],
                    "strengths": [],
                    "weaknesses": [],
                    "explanation": None,
                }
                for c in companies
            ],
            "ties": [],
        }

    stats = {}
    for metric in used:
        real = [values[s][metric] for s in symbols if values[s][metric] is not None]
        stats[metric] = {"low": min(real), "high": max(real), "median": median(real)}

    rows = {}
    for company in companies:
        symbol = company["symbol"]
        breakdown, composite, coverage = [], 0.0, 0.0
        for metric in used:
            raw = values[symbol][metric]
            imputed = raw is None
            basis = stats[metric]["median"] if imputed else raw
            score = _normalise(basis, stats[metric]["low"], stats[metric]["high"], specs[metric]["direction"])
            contribution = applied[metric] * score
            composite += contribution
            if not imputed:
                coverage += applied[metric]
            breakdown.append({
                "metric": metric,
                "label": specs[metric]["label"],
                "unit": specs[metric].get("unit"),
                "direction": specs[metric]["direction"],
                "raw_value": raw,
                "value_used": basis,
                "imputed": imputed,
                "normalized_score": round(score, 2),
                "weight": round(applied[metric], 6),
                "contribution": round(contribution, 4),
            })
        rows[symbol] = {
            "symbol": symbol,
            "name": company.get("name"),
            "score_exact": composite,
            "score": round(composite, decimals),
            "coverage": round(coverage, 4),
            "breakdown": breakdown,
        }

    rankable = {s: r["score"] for s, r in rows.items() if r["coverage"] + 1e-9 >= min_coverage}
    status = "ranked" if len(rankable) >= 2 else "insufficient_companies"
    ranks = competition_ranks(rankable) if status == "ranked" else {}

    out = []
    for symbol, row in rows.items():
        real_items = [b for b in row["breakdown"] if not b["imputed"]]
        strengths = sorted(
            (b for b in real_items if b["normalized_score"] >= 60),
            key=lambda b: (-b["contribution"], b["metric"]),
        )[:3]
        weaknesses = sorted(
            (b for b in real_items if b["normalized_score"] <= 40),
            key=lambda b: (b["normalized_score"], b["metric"]),
        )[:3]
        ranked = symbol in ranks
        reason = None
        if not ranked:
            reason = (
                f"real data covers {row['coverage'] * 100:.0f}% of the applied weight "
                f"(minimum {min_coverage * 100:.0f}%)"
                if row["coverage"] + 1e-9 < min_coverage
                else "fewer than two companies have enough data to rank"
            )
        out.append({
            "symbol": symbol,
            "name": row["name"],
            "rank": ranks.get(symbol),
            "score": row["score"] if ranked else None,
            "score_unranked": None if ranked else row["score"],
            "ranked": ranked,
            "not_ranked_reason": reason,
            "coverage": row["coverage"],
            "breakdown": row["breakdown"],
            "strengths": [b["metric"] for b in strengths],
            "weaknesses": [b["metric"] for b in weaknesses],
            "imputed_metrics": [b["metric"] for b in row["breakdown"] if b["imputed"]],
        })

    total_ranked = len(ranks)
    for item in out:
        item["explanation"] = deterministic_explanation(item, total_ranked, specs, universe)

    out.sort(key=lambda c: (c["rank"] is None, c["rank"] or 0, c["symbol"]))

    groups = {}
    for symbol, rank in ranks.items():
        groups.setdefault(rank, []).append(symbol)
    ties = [sorted(g) for _, g in sorted(groups.items()) if len(g) > 1]

    return {"status": status, "method": method, "companies": out, "ties": ties}


def _format_value(item):
    value = item["raw_value"]
    if value is None:
        return "n/a"
    unit = item.get("unit")
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "x":
        return f"{value:.2f}x"
    if unit == "currency":
        return f"{value / 1e9:,.1f} bn"
    return f"{value:g}"


def deterministic_explanation(item, total_ranked, specs, universe="sector"):
    """Template explanation built only from the score breakdown."""
    scope = _SCOPE[universe]
    by_metric = {b["metric"]: b for b in item["breakdown"]}

    def describe(metrics):
        return ", ".join(
            f"{specs[m]['label']} ({_format_value(by_metric[m])}, score {by_metric[m]['normalized_score']:.0f})"
            for m in metrics
        )

    if not item["ranked"]:
        lead = f"Not ranked: {item['not_ranked_reason']}."
    else:
        lead = (
            f"Ranked {item['rank']} of {total_ranked} with a composite score of "
            f"{item['score']:.2f} out of 100."
        )
    parts = [lead]
    if item["strengths"]:
        parts.append(f"Relative strengths {scope['strengths']}: {describe(item['strengths'])}.")
    if item["weaknesses"]:
        parts.append(f"Relative weaknesses: {describe(item['weaknesses'])}.")
    if not item["strengths"] and not item["weaknesses"] and item["breakdown"]:
        parts.append(f"Its metrics sit close to {scope['middle']} on every measure.")
    if item["imputed_metrics"]:
        labels = ", ".join(specs[m]["label"] for m in item["imputed_metrics"])
        parts.append(f"Missing data replaced by the {scope['median']} for: {labels}.")
    return " ".join(parts)
