# META: audit of the standalone quarter ending 2025-12-31

**Date:** 2026-11-23. **Scope:** revenue and operating cash flow (OCF) for Meta Platforms, Inc. (CIK 0001326801), quarter 2025-10-01 to 2025-12-31.

**Method:** read-only. SQLite files were opened with `mode=ro`; backups were opened with `immutable=1`. Nothing was changed in the database, the reports or the forecast caches.

## Result

| Metric | Value shown | Derivation | SEC facts used |
|---|---|---|---|
| Revenue | **USD 59,893,000,000** ($59.89B) | FY2025 10-K total minus 9M 2025 YTD | 9886 − 9883 |
| Operating cash flow | **USD 36,214,000,000** ($36.21B) | FY2025 10-K total minus 9M 2025 YTD | 9186 − 9185 |

- **Company attribution:** correct. All facts come from Meta's companyfacts (CIK 0001326801).
- **Units:** USD, raw dollars.
- **Concepts:** revenue uses `RevenueFromContractWithCustomerExcludingAssessedTax`; OCF uses `NetCashProvidedByUsedInOperatingActivities`.
- **Why the value is derived:** the 10-K reports only the full year. No 3-month fact ending 2025-12-31 exists, so the quarter is necessarily FY minus 9M. The UI labels this method on every such quarter.
- **Same values everywhere:** the normalized series, the saved META report (`3871153c…`, claims `SEC_revenue_20251231` and `SEC_operating_cash_flow_20251231`) and the dashboard all show these values.

### Source facts

| Fact ID | Value (USD) | Period | Form | Filed | Accession |
|---|---|---|---|---|---|
| 9886 | 200,966,000,000 | 2025-01-01 → 2025-12-31 (revenue, FY) | 10-K | 2026-01-29 | 0001628280-26-003942 |
| 9883 | 141,073,000,000 | 2025-01-01 → 2025-09-30 (revenue, 9M) | 10-Q | 2026-10-22 | 0001628280-26-066773 |
| 9186 | 115,800,000,000 | 2025-01-01 → 2025-12-31 (OCF, FY) | 10-K | 2026-01-29 | 0001628280-26-003942 |
| 9185 | 79,586,000,000 | 2025-01-01 → 2025-09-30 (OCF, 9M) | 10-Q | 2026-10-22 | 0001628280-26-066773 |

## Flags (informational; no data was changed)

1. **The nine-month base comes from a later filing's comparative column.**
   - Facts 9883 and 9185 are the prior-year comparatives in the 2026 Q3 10-Q, filed 2026-10-22.
   - SEC tags them `fiscal_year=2026` because that tag follows the filing, not the period.
   - The original 2025 Q3 10-Q facts (9882, 9184) carry identical values, so the result is the same.
   - Normalization intentionally takes the latest filing for an identical period.
   - Anyone filtering SEC facts by `fiscal_year` should be aware of this tagging.
2. **Q3 2025 revenue differs by $1M depending on the source.**
   - The derived Q3 value (9M − H1) is USD 51,243M.
   - The directly reported 3-month fact (9885) is USD 51,242M.
   - InsightIQ uses the derived value, so the four quarters add up exactly to the 10-K annual total.
   - The difference is recorded as a `direct_ytd_discrepancy` diagnostic and shown on the Financials tab under "Data quality notes".
   - A similar ±$1M case exists for 2026-06-30.
3. **An older audit report disagrees with the series.**
   - `reports/financial_integrity_audit.json` flags FY2025 revenue reconciliation as "critical": quarter total 200,965M vs annual 200,966M.
   - That audit summed the *direct* Q3 fact (51,242M) rather than the derived one.
   - The derived series reconciles exactly. So does FY2025 OCF (115,800M).
   - `financial_evidence_reconciliation_final.json` (the review the forecast cache depends on) lists no critical issues for META.

## Recommendation

No correction is needed for the 2025-12-31 values. If you want the older audit to agree with the series, re-run it using derived quarters. Do not edit stored SEC facts.

Changing `financial_normalization.py` to prefer direct quarterly facts would change the forecast-cache fingerprint and force SARIMAX retraining, so that was deliberately not done.
