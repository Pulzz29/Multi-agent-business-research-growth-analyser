// Provider-company research report (Phase B) view-model tests.
// The fixture was produced by the backend from invented TEST data for an
// Indian company and a fake Groq client (see its _comment); not real data.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { buildReportModel } from "../src/lib/reportModel.js";

const here = dirname(fileURLToPath(import.meta.url));
const report = JSON.parse(readFileSync(join(here, "fixtures", "provider_report.test-fixture.json"), "utf8"));

test("provider report exposes currency, source and profile", () => {
  const model = buildReportModel(report, null);
  assert.equal(model.isProvider, true);
  assert.equal(model.currency, "INR");
  assert.equal(model.market, "IN");
  assert.equal(model.dataProvider, "YFINANCE");
  assert.equal(model.freshness.research_profile, "provider");
});

test("provider claims keep period type and provider provenance", () => {
  const model = buildReportModel(report, null);
  const types = Object.fromEntries(model.financial.claims.map((c) => [c.metric, c.period_type]));
  assert.deepEqual(types, { revenue: "quarterly", operating_cash_flow: "annual" });
  for (const claim of model.financial.claims) {
    assert.equal(claim.provider, "YFINANCE");
    assert.ok(claim.evidence_id.startsWith("YF_"));
    assert.equal(claim.comparison, null); // no SEC company data is used for provider reports
  }
});

test("unavailable forecasts are reported with reasons", () => {
  const model = buildReportModel(report, null);
  assert.equal(model.financial.forecasts.length, 0);
  assert.match(model.financial.forecastsUnavailable.revenue, /consecutive quarters/);
});
