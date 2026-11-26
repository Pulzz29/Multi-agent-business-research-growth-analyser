"""
Forecast qualification for market (provider-data) companies, US and India.

The forecasting engine (services/forecasting.py: SARIMAX model selection,
walk-forward backtests against a seasonal-naive baseline, out-of-sample
interval gate) is only run when the history qualifies. For provider data
every check below is evaluated on the stored statements and reported:

1. contiguous quarters (no gaps; nothing interpolated) >= forecasting.MIN_TRAIN
   + HORIZON for validation and >= forecasting.MIN_HISTORY for model selection
   with an independent hold-out;
2. point-in-time availability of every quarter's values, needed for a
   backtest that cannot leak data published after each fold's origin.
   Provider statements carry no regulatory filing dates. Results-announcement
   dates (Yahoo earnings calendar) are matched and reported, but they do not
   prove that the stored values are the versions first published (providers
   show their current, possibly restated, figures), so they never make a
   history eligible on their own;
3. one statement currency;
4. annual / quarterly reconciliation: the four quarters inside each stored
   fiscal year are compared with the annual figure, which also detects
   cumulative (year-to-date) quarterly disclosures.

When any check fails the forecast is unavailable and the exact reasons are
returned with the genuine historical series (for charts). Stock prices are
never used as a substitute for financial statements.
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.services import fiscal
from app.services import market_storage as storage

METRICS = ("revenue", "net_income", "operating_cash_flow")
LABELS = {"revenue": "Revenue", "net_income": "Net income", "operating_cash_flow": "Operating cash flow"}
RECONCILIATION_TOLERANCE = Decimal("0.02")  # 2 % of the annual figure


def _dec(text):
    try:
        number = Decimal(str(text))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _date(text):
    return date.fromisoformat(str(text)[:10])


def _series(rows_by_period, metric, market, period_type):
    out = []
    for end in sorted(rows_by_period):
        row = rows_by_period[end].get(metric)
        value = _dec(row["value_text"]) if row else None
        if value is None:
            continue
        out.append({"period_end": end, "label": fiscal.period_label(end, market, period_type),
                    "value": float(value), "currency": row.get("currency"),
                    "fiscal_year": row.get("fiscal_year"), "provider": row.get("provider")})
    return out


def _reconcile(quarterly, annual):
    """Compare each annual figure with the sum of the four quarters inside it."""
    results = []
    by_end = {q["period_end"]: q for q in quarterly}
    for year in annual:
        end = _date(year["period_end"])
        # The fiscal year's own quarters end within ~9 months before its end
        # (the previous year's final quarter ends ~365 days earlier).
        start = end - timedelta(days=330)
        inside = [q for q in quarterly if start < _date(q["period_end"]) <= end]
        if len(inside) != 4:
            results.append({"fiscal_period_end": year["period_end"], "status": "not_checkable",
                            "quarters_found": len(inside)})
            continue
        quarter_sum = sum(Decimal(str(q["value"])) for q in inside)
        annual_value = Decimal(str(year["value"]))
        last_quarter = Decimal(str(by_end.get(year["period_end"], inside[-1])["value"]))
        difference = abs(quarter_sum - annual_value)
        tolerance = abs(annual_value) * RECONCILIATION_TOLERANCE
        if difference <= tolerance:
            status = "reconciled"
        elif annual_value and abs(last_quarter - annual_value) <= tolerance:
            status = "cumulative_quarters"  # Q4 equals the full year: year-to-date figures
        else:
            status = "mismatch"
        results.append({"fiscal_period_end": year["period_end"], "status": status, "quarters_found": 4,
                        "quarters_sum": float(quarter_sum), "annual": float(annual_value),
                        "difference_pct": float(difference / abs(annual_value) * 100) if annual_value else None})
    return results


def _announcements(contiguous, announcements, max_days=120):
    """First announcement on or after each quarter end (within max_days)."""
    out = {}
    for q in contiguous:
        end = q["period_end"]
        limit = (_date(end) + timedelta(days=max_days)).isoformat()
        match = next((a for a in announcements if end < a <= limit), None)
        if match:
            out[end] = match
    return {"dated": len(out), "all_dated": bool(contiguous) and len(out) == len(contiguous), "dates": out}


def qualify(market, symbol):
    from app.agents.provider_financial import _with_starts
    from app.services import forecasting

    stored = storage.get_company(symbol) or {}
    profile = stored.get("profile") or {}
    announcements = sorted(profile.get("announcement_dates") or [])
    announcement_source = profile.get("announcement_dates_source")
    quarterly_rows = storage.statement_values(symbol, "quarterly")
    annual_rows = storage.statement_values(symbol, "annual")
    validation_min = forecasting.MIN_TRAIN + forecasting.HORIZON
    selection_min = forecasting.MIN_HISTORY
    metrics = []
    for metric in METRICS:
        quarterly = _series(quarterly_rows, metric, market, "quarterly")
        annual = _series(annual_rows, metric, market, "annual")
        dated = [{"period_start": start, "period_end": end, "value": row["value_text"]}
                 for end, row, start, _ in _with_starts(
                     sorted((e, m[metric]) for e, m in quarterly_rows.items() if metric in m), "quarterly")]
        contiguous = forecasting.contiguous_recent_history(dated)
        filing = _announcements(contiguous, announcements)
        currencies = sorted({p["currency"] for p in quarterly + annual if p.get("currency")})
        reconciliation = _reconcile(quarterly, annual)
        reasons = []
        if not quarterly:
            reasons.append("No quarterly statements are stored for this metric.")
        elif len(contiguous) < validation_min:
            reasons.append(
                f"Only {len(contiguous)} contiguous quarters are stored "
                f"({len(quarterly)} quarters in total); the backtested forecast needs at least "
                f"{validation_min} to validate and {selection_min} for model selection with an "
                "independent hold-out. Missing quarters are never interpolated."
            )
        if quarterly:
            dated = (f"Results-announcement dates were matched for {filing['dated']} of {len(contiguous)} "
                     "contiguous quarters (Yahoo Finance earnings calendar), but these are not regulatory "
                     "filing records and the stored values are the provider's current figures, not the "
                     "versions first published on those dates; " if announcements else
                     "The provider's statements carry no filing dates; ")
            reasons.append(dated + "a leakage-free point-in-time backtest therefore cannot be established.")
        if len(currencies) > 1:
            reasons.append(f"Statements are reported in more than one currency ({', '.join(currencies)}).")
        bad = [r for r in reconciliation if r["status"] in ("mismatch", "cumulative_quarters")]
        if bad:
            kinds = sorted({r["status"] for r in bad})
            reasons.append("Quarterly and annual figures do not reconcile for "
                           f"{len(bad)} fiscal year(s) ({', '.join(kinds)}).")
        metrics.append({
            "metric": metric,
            "label": LABELS[metric],
            "status": "unavailable" if reasons else "qualified",
            "reasons": reasons,
            "checks": {
                "quarters_stored": len(quarterly),
                "contiguous_quarters": len(contiguous),
                "required_for_validation": validation_min,
                "required_for_model_selection": selection_min,
                "filing_dates_available": False,
                "point_in_time_values": False,
                "announcement_dates_matched": filing["dated"],
                "announcement_dates_source": announcement_source if announcements else None,
                "first_announced": filing["dates"],
                "currencies": currencies,
                "reconciliation": reconciliation,
            },
            "quarterly": quarterly,
            "annual": annual,
            "forecast": None,
        })
    return {"market": market, "symbol": symbol, "metrics": metrics,
            "engine": "SARIMAX model selection with walk-forward backtests vs seasonal-naive baseline",
            "note": ("Forecasts are shown only when every check passes; historical values are "
                     "the provider's reported statements, never stock prices.")}
