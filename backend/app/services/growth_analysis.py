"""
Growth analysis: one definition of every growth metric, used by the company
pages, the report charts, the AI research brief and the PDF.

The first part is pure (no I/O): it takes period series and returns metric
results. The adapters at the end read stored data (SEC facts, provider
statements) and never call an external service.

Rules (also returned as text in ``DEFINITIONS`` for tooltips and docs):

* growth = (current - base) / base. A base of zero, or a negative base (a loss,
  or negative cash flow), makes a percentage meaningless: the result is "n/a"
  with a reason and the absolute change instead. Nothing returns NaN/Infinity.
* QoQ compares with the adjacent quarter (period end 80-100 days earlier).
* Quarterly YoY compares with the same fiscal quarter one year earlier (period
  end 350-380 days earlier, and the same fiscal quarter number when known),
  never simply "four rows back". It needs at least five quarters of history.
* Annual YoY compares with the prior fiscal year (period end 335-395 days
  earlier). CAGR over N years needs fiscal years exactly N years apart
  (+/- 45 days), i.e. N + 1 annual periods, and positive values at both ends.
* Short or broken history is reported as "Insufficient history (need N periods,
  have M)"; values are never extrapolated or filled in.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

GROWTH_VERSION = 1

QOQ_GAP_DAYS = (80, 100)
YOY_QUARTER_GAP_DAYS = (350, 380)
YOY_ANNUAL_GAP_DAYS = (335, 395)
CAGR_TOLERANCE_DAYS = 45
MIN_QUARTERS_FOR_YOY = 5
TREND_THRESHOLD_PP = 2.0
STALE_QUARTER_DAYS = 200
STALE_ANNUAL_DAYS = 450

TREND_RULE = (
    "Uses the year-over-year revenue growth series (quarterly when at least three "
    "quarterly YoY values exist, otherwise annual). Accelerating: the latest YoY "
    f"rate is at least {TREND_THRESHOLD_PP:g} percentage points above the average of "
    "the previous two. Decelerating: at least "
    f"{TREND_THRESHOLD_PP:g} points below. Stable: in between. Needs three YoY values."
)

DEFINITIONS = {
    "revenue_qoq": "Revenue growth versus the immediately preceding quarter.",
    "revenue_yoy": "Revenue growth versus the same fiscal quarter one year earlier.",
    "net_income_yoy": "Net profit growth versus the same fiscal quarter one year earlier.",
    "eps_yoy": "Diluted EPS growth versus the same fiscal quarter one year earlier "
               "(reported quarterly EPS only; fourth-quarter EPS is never derived).",
    "revenue_annual_yoy": "Revenue growth versus the prior fiscal year.",
    "net_income_annual_yoy": "Net profit growth versus the prior fiscal year.",
    "eps_annual_yoy": "Diluted EPS growth versus the prior fiscal year.",
    "revenue_cagr_3y": "Compound annual revenue growth over the last 3 fiscal years.",
    "revenue_cagr_5y": "Compound annual revenue growth over the last 5 fiscal years.",
    "net_income_cagr_3y": "Compound annual net profit growth over the last 3 fiscal years.",
    "net_income_cagr_5y": "Compound annual net profit growth over the last 5 fiscal years.",
}

LABELS = {
    "revenue_qoq": "Revenue growth (QoQ)",
    "revenue_yoy": "Revenue growth (YoY, quarter)",
    "net_income_yoy": "Net profit growth (YoY, quarter)",
    "eps_yoy": "EPS growth (YoY, quarter)",
    "revenue_annual_yoy": "Revenue growth (YoY, fiscal year)",
    "net_income_annual_yoy": "Net profit growth (YoY, fiscal year)",
    "eps_annual_yoy": "EPS growth (YoY, fiscal year)",
    "revenue_cagr_3y": "Revenue CAGR (3 years)",
    "revenue_cagr_5y": "Revenue CAGR (5 years)",
    "net_income_cagr_3y": "Net profit CAGR (3 years)",
    "net_income_cagr_5y": "Net profit CAGR (5 years)",
}

METRIC_NOUN = {"revenue": "revenue", "net_income": "net profit", "eps": "diluted EPS"}


# ------------------------------------------------------------------ helpers

def _date(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _dec(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _num(value):
    number = _dec(value)
    return None if number is None else float(number)


def clean_points(points):
    """Sorted, de-duplicated periods with a finite value and a valid end date."""
    by_end = {}
    for point in points or []:
        end = _date(point.get("period_end"))
        value = _dec(point.get("value"))
        if end is None or value is None:
            continue
        by_end[end] = {**point, "period_end": end.isoformat(), "value": value, "_end": end}
    return [by_end[k] for k in sorted(by_end)]


def _point(p):
    return None if p is None else {"period_end": p["period_end"], "value": float(p["value"])}


def change(current, base):
    """Growth between two values with the meaningfulness rules applied."""
    cur, prev = _dec(current), _dec(base)
    if cur is None or prev is None:
        return {"value_pct": None, "status": "unavailable", "reason": "A value is missing for one of the periods."}
    absolute = float(cur - prev)
    if prev == 0:
        return {"value_pct": None, "status": "not_meaningful", "absolute_change": absolute,
                "reason": "n/a: the comparison base is zero."}
    if prev < 0:
        reason = ("n/a: the comparison base is negative (a loss), so a growth percentage is not meaningful; "
                  "the absolute change is shown instead.")
        return {"value_pct": None, "status": "not_meaningful", "absolute_change": absolute, "reason": reason}
    return {"value_pct": round(float((cur - prev) / prev * 100), 2), "status": "ok",
            "absolute_change": absolute, "reason": None}


def _result(key, basis, outcome, current=None, base=None):
    return {
        "key": key,
        "label": LABELS.get(key, key),
        "definition": DEFINITIONS.get(key),
        "basis": basis,
        "value_pct": outcome.get("value_pct"),
        "status": outcome["status"],
        "reason": outcome.get("reason"),
        "absolute_change": outcome.get("absolute_change"),
        "current": _point(current),
        "base": _point(base),
    }


def insufficient(key, basis, need, have, unit="periods"):
    return _result(key, basis, {
        "value_pct": None, "status": "insufficient_history",
        "reason": f"Insufficient history (need {need} {unit}, have {have}).",
    })


def unavailable(key, basis, reason):
    return _result(key, basis, {"value_pct": None, "status": "unavailable", "reason": reason})


# ------------------------------------------------------------- comparisons

def _in(gap, bounds):
    return bounds[0] <= gap <= bounds[1]


def find_adjacent_quarter(points, index):
    current = points[index]
    for prev in reversed(points[:index]):
        gap = (current["_end"] - prev["_end"]).days
        if _in(gap, QOQ_GAP_DAYS):
            return prev
        if gap > QOQ_GAP_DAYS[1]:
            return None
    return None


def find_same_quarter_last_year(points, index):
    current = points[index]
    for prev in reversed(points[:index]):
        gap = (current["_end"] - prev["_end"]).days
        if gap > YOY_QUARTER_GAP_DAYS[1]:
            return None
        if _in(gap, YOY_QUARTER_GAP_DAYS):
            fq, pfq = current.get("fiscal_quarter"), prev.get("fiscal_quarter")
            if fq is not None and pfq is not None and fq != pfq:
                continue
            return prev
    return None


def find_prior_year(points, index):
    current = points[index]
    for prev in reversed(points[:index]):
        gap = (current["_end"] - prev["_end"]).days
        if _in(gap, YOY_ANNUAL_GAP_DAYS):
            return prev
        if gap > YOY_ANNUAL_GAP_DAYS[1]:
            return None
    return None


def qoq(key, quarters):
    points = clean_points(quarters)
    if len(points) < 2:
        return insufficient(key, "quarterly", 2, len(points), "quarters")
    base = find_adjacent_quarter(points, len(points) - 1)
    if base is None:
        return unavailable(key, "quarterly", "n/a: the immediately preceding quarter is not available (gap in history).")
    return _result(key, "quarterly", change(points[-1]["value"], base["value"]), points[-1], base)


def yoy_quarter(key, quarters):
    points = clean_points(quarters)
    if len(points) < MIN_QUARTERS_FOR_YOY:
        return insufficient(key, "quarterly", MIN_QUARTERS_FOR_YOY, len(points), "quarters")
    base = find_same_quarter_last_year(points, len(points) - 1)
    if base is None:
        return unavailable(key, "quarterly", "n/a: the matching quarter one year earlier is not available.")
    return _result(key, "quarterly", change(points[-1]["value"], base["value"]), points[-1], base)


def yoy_annual(key, years):
    points = clean_points(years)
    if len(points) < 2:
        return insufficient(key, "annual", 2, len(points), "fiscal years")
    base = find_prior_year(points, len(points) - 1)
    if base is None:
        return unavailable(key, "annual", "n/a: the prior fiscal year is not available (gap in history).")
    return _result(key, "annual", change(points[-1]["value"], base["value"]), points[-1], base)


def cagr(key, years, n_years):
    points = clean_points(years)
    if len(points) < n_years + 1:
        return insufficient(key, "annual", n_years + 1, len(points), "fiscal years")
    end = points[-1]
    target = end["_end"].toordinal() - round(365.25 * n_years)
    start = min(points[:-1], key=lambda p: abs(p["_end"].toordinal() - target))
    if abs(start["_end"].toordinal() - target) > CAGR_TOLERANCE_DAYS:
        return unavailable(key, "annual", f"n/a: no fiscal year ends exactly {n_years} years before the latest one.")
    if start["value"] <= 0 or end["value"] <= 0:
        return _result(key, "annual", {
            "value_pct": None, "status": "not_meaningful",
            "absolute_change": float(end["value"] - start["value"]),
            "reason": "n/a: CAGR needs positive values at both ends (a loss or zero value is present).",
        }, end, start)
    rate = (float(end["value"]) / float(start["value"])) ** (1.0 / n_years) - 1.0
    return _result(key, "annual", {"value_pct": round(rate * 100, 2), "status": "ok", "reason": None,
                                   "absolute_change": float(end["value"] - start["value"])}, end, start)


def yoy_points(quarters=None, years=None):
    """Every YoY observation (for charts): quarterly when quarters are given."""
    out = []
    if quarters is not None:
        points = clean_points(quarters)
        for i in range(len(points)):
            base = find_same_quarter_last_year(points, i)
            if base is not None:
                out.append({"period_end": points[i]["period_end"], "base_period_end": base["period_end"],
                            **change(points[i]["value"], base["value"])})
    else:
        points = clean_points(years)
        for i in range(len(points)):
            base = find_prior_year(points, i)
            if base is not None:
                out.append({"period_end": points[i]["period_end"], "base_period_end": base["period_end"],
                            **change(points[i]["value"], base["value"])})
    return out


def trend(quarterly_yoy, annual_yoy):
    """Accelerating / stable / decelerating, with the rule that decided it."""
    for basis, series in (("quarterly", quarterly_yoy), ("annual", annual_yoy)):
        values = [p for p in series or [] if p.get("status") == "ok"]
        if len(values) >= 3:
            latest = values[-1]["value_pct"]
            previous = (values[-2]["value_pct"] + values[-3]["value_pct"]) / 2
            delta = round(latest - previous, 2)
            label = ("accelerating" if delta >= TREND_THRESHOLD_PP
                     else "decelerating" if delta <= -TREND_THRESHOLD_PP else "stable")
            return {"label": label, "basis": basis, "latest_pct": latest,
                    "previous_average_pct": round(previous, 2), "difference_pp": delta,
                    "periods": [v["period_end"] for v in values[-3:]], "rule": TREND_RULE}
    have = max(len([p for p in quarterly_yoy or [] if p.get("status") == "ok"]),
               len([p for p in annual_yoy or [] if p.get("status") == "ok"]))
    return {"label": "insufficient_history", "basis": None, "rule": TREND_RULE,
            "reason": f"Insufficient history (need 3 year-over-year values, have {have})."}


def staleness(quarters, years, as_of):
    as_of_date = _date(as_of) or date.today()
    q, y = clean_points(quarters), clean_points(years)
    latest_q = q[-1]["period_end"] if q else None
    latest_y = y[-1]["period_end"] if y else None
    q_age = (as_of_date - q[-1]["_end"]).days if q else None
    y_age = (as_of_date - y[-1]["_end"]).days if y else None
    stale_q = q_age is not None and q_age > STALE_QUARTER_DAYS
    stale_y = y_age is not None and y_age > STALE_ANNUAL_DAYS
    notes = []
    if stale_q:
        notes.append(f"The latest stored quarter ended {latest_q}, more than {STALE_QUARTER_DAYS} days before "
                     f"{as_of_date.isoformat()}; quarterly growth describes that period, not the current one.")
    if stale_y:
        notes.append(f"The latest stored fiscal year ended {latest_y}; annual growth may be out of date.")
    return {"latest_quarter_end": latest_q, "latest_year_end": latest_y,
            "stale_quarterly": stale_q, "stale_annual": stale_y, "notes": notes}


def _fmt_pct(value):
    return f"{value:+.1f}%"


def readout(metrics, trend_result, stale):
    """Two or three plain sentences built only from the computed values."""
    sentences = []
    for key in ("revenue_yoy", "revenue_annual_yoy"):
        m = metrics.get(key)
        if m and m["status"] == "ok":
            period = "quarter" if m["basis"] == "quarterly" else "fiscal year"
            sentences.append(
                f"Revenue changed {_fmt_pct(m['value_pct'])} year over year in the {period} ended "
                f"{m['current']['period_end']} (compared with {m['base']['period_end']})."
            )
            break
    else:
        m = metrics.get("revenue_yoy") or {}
        sentences.append(f"Year-over-year revenue growth is not available: {m.get('reason') or 'no data'}")
    if trend_result["label"] in ("accelerating", "decelerating", "stable"):
        sentences.append(
            f"Growth is {trend_result['label']}: the latest {trend_result['basis']} YoY rate "
            f"({trend_result['latest_pct']:+.1f}%) is {trend_result['difference_pp']:+.1f} percentage points "
            f"from the average of the previous two ({trend_result['previous_average_pct']:+.1f}%)."
        )
    for key in ("net_income_yoy", "net_income_annual_yoy"):
        m = metrics.get(key)
        if m and m["status"] == "ok":
            sentences.append(f"Net profit changed {_fmt_pct(m['value_pct'])} on the same basis "
                             f"({m['current']['period_end']} vs {m['base']['period_end']}).")
            break
        if m and m["status"] == "not_meaningful":
            sentences.append("Net profit growth is not meaningful because the comparison base is a loss or zero.")
            break
    if stale["stale_quarterly"] or stale["stale_annual"]:
        sentences.append("Note: the latest stored data is old; see the data-age note.")
    return " ".join(sentences)


def analyze(series, *, as_of=None, currency=None, source=None):
    """
    series: {"revenue"|"net_income"|"eps": {"quarterly": [points], "annual": [points]}}
    where a point is {"period_end", "value", optional "fiscal_quarter"}.
    """
    def get(metric, basis):
        return (series.get(metric) or {}).get(basis) or []

    metrics = {}
    for metric in ("revenue", "net_income", "eps"):
        quarters, years = get(metric, "quarterly"), get(metric, "annual")
        if metric == "revenue":
            metrics["revenue_qoq"] = qoq("revenue_qoq", quarters)
        metrics[f"{metric}_yoy"] = yoy_quarter(f"{metric}_yoy", quarters)
        metrics[f"{metric}_annual_yoy"] = yoy_annual(f"{metric}_annual_yoy", years)
        if metric in ("revenue", "net_income"):
            for n in (3, 5):
                metrics[f"{metric}_cagr_{n}y"] = cagr(f"{metric}_cagr_{n}y", years, n)
        if not quarters and not years:
            note = ("EPS is not stored for this company yet; it is stored from the same filings or statements "
                    "at the next data refresh." if metric == "eps" else f"No stored {METRIC_NOUN[metric]} data.")
            for key in [k for k in metrics if k.startswith(f"{metric}_")]:
                metrics[key] = unavailable(key, metrics[key]["basis"], note)

    yoy_series = {
        metric: {
            "quarterly": yoy_points(quarters=get(metric, "quarterly")),
            "annual": yoy_points(years=get(metric, "annual")),
        }
        for metric in ("revenue", "net_income")
    }
    trend_result = trend(yoy_series["revenue"]["quarterly"], yoy_series["revenue"]["annual"])
    stale = staleness(get("revenue", "quarterly"), get("revenue", "annual"), as_of)
    return {
        "version": GROWTH_VERSION,
        "as_of": str(as_of)[:10] if as_of else None,
        "currency": currency,
        "source": source,
        "metrics": metrics,
        "yoy_series": yoy_series,
        "trend": trend_result,
        "staleness": stale,
        "readout": readout(metrics, trend_result, stale),
        "definitions": DEFINITIONS,
        "history": {
            metric: {"quarters": len(clean_points(get(metric, "quarterly"))),
                     "fiscal_years": len(clean_points(get(metric, "annual")))}
            for metric in ("revenue", "net_income", "eps")
        },
    }


# ----------------------------------------------------------------- adapters
# Read stored data only. ``as_of`` (ISO timestamp) limits the data to what was
# stored (provider) or filed (SEC) by then, for point-in-time report datasets.

def sec_series(ticker, as_of=None):
    """Quarterly and annual revenue / net income / EPS from SEC facts."""
    from app.services import financial_normalization as fin

    base = fin.normalized_quarterly_financials(ticker, as_of=as_of)
    extended = fin.normalized_extended_quarterly(ticker, as_of=as_of)

    def quarters(rows):
        return [{"period_end": r["period_end"], "period_start": r["period_start"],
                 "fiscal_quarter": r["fiscal_quarter"], "fiscal_year_start": r["fiscal_year_start"],
                 "value": r["value"]} for r in rows]

    def annual(rows):
        by_year = {}
        for r in rows:
            by_year.setdefault(r["fiscal_year_start"], []).append(r)
        out = []
        for _, qs in sorted(by_year.items()):
            if sorted(q["fiscal_quarter"] for q in qs) == [1, 2, 3, 4]:
                end = max(q["period_end"] for q in qs)
                out.append({"period_end": end, "value": str(sum(Decimal(q["value"]) for q in qs))})
        return out

    revenue = base.get("revenue", [])
    net_income = extended.get("net_income", [])
    eps = fin.sec_eps_series(ticker, as_of=as_of)
    return {
        "revenue": {"quarterly": quarters(revenue), "annual": annual(revenue)},
        "net_income": {"quarterly": quarters(net_income), "annual": annual(net_income)},
        "eps": eps,
        "operating_income": {"quarterly": quarters(extended.get("operating_income", []))},
        "gross_profit": {"quarterly": quarters(extended.get("gross_profit", []))},
        "_diagnostics": extended.get("diagnostics", []),
    }


def provider_series(symbol, as_of=None):
    """Quarterly and annual series from stored provider statements."""
    from app.services import market_storage as storage

    out = {}
    currency = None
    for period_type, basis in (("quarterly", "quarterly"), ("annual", "annual")):
        periods = storage.statement_values(symbol, period_type, as_of=as_of)
        for period_end, metrics in periods.items():
            for metric, row in metrics.items():
                key = "eps" if metric == "eps_diluted" else metric
                currency = currency or row.get("currency")
                out.setdefault(key, {}).setdefault(basis, []).append(
                    {"period_end": period_end, "value": row["value_text"]})
    out["_currency"] = currency
    return out


def company_growth(ticker, *, as_of=None):
    """Growth analysis for any supported company from stored data."""
    from app.services.sec_coverage import SEC_VALIDATED

    if ticker in SEC_VALIDATED:
        series = sec_series(ticker, as_of=as_of)
        result = analyze(series, as_of=as_of or date.today().isoformat(), currency="USD",
                         source="SEC filings (10-Q / 10-K)")
    else:
        series = provider_series(ticker, as_of=as_of)
        result = analyze(series, as_of=as_of or date.today().isoformat(), currency=series.get("_currency"),
                         source="Provider financial statements (stored)")
    return result, series
