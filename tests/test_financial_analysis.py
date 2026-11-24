"""
Financial evidence package and AI-finding validation (offline).

Packages are built from the real stored SEC evidence on an isolated copy of
the database. Findings are constructed directly: they stand in for model
output and are never shown to users.
"""
from decimal import Decimal

import pytest

from research_v2_support import isolated_runtime  # noqa: F401

TICKERS = ("AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA")


def package_for(ticker):
    from app.agents import financial_agent as fa
    from app.services import evidence_package as ep

    records, financials = fa.build_verified_financial_evidence(ticker, n_quarters=8)
    forecasts, quality, _unavailable = fa.build_forecasts(ticker, financials)
    package = ep.build_financial_package(
        ticker, records, financials, forecasts, quality, fa.quality_provenance(),
    )
    return package, records


def finding(**overrides):
    from app.agents.financial_agent import FinancialFinding

    base = {
        "title": "Quarterly revenue scale",
        "evidence_ids": [],
        "comparison_ids": [],
        "interpretation": "",
        "business_significance": "It shows the reported scale of the business in the period.",
        "limitations": "A single quarter cannot show what drove the result or whether it persists.",
    }
    base.update(overrides)
    if base["interpretation"] and not base["interpretation"].startswith("x "):
        # Neutral padding so each case reaches the minimum length and the
        # check under test is the one that decides.
        base["interpretation"] += FILLER
    return FinancialFinding(**base)


FILLER = (
    " Readers should compare the cited figure with other reported periods "
    "before drawing any conclusion about the business."
)


def latest(package, metric):
    rows = sorted(
        (r for r in package["records"] if r["metric"] == metric),
        key=lambda r: r["period_end"],
    )
    return rows


@pytest.fixture
def msft(isolated_runtime):
    return package_for("MSFT")


def validate(f, package):
    from app.agents.financial_agent import validate_financial_finding

    return validate_financial_finding(f, package)


def rejected(f, package, fragment):
    from app.services.narrative_validation import NarrativeRejected

    with pytest.raises(NarrativeRejected) as info:
        validate(f, package)
    assert fragment in str(info.value), str(info.value)


@pytest.mark.parametrize("ticker", TICKERS)
def test_package_is_bounded_validated_and_fits_budget(isolated_runtime, ticker):
    from app.agents import financial_agent as fa
    from app.services.ai_provider import (
        ai_runtime_settings, estimate_request_tokens, split_messages, text_format,
    )

    package, records = package_for(ticker)
    assert len(package["records"]) == 16
    assert {r["metric"] for r in package["records"]} == {"revenue", "operating_cash_flow"}

    for c in package["comparisons"]:
        current = package["items"][c["current_evidence_id"]]
        basis = package["items"][c["basis_evidence_id"]]
        expected = (
            (Decimal(current["value_usd"]) - Decimal(basis["value_usd"]))
            / abs(Decimal(basis["value_usd"])) * 100
        )
        assert abs(Decimal(c["change_pct"]) - expected) <= Decimal("0.05")
        assert current["metric"] == basis["metric"] == c["metric"]

    budget = ai_runtime_settings()["input_token_budget"]
    full = fa.financial_profiles()[0]
    messages = fa.build_financial_messages(ticker, package, full)
    instructions, items = split_messages(messages)
    estimate = estimate_request_tokens(
        instructions, items, text_format("x", fa.FinancialAnalysisResponse))
    # The full evidence profile fits the input budget (no reduced-profile fallback needed).
    assert estimate <= budget, estimate
    # Saved reports are never part of the evidence.
    assert "research_runs" not in messages[1]["content"]


def test_valid_finding_passes_and_observation_is_deterministic(msft):
    package, _ = msft
    rev = latest(package, "revenue")[-1]
    yoy_id = f"CMP_revenue_{rev['period_end'].replace('-', '')}_yoy"
    yoy = package["items"][yoy_id]
    f = finding(
        evidence_ids=[rev["evidence_id"]],
        comparison_ids=[yoy_id],
        interpretation=(
            f"Revenue for the quarter ending {rev['period_end']} was USD "
            f"{rev['value_billion_usd']} billion. Compared with the same quarter a year "
            f"earlier the InsightIQ comparison shows a change of "
            f"{yoy['change_pct'].lstrip('-')}%. Matching quarters limits seasonal effects. "
            "The figure describes reported scale only."
        ),
    )
    observation = validate(f, package)
    assert rev["evidence_id"] in observation and yoy_id in observation
    assert rev["value_billion_usd"] in observation


def test_unsupported_amount_rejected(msft):
    package, _ = msft
    rev = latest(package, "revenue")[-1]
    f = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=(
            "Revenue for the quarter reached USD 999.99 billion, an unusually large "
            "amount for the company compared with any earlier reporting period shown."
        ),
    )
    rejected(f, package, "number 999.99")


def test_unsupported_growth_rate_rejected(msft):
    package, _ = msft
    rev = latest(package, "revenue")[-1]
    yoy_id = f"CMP_revenue_{rev['period_end'].replace('-', '')}_yoy"
    f = finding(
        evidence_ids=[rev["evidence_id"]],
        comparison_ids=[yoy_id],
        interpretation=(
            "Revenue grew 47.3% against the prior-year quarter according to the comparison, "
            "which would represent a very significant change for a company of this size."
        ),
    )
    rejected(f, package, "percentage 47.3")


