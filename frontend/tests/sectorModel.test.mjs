// Sector comparison view-model tests (node --test).
// The fixture was produced by the backend from invented TEST data and a fake
// Groq client (see its _comment field); it is not real market data.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { buildComparisonModel, formatMetric } from "../src/lib/sectorModel.js";

const here = dirname(fileURLToPath(import.meta.url));
const snapshot = JSON.parse(readFileSync(join(here, "fixtures", "sector_snapshot.test-fixture.json"), "utf8"));

test("comparison snapshot maps to a view model without recomputing ranks", () => {
  const model = buildComparisonModel(snapshot);
  assert.equal(model.id, snapshot.comparison_id);
  assert.equal(model.status, "ranked");
  const backendRanks = snapshot.ranking.companies.map((c) => [c.symbol, c.rank, c.score]);
  assert.deepEqual(model.rows.map((r) => [r.symbol, r.rank, r.score]), backendRanks);
  assert.equal(model.rankedCount, snapshot.ranking.companies.filter((c) => c.ranked).length);
  assert.ok(model.metrics.length >= 5);
});

test("charts receive only ranked companies and normalised scores", () => {
  const model = buildComparisonModel(snapshot);
  assert.equal(model.scoreBars.length, model.rankedCount);
  for (const bar of model.scoreBars) assert.ok(bar.values[0].value >= 0 && bar.values[0].value <= 100);
  assert.equal(model.radar.axes.length, model.metrics.length);
  for (const s of model.radar.series) assert.equal(s.values.length, model.metrics.length);
  assert.ok(model.radar.series.length <= 4);
});

test("unranked companies keep their reason and explanation text", () => {
  const model = buildComparisonModel(snapshot);
  const unranked = model.rows.filter((r) => !r.ranked);
  assert.ok(unranked.length >= 1);
  for (const row of unranked) {
    assert.ok(row.notRankedReason);
    assert.equal(row.rank, null);
  }
  const ranked = model.rows.find((r) => r.ranked);
  assert.ok(ranked.why.length > 10);
  assert.ok(["ai", "deterministic"].includes(ranked.textSource));
});

test("formatting helpers and malformed input", () => {
  assert.equal(formatMetric(12.345, "%", "USD"), "12.3%");
  assert.equal(formatMetric(0.5, "x", "USD"), "0.50×");
  assert.equal(formatMetric(3.5e12, "currency", "USD"), "$3.50T");
  assert.equal(formatMetric(2.4e11, "currency", "INR"), "₹240.0B");
  assert.equal(formatMetric(null, "%", "USD"), "—");
  assert.equal(buildComparisonModel(null), null);
  assert.equal(buildComparisonModel({}), null);
});
