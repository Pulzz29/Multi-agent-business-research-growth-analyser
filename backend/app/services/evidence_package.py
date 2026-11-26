"""
Bounded, validated evidence packages for the research agents.

A package contains only inputs that were already validated by InsightIQ:
SEC facts re-checked against the sec_facts table, comparisons computed here
by Python with Decimal arithmetic, forecast outputs from the fingerprinted
forecast cache, and news records loaded from SQLite. Saved research reports
are never used as evidence.

Every package item carries an internal ``_allow`` block (numbers, dates,
periods) used by narrative_validation; it is stripped before the package
is shown to the model or written to a report.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.services.narrative_validation import (
    build_item_allowance,
    text_number_allowance,
)


PACKAGE_VERSION = 1
METRIC_LABELS = {
    "revenue": "Revenue",
    "operating_cash_flow": "Operating cash flow",
}
EXCERPT_FULL_CHARS = 700
EXCERPT_REDUCED_CHARS = 300
HEADLINE_ONLY_MIN_CHARS = 80


# --------------------------------------------------------------- helpers

def public(item):
    """Copy of an item without internal validation fields."""
    return {k: v for k, v in item.items() if not k.startswith("_")}


def billions(value_usd):
    return (Decimal(str(value_usd)) / Decimal(1_000_000_000)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def percent_change(current, previous):
    # Same rule as growth_analysis.change: a zero or negative base (a loss or
    # negative cash flow) gives no percentage, so none is offered as evidence.
    current, previous = Decimal(str(current)), Decimal(str(previous))
    if previous <= 0:
        return None
    return ((current - previous) / previous * 100).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def contiguous_run_length(records):
    """Longest run of consecutive standalone quarters (per metric)."""
    best = {}
    by_metric = {}
    for record in records:
        by_metric.setdefault(record["metric"], []).append(record)
    for metric, rows in by_metric.items():
        rows = sorted(
            {r["period_end"]: r for r in rows}.values(),
            key=lambda r: r["period_end"],
        )
        run = longest = 1 if rows else 0
        for prev, cur in zip(rows, rows[1:]):
            prev_end, cur_start = _date(prev["period_end"]), _date(cur["period_start"])
            if prev_end and cur_start and prev_end + timedelta(days=1) == cur_start:
                run += 1
            else:
                run = 1
            longest = max(longest, run)
        best[metric] = longest
    return best


# ------------------------------------------------------------- financial

def build_financial_package(
    ticker, evidence_records, financials, forecasts, quality, provenance,
):
    """
    evidence_records: output of build_verified_financial_evidence (already
    re-validated against sec_facts). financials: normalized quarterly series.
    forecasts: financial_agent forecast dict. quality: forecast quality
    assessments loaded from the stored review file.
    """
    from app.services.company_profile import build_series

    labels = {}
    for metric, rows in ((m, financials[m]) for m in METRIC_LABELS):
        for row in build_series(rows):
            labels[(metric, row["period_end"])] = (
                row["fiscal_label"], row["calendar_label"], row
            )

    records = []
    by_metric_end = {}
    for record in evidence_records:
        fiscal_label, calendar_label, _ = labels.get(
            (record["metric"], record["period_end"]), (None, None, None)
        )
        item = {
            "evidence_id": record["evidence_id"],
            "kind": "sec_quarter",
            "metric": record["metric"],
            "period_start": record["period_start"],
            "period_end": record["period_end"],
            "fiscal_label": fiscal_label,
            "calendar_label": calendar_label,
            "value_usd": str(record["value_usd"]),
            "value_billion_usd": str(billions(record["value_usd"])),
            "derivation": record["normalization_method"],
            "sec_fact_ids": record["sec_fact_ids"],
            "_allow": build_item_allowance(
                usd_values=[record["value_usd"]],
                dates=[record["period_start"], record["period_end"]],
                fiscal_labels=[fiscal_label],
                calendar_labels=[calendar_label],
            ),
        }
        records.append(item)
        by_metric_end[(record["metric"], record["period_end"])] = item

    comparisons = []
    for metric, rows in ((m, financials[m]) for m in METRIC_LABELS):
        for row in build_series(rows):
            current = by_metric_end.get((metric, row["period_end"]))
            if current is None:
                continue
            for kind, basis_key in (
                ("quarter_over_quarter", "qoq_basis"),
                ("year_over_year", "yoy_basis"),
            ):
                basis_end = row.get(basis_key)
                basis = by_metric_end.get((metric, basis_end)) if basis_end else None
                if basis is None:
                    continue
                change = percent_change(current["value_usd"], basis["value_usd"])
                if change is None:
                    continue
                suffix = "qoq" if kind == "quarter_over_quarter" else "yoy"
                comparisons.append({
                    "comparison_id": (
                        f"CMP_{metric}_{row['period_end'].replace('-', '')}_{suffix}"
                    ),
                    "kind": kind,
                    "metric": metric,
                    "period_end": current["period_end"],
                    "basis_period_end": basis["period_end"],
                    "current_evidence_id": current["evidence_id"],
                    "basis_evidence_id": basis["evidence_id"],
                    "current_value_billion_usd": current["value_billion_usd"],
                    "basis_value_billion_usd": basis["value_billion_usd"],
                    "change_pct": str(change),
                    "direction": (
                        "increase" if change > 0
                        else "decrease" if change < 0
                        else "unchanged"
                    ),
                    "computed_by": "InsightIQ (Python Decimal), not reported by SEC",
                    "_allow": _merge_allow(
                        current["_allow"],
                        basis["_allow"],
                        build_item_allowance(percentages=[change]),
                    ),
                })

    forecast_items = []
    for metric, item in (forecasts or {}).items():
        q = (quality or {}).get(metric, {})
        predictions = []
        decimals = []
        # Intervals are sent only when they exist and passed the quality gate;
        # otherwise the model sees the point estimate and "interval unavailable".
        intervals_allowed = q.get("interval_display_allowed") is not False
        for row in item.get("predictions") or []:
            if row.get("predicted_billion_usd") is None:
                continue
            point = round(float(row["predicted_billion_usd"]), 2)
            low_raw, high_raw = row.get("lower_95_billion_usd"), row.get("upper_95_billion_usd")
            has_interval = intervals_allowed and low_raw is not None and high_raw is not None
            low = round(float(low_raw), 2) if has_interval else None
            high = round(float(high_raw), 2) if has_interval else None
            predictions.append({
                "quarters_ahead": row.get("fiscal_quarter_ahead"),
                "estimate_billion_usd": point,
                "lower_95_billion_usd": low,
                "upper_95_billion_usd": high,
                "interval": "available" if has_interval else "unavailable (not validated)",
            })
            decimals += [v for v in (point, low, high) if v is not None]
        mae = q.get("selected_mae_billion_usd")
        base = q.get("baseline_mae_billion_usd")
        mae_r = round(float(mae), 2) if mae is not None else None
        base_r = round(float(base), 2) if base is not None else None
        coverage = q.get("interval_coverage")
        coverage_pct = round(float(coverage) * 100, 1) if coverage is not None else None
        forecast_items.append({
            "evidence_id": f"FORECAST_{metric}",
            "kind": "statistical_forecast",
            "metric": metric,
            "nature": "statistical estimate, not a reported result",
            "model": item.get("model"),
            "data_cutoff_period_end": item.get("last_reported_sec_period"),
            "cache_status": item.get("cache_status"),
            "predictions": predictions,
            "holdout_mae_billion_usd": mae_r,
            "baseline_mae_billion_usd": base_r,
            "beats_seasonal_naive_baseline": q.get("beats_baseline"),
            "interval_status": q.get("interval_status"),
            "interval_display_allowed": q.get("interval_display_allowed"),
            "holdout_interval_coverage_pct": coverage_pct,
            "holdout_quarters": q.get("holdout_quarters"),
            "quality_provenance": provenance,
            "_allow": _merge_allow(
                build_item_allowance(
                    decimals=decimals + [v for v in (mae_r, base_r) if v is not None],
                    percentages=[coverage_pct] if coverage_pct is not None else [],
                    dates=[item.get("last_reported_sec_period")],
                    extra_numbers=[q.get("holdout_quarters") or 0],
                ),
            ),
        })

    items = {r["evidence_id"]: r for r in records}
    items.update({c["comparison_id"]: c for c in comparisons})
    items.update({f["evidence_id"]: f for f in forecast_items})

    return {
        "package_version": PACKAGE_VERSION,
        "ticker": ticker,
        "records": records,
        "comparisons": comparisons,
        "forecasts": forecast_items,
        "items": items,
        "limitations": [
            "SEC values are standalone quarters reported or derived from "
            "cumulative filings (see derivation).",
            "Comparisons were computed by InsightIQ only where the periods "
            "are directly comparable.",
            "Forecasts are statistical estimates; interval quality comes "
            "from a stored historical review.",
            "The evidence contains no segment, margin, guidance or "
            "management-commentary data.",
        ],
    }


def _merge_allow(*allows):
    merged = {
        "numbers": set(), "percentages": set(), "dates": set(),
        "fiscal_years": set(), "quarter_years": set(),
    }
    for allow in allows:
        for key in merged:
            merged[key] |= set(allow.get(key, ()))
    return merged


def financial_prompt_evidence(package, profile_options):
    """Compact, model-facing view for one request profile."""
    quarters = profile_options.get("quarters_per_metric", 8)
    forecast_quarters = profile_options.get("forecast_quarters", 4)

    selected_records = []
    for metric in ("revenue", "operating_cash_flow"):
        rows = [r for r in package["records"] if r["metric"] == metric]
        rows.sort(key=lambda r: r["period_end"])
        selected_records += rows[-quarters:]
    selected_ids = {r["evidence_id"] for r in selected_records}

    comparisons = [
        c for c in package["comparisons"]
        if c["current_evidence_id"] in selected_ids
        and c["basis_evidence_id"] in selected_ids
    ]
    # Keep every year-over-year comparison and only the latest N
    # quarter-over-quarter comparisons per metric (bounded payload).
    qoq_limit = profile_options.get("latest_qoq_per_metric", 4)
    qoq_by_metric = {}
    for c in comparisons:
        if c["kind"] == "quarter_over_quarter":
            qoq_by_metric.setdefault(c["metric"], []).append(c)
    kept_qoq = []
    for rows in qoq_by_metric.values():
        rows.sort(key=lambda c: c["period_end"])
        kept_qoq += rows[-qoq_limit:] if qoq_limit else []
    comparisons = [
        c for c in comparisons if c["kind"] == "year_over_year"
    ] + kept_qoq

    return {
        "sec_quarters": [
            {
                "evidence_id": r["evidence_id"],
                "metric": r["metric"],
                "period_start": r["period_start"],
                "period_end": r["period_end"],
                "fiscal_label": r["fiscal_label"],
                "value_usd": r["value_usd"],
                "value_billion_usd": r["value_billion_usd"],
                "derivation": r["derivation"],
            }
            for r in selected_records
        ],
        # Compact form: the identifier encodes metric, period and kind
        # (CMP_<metric>_<period_end>_<qoq|yoy>); the current period is the
        # SEC record with the same metric and period_end.
        "comparison_format": (
            "comparison_id = CMP_<metric>_<period_end yyyymmdd>_<qoq|yoy>; "
            "vs = basis period_end; change_pct computed by InsightIQ"
        ),
        "comparisons": [
            {
                "comparison_id": c["comparison_id"],
                "vs": c["basis_period_end"],
                "change_pct": c["change_pct"],
            }
            for c in comparisons
        ],
        "forecasts": [
            {
                **{k: f[k] for k in (
                    "evidence_id", "metric", "nature", "model",
                    "data_cutoff_period_end", "holdout_mae_billion_usd",
                    "baseline_mae_billion_usd", "beats_seasonal_naive_baseline",
                    "interval_status", "holdout_interval_coverage_pct",
                )},
                "predictions": f["predictions"][:forecast_quarters],
            }
            for f in package["forecasts"]
        ],
        "known_limitations": package["limitations"],
    }


def render_observation(item):
    """Deterministic, evidence-derived sentence for one package item."""
    kind = item.get("kind")
    if kind == "sec_quarter":
        label = item.get("fiscal_label") or item.get("calendar_label")
        return (
            f"{METRIC_LABELS[item['metric']]} for the standalone quarter "
            f"{item['period_start']} to {item['period_end']}"
            f"{f' ({label})' if label else ''}: USD {item['value_billion_usd']} "
            f"billion [{item['evidence_id']}]."
        )
    if kind == "provider_ratio":
        unit = "%" if item.get("unit") == "percent" else ""
        period = f" for the fiscal year ending {item['period_end']}" if item.get("period_end") else ""
        return (f"{item['label']}{period}: {item['value']}{unit} ({item['formula']}; computed by "
                f"InsightIQ from the provider's data) [{item['evidence_id']}].")
    if kind == "provider_period":
        label = item.get("fiscal_label") or item.get("calendar_label")
        unit = "quarter" if item["period_type"] == "quarterly" else "fiscal year"
        return (
            f"{METRIC_LABELS[item['metric']]} for the {unit} "
            f"{item['period_start']} to {item['period_end']}"
            f"{f' ({label})' if label else ''}: {item['currency']} {item['value_billion_usd']} "
            f"billion, as reported by the data provider [{item['evidence_id']}]."
        )
    if kind in ("quarter_over_quarter", "year_over_year"):
        basis = "previous quarter" if kind == "quarter_over_quarter" else "same quarter one year earlier"
        sign = "+" if item["direction"] == "increase" else ""
        return (
            f"{METRIC_LABELS[item['metric']]} change versus the {basis} "
            f"(quarter ended {item['basis_period_end']}): {sign}{item['change_pct']}% "
            f"[{item['comparison_id']}, computed by InsightIQ]."
        )
    if kind == "statistical_forecast":
        first = (item.get("predictions") or [None])[0]
        if not first:
            return None
        low, high = first.get("lower_95_billion_usd"), first.get("upper_95_billion_usd")
        interval = (
            f"95% model interval {low:.2f}–{high:.2f}"
            if low is not None and high is not None
            else "95% model interval not shown (it did not pass the out-of-sample quality check)"
        )
        return (
            f"Statistical estimate (not a reported result) for the next quarter's "
            f"{METRIC_LABELS[item['metric']].lower()}: USD {first['estimate_billion_usd']:.2f} "
            f"billion, {interval} [{item['evidence_id']}]."
        )
    return None


# ------------------------------------------------------------------ news

def news_content_basis(article):
    description = (article.get("description") or "").strip()
    title = (article.get("title") or "").strip()
    if len(description) < HEADLINE_ONLY_MIN_CHARS or description == title:
        return "headline_only"
    return "headline_and_description_excerpt"


def excerpt(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut + " …", True


def build_news_package(ticker, assessed_articles, excerpt_chars=EXCERPT_FULL_CHARS):
    """assessed_articles: classified selected articles (primary first)."""
    items = []
    for article in assessed_articles:
        basis = news_content_basis(article)
        text, truncated = ("", False)
        if basis != "headline_only":
            text, truncated = excerpt(article.get("description"), excerpt_chars)
        published = str(article.get("published_at") or "")[:10] or None
        numbers, pcts, dates = text_number_allowance(
            f"{article.get('title') or ''} {text}"
        )
        allow = build_item_allowance(dates=[published] if published else [])
        allow["numbers"] |= numbers
        allow["percentages"] |= pcts
        allow["dates"] |= dates
        items.append({
            "evidence_id": article["evidence_id"],
            "kind": "news_article",
            "headline": article.get("title"),
            "publisher": article.get("publisher"),
            "published_date": published,
            "source_url": article.get("source_url"),
            "evidence_category": article.get("evidence_category"),
            "company_specific": bool(article.get("company_specific_evidence_eligible")),
            "event_theme": article.get("event_theme"),
            "content_basis": basis,
            "description_excerpt": text,
            "excerpt_truncated": truncated,
            "_allow": allow,
        })
    return {
        "package_version": PACKAGE_VERSION,
        "ticker": ticker,
        "articles": items,
        "items": {i["evidence_id"]: i for i in items},
    }


def news_prompt_evidence(package):
    return [
        {
            k: item[k] for k in (
                "evidence_id", "headline", "publisher", "published_date",
                "evidence_category", "company_specific", "content_basis",
                "description_excerpt", "excerpt_truncated",
            )
        }
        for item in package["articles"]
    ]