def test_percentage_without_cited_comparison_rejected(msft):
    package, _ = msft
    rev = latest(package, "revenue")[-1]
    yoy = package["items"][f"CMP_revenue_{rev['period_end'].replace('-', '')}_yoy"]
    f = finding(
        evidence_ids=[rev["evidence_id"]],
        comparison_ids=[],
        interpretation=(
            f"Revenue changed by {yoy['change_pct'].lstrip('-')}% against the prior-year "
            "quarter, which is notable when compared with earlier quarters of the series."
        ),
    )
    rejected(f, package, "percentage")


def test_trend_language_requires_three_contiguous_quarters(msft):
    package, _ = msft
    revs = latest(package, "revenue")
    one = finding(
        evidence_ids=[revs[-1]["evidence_id"]],
        interpretation=(
            "Revenue shows a sustained upward trend in the latest quarter, which suggests "
            "the business has been expanding steadily over a long period of time."
        ),
    )
    rejected(one, package, "trend language")

    three = finding(
        evidence_ids=[r["evidence_id"] for r in revs[-3:]],
        interpretation=(
            "Across three consecutive standalone quarters the cited revenue figures form a "
            "consistent sequence that can be read together. The comparison is limited to "
            "these reported quarters only."
        ),
    )
    validate(three, package)


def test_unknown_and_foreign_evidence_ids_rejected(msft):
    package, _ = msft
    rejected(finding(evidence_ids=["SEC_revenue_19990331"],
                     interpretation="x " * 30), package, "unknown evidence ID")
    rejected(finding(evidence_ids=["NEWS_AAPL_1"],
                     interpretation="x " * 30), package, "unknown evidence ID")
    rev = latest(package, "revenue")[-1]
    ocf_cmp = next(c for c in package["comparisons"] if c["metric"] == "operating_cash_flow")
    rejected(finding(evidence_ids=[rev["evidence_id"]],
                     comparison_ids=[ocf_cmp["comparison_id"]],
                     interpretation="x " * 30), package, "does not match the cited metric")


def test_wrong_period_or_fiscal_label_rejected(msft):
    package, _ = msft
    rev = latest(package, "revenue")[-1]
    label = rev["fiscal_label"]  # e.g. "FY2026 Q1" for MSFT (Jul-Sep 2025)
    fy = int(label[2:6])
    q = label[-1]
    good = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=(
            f"Revenue in FY{fy} Q{q} was USD {rev['value_billion_usd']} billion, according to "
            "the cited standalone quarter. The amount records reported scale for the period "
            "and should be read against matching quarters."
        ),
    )
    validate(good, package)

    wrong_fy = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=good.interpretation.replace(f"FY{fy}", f"FY{fy - 1}"),
    )
    rejected(wrong_fy, package, "not in cited evidence")

    wrong_date = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=(
            "Revenue for the period ending 2019-03-31 is cited here as the reference point "
            "for the latest quarter, which helps frame the company's reported scale."
        ),
    )
    rejected(wrong_date, package, "date not in cited evidence")


def test_cumulative_ytd_record_is_rejected_as_non_standalone():
    from app.services.financial_claims import FinancialClaimError, validated_evidence_map

    with pytest.raises(FinancialClaimError, match="standalone quarter"):
        validated_evidence_map([{
            "evidence_id": "SEC_revenue_20250630",
            "metric": "revenue",
            "period_start": "2025-01-01",
            "period_end": "2025-06-30",  # six-month year-to-date value
            "value_usd": "100",
            "sec_fact_ids": [1],
        }])


def test_causal_language_rejected_but_negated_limitation_allowed(msft):
    package, _ = msft
    rev = latest(package, "revenue")[-1]
    causal = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=(
            "Revenue was driven by cloud demand and strong pricing, which led to a higher "
            "result in the quarter than investors expected from the company."
        ),
    )
    rejected(causal, package, "causal")

    ok = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=(
            f"Revenue for the cited quarter was USD {rev['value_billion_usd']} billion. "
            "The value is a standalone quarter reported or derived from SEC filings. It "
            "shows scale for the period only."
        ),
        limitations="These figures cannot show what drove the change in revenue.",
    )
    validate(ok, package)

    bad_limitation = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=ok.interpretation,
        limitations="Growth was driven by advertising demand in the period.",
    )
    rejected(bad_limitation, package, "causal")


def test_evaluation_withholds_individual_findings_and_duplicates(msft):
    from app.agents.financial_agent import evaluate_financial_findings

    package, _ = msft
    rev = latest(package, "revenue")[-1]
    good = finding(
        evidence_ids=[rev["evidence_id"]],
        interpretation=(
            f"Revenue for the cited quarter was USD {rev['value_billion_usd']} billion. "
            "It is a standalone quarter value. It indicates reported scale for the period."
        ),
    )
    bad = finding(title="Invented", evidence_ids=["SEC_revenue_19990331"],
                  interpretation="x " * 30)
    accepted, withheld = evaluate_financial_findings([good, bad, good], package)
    assert [a["finding_id"] for a in accepted] == ["FIN_1"]
    assert {w["finding_id"] for w in withheld} == {"FIN_2", "FIN_3"}
    assert accepted[0]["validation"]["independently_verified"] is False
    assert "not independently verified" in accepted[0]["validation"]["label"]


def test_deterministic_fallback_claims_are_exact(msft):
    from app.agents.financial_agent import deterministic_claims

    package, records = msft
    claims = deterministic_claims(records)
    by_id = {r["evidence_id"]: r for r in records}
    assert {c["metric"] for c in claims} == {"revenue", "operating_cash_flow"}
    for c in claims:
        assert c["value_usd"] == by_id[c["evidence_id"]]["value_usd"]
