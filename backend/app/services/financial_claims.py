import json
import os
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict


class FinancialClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    metric: Literal["revenue", "operating_cash_flow"]
    period_start: str
    period_end: str
    value_usd: str


class FinancialClaimsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[FinancialClaim]


class FinancialClaimError(ValueError):
    pass


def exact_decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise FinancialClaimError(
            "Invalid financial amount."
        )

    if not result.is_finite():
        raise FinancialClaimError(
            "Non-finite financial amount."
        )

    return result


def validated_evidence_map(records):
    if not records:
        raise FinancialClaimError(
            "No source evidence provided."
        )

    evidence_map = {}

    for record in records:
        evidence_id = record["evidence_id"]

        if evidence_id in evidence_map:
            raise FinancialClaimError(
                "Duplicate SEC evidence identifier."
            )

        if record["metric"] not in {
            "revenue", "operating_cash_flow"
        }:
            raise FinancialClaimError(
                "Unsupported financial metric."
            )

        start = date.fromisoformat(
            record["period_start"]
        )

        end = date.fromisoformat(
            record["period_end"]
        )

        days = (end - start).days + 1

        if not 60 <= days <= 120:
            raise FinancialClaimError(
                "Evidence is not a standalone quarter."
            )

        exact_decimal(record["value_usd"])

        if not record.get("sec_fact_ids"):
            raise FinancialClaimError(
                "SEC provenance is missing."
            )

        evidence_map[evidence_id] = record

    return evidence_map


def validate_financial_claims(response, evidence_records):
    evidence_map = validated_evidence_map(
        evidence_records
    )

    if not 2 <= len(response.claims) <= 6:
        raise FinancialClaimError(
            "Expected two to six financial claims."
        )

    verified = []
    used_ids = set()

    for claim in response.claims:
        if claim.evidence_id not in evidence_map:
            raise FinancialClaimError(
                "Unknown SEC evidence identifier: "
                + claim.evidence_id
            )

        if claim.evidence_id in used_ids:
            raise FinancialClaimError(
                "Repeated evidence identifier."
            )

        used_ids.add(claim.evidence_id)

        source = evidence_map[claim.evidence_id]

        if claim.metric != source["metric"]:
            raise FinancialClaimError(
                "Financial metric does not match evidence."
            )

        if (
            claim.period_start != source["period_start"]
            or claim.period_end != source["period_end"]
        ):
            raise FinancialClaimError(
                "Reporting period does not match SEC evidence."
            )

        if (
            exact_decimal(claim.value_usd)
            != exact_decimal(source["value_usd"])
        ):
            raise FinancialClaimError(
                "Financial amount does not match SEC evidence."
            )

        verified.append({
            "evidence_id": claim.evidence_id,
            "metric": source["metric"],
            "period_start": source["period_start"],
            "period_end": source["period_end"],
            "value_usd": source["value_usd"],
            "sec_fact_ids": source["sec_fact_ids"],
            "normalization_method": source[
                "normalization_method"
            ],
        })

    metrics = {
        record["metric"] for record in verified
    }

    if metrics != {
        "revenue", "operating_cash_flow"
    }:
        raise FinancialClaimError(
            "Both financial metrics must be represented."
        )

    return verified


def render_verified_financial_claims(verified_claims):
    """
    Construct text from validated records, not
    unchecked generative prose.
    """
    lines = []

    for claim in verified_claims:
        metric_label = (
            "Revenue"
            if claim["metric"] == "revenue"
            else "Operating cash flow"
        )

        amount = exact_decimal(
            claim["value_usd"]
        )

        line = (
            f"{metric_label} for the standalone quarter "
            f"from {claim['period_start']} through "
            f"{claim['period_end']} was "
            f"USD {amount:,.0f}. "
            f"SEC evidence: {claim['evidence_id']}."
        )

        lines.append(line)

    return lines
