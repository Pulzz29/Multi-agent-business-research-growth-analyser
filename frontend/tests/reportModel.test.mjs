// Report view-model tests (node --test, no extra dependencies).
// The v2 object below is a hand-written TEST FIXTURE that mirrors the backend
// report schema; it is not real research output. v1 compatibility is checked
// against the real saved reports, read-only.

import assert from "node:assert/strict";
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { buildReportModel } from "../src/lib/reportModel.js";

const here = dirname(fileURLToPath(import.meta.url));
const reportsDir = join(here, "..", "..", "reports", "research_runs");

const V2_FIXTURE = {
  run_id: "00000000-0000-4000-8000-000000000001",
  ticker: "MSFT",
  status: "completed",
  report_schema_version: 2,
  generated_at_utc: "2026-11-23T12:00:00+00:00",
  agent_statuses: { financial: "completed", news: "completed", strategic: "completed" },
  source_freshness: {
    refresh_mode: "if_stale",
    refresh_failures: ["NEWSDATA"],
    sec: { evidence_state: "latest_available_confirmed", data_cutoff_period_end: "2026-09-30" },
    news: { evidence_state: "refresh_failed_using_cached", latest_cited_publication_date: "2026-11-22" },
    forecasts: { revenue: { cache_status: "hit", data_cutoff_period_end: "2026-09-30" } },
  },
  generation: {
    model: "openai/gpt-oss-120b",
    ai_analysis_status: { financial: "partial", news: "completed", strategic: "completed" },
    calls: [{ agent: "financial", call: "financial_analysis", status: "completed", prompt_tokens: 10, completion_tokens: 20 }],
  },
  financial: {
    verified_claims: {
      verified_claims: [
        { evidence_id: "SEC_revenue_20260930", metric: "revenue", period_start: "2026-07-01", period_end: "2026-09-30", value_usd: "77673000000", sec_fact_ids: [1] },
      ],
      verified_statements: ["Revenue ... SEC evidence: SEC_revenue_20260930."],
    },
    analysis: {
      status: "partial",
      claims_selection: "groq",
      findings: [
        {
          finding_id: "FIN_1",
          title: "Fixture finding title",
          verified_observation: "Fixture observation [SEC_revenue_20260930].",
          interpretation: "Fixture interpretation text.",
          business_significance: "Fixture significance.",
          limitations: "Fixture limitation.",
          evidence_ids: ["SEC_revenue_20260930"],
          comparison_ids: ["CMP_revenue_20260930_yoy"],
          validation: { status: "automated_checks_passed", label: "Fixture label", independently_verified: false },
        },
      ],
      withheld: [{ finding_id: "FIN_2", reason: "number 999 not in cited evidence" }],
    },
    evidence_package: {
      comparisons: [
        {
          comparison_id: "CMP_revenue_20260930_yoy",
          kind: "year_over_year",
          metric: "revenue",
          period_end: "2026-09-30",
          basis_period_end: "2025-09-30",
          change_pct: "18.4",
          basis_value_billion_usd: "65.59",
        },
      ],
      records: [],
      forecasts: [],
    },
    forecast_quality_provenance: { interval_quality_source: "reports/forecast_uncertainty_quality_review.json" },
  },
  news: {
    approved_primary_articles: [{ evidence_id: "NEWS_MSFT_1", title: "Headline", source_url: "https://example.com/a" }],
    analysis: {
      status: "completed",
      items: [
        {
          item_id: "NEWSINT_1",
          evidence_ids: ["NEWS_MSFT_1"],
          content_basis: "headline_only",
          sources: [{ evidence_id: "NEWS_MSFT_1", headline: "Headline", source_url: "https://example.com/a" }],
          reported_development: "Example Wire reported the headline.",
        },
      ],
      withheld: [],
    },
  },
  strategic: {
    status: "completed",
    analysis_status: "completed",
    research_priorities: [
      {
        evidence_id: "SEC_revenue_20260930",
        theme: "revenue_performance",
        priority: "high",
        monitoring_question: "Template question",
        source_metadata: { kind: "verified_sec_financial_fact" },
        analysis: {
          title: "AI priority title",
          classification: "potential_opportunity",
          classification_label: "Potential opportunity (AI interpretation)",
          interpretation: "Fixture interpretation.",
          indicators_to_monitor: ["Next filing"],
        },
      },
      {
        evidence_id: "NEWS_MSFT_1",
        theme: "company_developments",
        priority: "low",
        monitoring_question: "Template question 2",
        source_metadata: { kind: "company_news_headline" },
        analysis: null,
      },
    ],
  },
  limitations: ["Fixture limitation"],
};

test("v2 report fields reach the view model unchanged", () => {
  const model = buildReportModel(V2_FIXTURE, null);
  assert.equal(model.schemaVersion, 2);
  assert.equal(model.hasExtendedAnalysis, true);
  assert.equal(model.freshness.sec.evidence_state, "latest_available_confirmed");
  assert.deepEqual(model.freshness.refresh_failures, ["NEWSDATA"]);
  assert.equal(model.generation.calls.length, 1);

  const fin = model.financial.analysis;
  assert.equal(fin.status, "partial");
  assert.equal(fin.findings[0].interpretation, "Fixture interpretation text.");
  assert.equal(fin.withheld.length, 1);

  // Comparisons come from the report itself, not from current company data.
  const claim = model.financial.claims[0];
  assert.equal(claim.reportComparison.yoy.change_pct, "18.4");
  assert.equal(claim.comparison, null);

  assert.equal(model.news.analysis.items[0].content_basis, "headline_only");
  assert.equal(model.financial.qualityProvenance.interval_quality_source, "reports/forecast_uncertainty_quality_review.json");

  const [withAi, withoutAi] = model.strategic.priorities;
  assert.equal(withAi.aiTitle, "AI priority title");
  assert.equal(withAi.classificationLabel, "Potential opportunity (AI interpretation)");
  assert.equal(withoutAi.analysis, null);
  assert.equal(withoutAi.monitoring_question, "Template question 2");
});

test("missing or malformed v2 sections degrade safely", () => {
  const broken = { ...V2_FIXTURE, financial: { ...V2_FIXTURE.financial, analysis: "nonsense" }, generation: null };
  const model = buildReportModel(broken, null);
  assert.equal(model.financial.analysis, null);
  assert.equal(model.generation, null);
  assert.equal(buildReportModel(null, null), null);
});

const savedReports = existsSync(reportsDir) ? readdirSync(reportsDir).filter((f) => f.endsWith(".json")) : [];

test("every saved historical report still builds a view model", { skip: savedReports.length === 0 }, () => {
  for (const file of savedReports) {
    const raw = readFileSync(join(reportsDir, file), "utf8");
    const report = JSON.parse(raw);
    const model = buildReportModel(report, null);
    assert.ok(model, file);
    assert.equal(model.runId, report.run_id);
    if (!report.report_schema_version) {
      assert.equal(model.schemaVersion, 1, file);
      assert.equal(model.hasExtendedAnalysis, false, file);
      assert.equal(model.freshness, null, file);
      assert.equal(model.financial.analysis, null, file);
      assert.ok(model.financial.claims.length > 0, file);
    }
    // Reading never mutates the stored file.
    assert.equal(readFileSync(join(reportsDir, file), "utf8"), raw);
  }
});
