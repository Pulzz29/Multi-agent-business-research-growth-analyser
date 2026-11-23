from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from app.database.sqlite_manager import connect


REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
OCF_CONCEPT = "NetCashProvidedByUsedInOperatingActivities"
ANNUAL_FORMS = frozenset({"10-K", "10-K/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})


class FinancialNormalizationError(RuntimeError):
    pass


def parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def duration(row):
    start = parse_date(row.get("period_start"))
    end = parse_date(row.get("period_end"))
    if start is None or end is None or end < start:
        return None
    return (end - start).days + 1


def decimal_value(row):
    if row is None:
        return None
    try:
        value = Decimal(str(row["value_text"]))
        return value if value.is_finite() else None
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None


def evidence(row):
    return {
        "sec_fact_id": row["id"],
        "taxonomy": row["taxonomy"],
        "concept": row["concept"],
        "unit": row["unit"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "form": row["form"],
        "filed_at": row["filed_at"],
        "accession": row["accession"],
        "fiscal_year_reported": row["fiscal_year"],
        "fiscal_period_reported": row["fiscal_period"],
        "sec_frame": row["sec_frame"],
        "source_url": row["source_url"],
    }


def select_latest_filing(rows):
    eligible = [
        row for row in rows
        if decimal_value(row) is not None
        and row.get("accession")
        and row.get("filed_at")
        and row.get("form") in ANNUAL_FORMS | QUARTERLY_FORMS
    ]
    if not eligible:
        return None

    return max(
        eligible,
        key=lambda r: (
            r["filed_at"],
            r["form"].endswith("/A"),
            r["accession"],
            r["id"],
        ),
    )


def exact_fact(rows, concept, start, end, allowed_forms):
    return select_latest_filing([
        row for row in rows
        if row["concept"] == concept
        and row["unit"] == "USD"
        and row["period_start"] == start
        and row["period_end"] == end
        and row["form"] in allowed_forms
    ])


def load_financial_rows(ticker):
    concepts = (*REVENUE_CONCEPTS, OCF_CONCEPT)
    placeholders = ",".join("?" for _ in concepts)

    with connect() as conn:
        records = conn.execute(
            f"""
            SELECT *
            FROM sec_facts
            WHERE company_ticker = ?
              AND unit = 'USD'
              AND concept IN ({placeholders})
            ORDER BY period_end, filed_at
            """,
            (ticker, *concepts),
        ).fetchall()

    return [dict(row) for row in records]


def fiscal_year_anchors(rows):
    # Fiscal boundaries must originate from true annual 10-K facts.
    year_candidates = defaultdict(list)

    for row in rows:
        if (
            row["concept"] not in REVENUE_CONCEPTS
            or row["form"] not in ANNUAL_FORMS
            or not 321 <= (duration(row) or 0) <= 400
            or decimal_value(row) is None
        ):
            continue

        key = (row["period_start"], row["period_end"])
        year_candidates[key].append(row)

    annual = {}

    for (start, end), candidates in year_candidates.items():
        chosen = select_latest_filing(candidates)
        if chosen:
            existing = annual.get(start)

            if (
                existing is None
                or (end, chosen["filed_at"])
                > (existing["end"], existing["filed_at"])
            ):
                annual[start] = {
                    "start": start,
                    "end": end,
                    "filed_at": chosen["filed_at"],
                }

    anchors = sorted(
        annual.values(),
        key=lambda item: item["start"],
    )

    # Discover a current, not-yet-annualized fiscal year.
    if anchors:
        latest_end = parse_date(anchors[-1]["end"])
        next_start = (latest_end + timedelta(days=1)).isoformat()

        has_new_quarter = any(
            row["period_start"] == next_start
            and row["form"] in QUARTERLY_FORMS
            and row["concept"] in REVENUE_CONCEPTS
            and 60 <= (duration(row) or 0) <= 120
            for row in rows
        )

        if has_new_quarter:
            anchors.append({
                "start": next_start,
                "end": None,
                "filed_at": None,
            })

    return anchors


def quarter_ends_for_year(rows, fiscal_start, fiscal_end):
    """
    Derive Q1/Q2/Q3 ends using cumulative observations
    starting at the actual fiscal-year boundary.

    A Q2 cumulative value cannot be treated as Q2 itself.
    """
    ranges = {
        1: (60, 120),
        2: (121, 220),
        3: (221, 320),
    }

    candidates = defaultdict(Counter)
    fiscal_end_date = parse_date(fiscal_end)

    for row in rows:
        if (
            row["form"] not in QUARTERLY_FORMS
            or row["period_start"] != fiscal_start
            or row["unit"] != "USD"
            or row["concept"] not in (*REVENUE_CONCEPTS, OCF_CONCEPT)
            or decimal_value(row) is None
        ):
            continue

        days = duration(row)
        end = parse_date(row["period_end"])

        if days is None or end is None:
            continue

        if fiscal_end_date and end >= fiscal_end_date:
            continue

        for number, (minimum, maximum) in ranges.items():
            if minimum <= days <= maximum:
                candidates[number][row["period_end"]] += 1
                break

    ends = {}

    for number in (1, 2, 3):
        if not candidates[number]:
            continue

        # Prefer the end date supported by the most SEC facts.
        # Use the later end only to break an evidence-count tie.
        ends[number] = max(
            candidates[number],
            key=lambda end: (candidates[number][end], end),
        )

    # Reject inconsistent quarter ordering.
    for number in (2, 3):
        if number in ends and number - 1 in ends:
            if ends[number] <= ends[number - 1]:
                raise FinancialNormalizationError(
                    f"Invalid fiscal quarter-end sequence: {fiscal_start}"
                )

    return ends


def choose_revenue_concept(rows, fiscal_start, fiscal_end, ends):
    ranked = []

    for priority, concept in enumerate(REVENUE_CONCEPTS):
        available_ytd = sum(
            exact_fact(
                rows, concept, fiscal_start,
                end, QUARTERLY_FORMS
            ) is not None
            for end in ends.values()
        )

        annual = (
            exact_fact(
                rows, concept, fiscal_start,
                fiscal_end, ANNUAL_FORMS
            )
            if fiscal_end else None
        )

        if available_ytd:
            ranked.append((
                available_ytd,
                int(annual is not None),
                -priority,
                concept,
            ))

    return max(ranked)[-1] if ranked else None


def make_quarter(
    ticker, metric, fiscal_start, quarter_number,
    start, end, value, concept, method, sources
):
    return {
        "ticker": ticker,
        "metric": metric,
        "fiscal_year_start": fiscal_start,
        "fiscal_quarter": quarter_number,
        "period_start": start,
        "period_end": end,
        "unit": "USD",
        "value": str(value),
        "concept": concept,
        "method": method,
        "evidence": sources,
    }


def normalize_metric_year(
    ticker, rows, fiscal_start, fiscal_end,
    ends, metric, concept
):
    if not concept:
        return [], [{"code": "concept_unavailable", "metric": metric}]

    quarters = []
    diagnostics = []
    previous_ytd = None
    previous_end = None

    for q in (1, 2, 3):
        if q not in ends:
            break

        end = ends[q]
        start = (
            fiscal_start if q == 1
            else (
                parse_date(previous_end) + timedelta(days=1)
            ).isoformat()
            if previous_end else None
        )

        if start is None:
            break

        if not 60 <= (
            parse_date(end) - parse_date(start)
        ).days + 1 <= 120:
            diagnostics.append({
                "code": "invalid_quarter_boundary",
                "metric": metric,
                "quarter": q,
                "end": end,
            })
            break

        current_ytd = exact_fact(
            rows, concept, fiscal_start, end, QUARTERLY_FORMS
        )

        if current_ytd is None:
            diagnostics.append({
                "code": "missing_cumulative_sec_fact",
                "metric": metric,
                "quarter": q,
                "end": end,
            })
            break

        if q == 1:
            value = decimal_value(current_ytd)
            sources = [evidence(current_ytd)]
            method = "first_quarter_ytd"
        else:
            if previous_ytd is None:
                break

            value = (
                decimal_value(current_ytd)
                - decimal_value(previous_ytd)
            )
            sources = [
                evidence(current_ytd),
                evidence(previous_ytd),
            ]
            method = "current_ytd_minus_previous_ytd"

        quarter = make_quarter(
            ticker, metric, fiscal_start, q,
            start, end, value, concept, method, sources
        )
        quarters.append(quarter)

        # Independent cross-check against separately reported
        # direct quarterly observations.
        direct = exact_fact(
            rows, concept, start, end, QUARTERLY_FORMS
        )

        if direct:
            direct_value = decimal_value(direct)
            if direct_value != value:
                diagnostics.append({
                    "code": "direct_ytd_discrepancy",
                    "metric": metric,
                    "quarter": q,
                    "end": end,
                    "derived_value": str(value),
                    "direct_value": str(direct_value),
                    "direct_fact_id": direct["id"],
                })

        previous_ytd = current_ytd
        previous_end = end

    if fiscal_end and len(quarters) == 3:
        annual = exact_fact(
            rows, concept, fiscal_start,
            fiscal_end, ANNUAL_FORMS
        )

        if annual:
            q4_start = (
                parse_date(ends[3]) + timedelta(days=1)
            ).isoformat()

            q4_duration = (
                parse_date(fiscal_end)
                - parse_date(q4_start)
            ).days + 1

            if 60 <= q4_duration <= 120:
                value = (
                    decimal_value(annual)
                    - decimal_value(previous_ytd)
                )

                quarters.append(make_quarter(
                    ticker, metric, fiscal_start, 4,
                    q4_start, fiscal_end, value, concept,
                    "annual_minus_nine_month_ytd",
                    [evidence(annual), evidence(previous_ytd)],
                ))
            else:
                diagnostics.append({
                    "code": "invalid_q4_boundary",
                    "metric": metric,
                    "fiscal_start": fiscal_start,
                })
        else:
            diagnostics.append({
                "code": "annual_fact_unavailable",
                "metric": metric,
                "fiscal_start": fiscal_start,
            })

    if fiscal_end and len(quarters) == 4:
        annual = exact_fact(
            rows, concept, fiscal_start,
            fiscal_end, ANNUAL_FORMS
        )

        total = sum(
            (Decimal(q["value"]) for q in quarters),
            Decimal(0),
        )

        if annual is None or total != decimal_value(annual):
            raise FinancialNormalizationError(
                f"{ticker} {metric} fiscal year {fiscal_start} "
                "failed annual reconciliation."
            )

    return quarters, diagnostics


def normalized_quarterly_financials(ticker):
    rows = load_financial_rows(ticker)

    if not rows:
        return {
            "ticker": ticker,
            "revenue": [],
            "operating_cash_flow": [],
            "warnings": ["no_financial_facts"],
            "diagnostics": [],
        }

    revenue = []
    ocf = []
    diagnostics = []

    for anchor in fiscal_year_anchors(rows):
        start = anchor["start"]
        end = anchor["end"]
        ends = quarter_ends_for_year(rows, start, end)

        if not ends:
            continue

        revenue_concept = choose_revenue_concept(
            rows, start, end, ends
        )

        for metric, concept, destination in (
            ("revenue", revenue_concept, revenue),
            ("operating_cash_flow", OCF_CONCEPT, ocf),
        ):
            quarters, issues = normalize_metric_year(
                ticker, rows, start, end, ends,
                metric, concept
            )

            destination.extend(quarters)

            for issue in issues:
                diagnostics.append({
                    "fiscal_start": start,
                    **issue,
                })

    warnings = []

    for metric, series in (
        ("revenue", revenue),
        ("operating_cash_flow", ocf),
    ):
        series.sort(key=lambda q: q["period_end"])

        observed = [
            (q["period_start"], q["period_end"])
            for q in series
        ]

        if len(observed) != len(set(observed)):
            raise FinancialNormalizationError(
                f"{ticker}: duplicate {metric} quarters."
            )

        if not series:
            warnings.append(f"{metric}_unavailable")

        for quarter in series:
            if not quarter["evidence"]:
                raise FinancialNormalizationError(
                    f"{ticker}: missing {metric} provenance."
                )

    return {
        "ticker": ticker,
        "revenue": revenue,
        "operating_cash_flow": ocf,
        "warnings": warnings,
        "diagnostics": diagnostics,
    }
