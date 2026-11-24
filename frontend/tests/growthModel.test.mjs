import { test } from "node:test";
import assert from "node:assert/strict";
import { growthRow, growthRows, pctText, trendView, yoyChartSeries } from "../src/lib/growthModel.js";

const metric = (overrides) => ({
  key: "revenue_yoy",
  label: "Revenue growth (YoY, quarter)",
  definition: "Revenue growth versus the same fiscal quarter one year earlier.",
  basis: "quarterly",
  value_pct: 12.345,
  status: "ok",
  reason: null,
  current: { period_end: "2026-09-30", value: 1 },
  base: { period_end: "2025-09-30", value: 1 },
  ...overrides,
});

test("ok values are signed percentages with the compared periods", () => {
  const row = growthRow(metric());
  assert.equal(row.valueText, "+12.3%");
  assert.equal(row.period, "2026-09-30 vs 2025-09-30");
  assert.equal(row.tone, "positive");
  assert.equal(row.reason, null);
});

test("not meaningful, insufficient and unavailable values show a label and the reason", () => {
  const loss = growthRow(metric({ status: "not_meaningful", value_pct: null, reason: "n/a: base is a loss" }));
  assert.equal(loss.valueText, "n/a");
  assert.equal(loss.reason, "n/a: base is a loss");
  assert.equal(loss.tone, "neutral");
  const short = growthRow(metric({ status: "insufficient_history", value_pct: null, reason: "Insufficient history (need 5 quarters, have 4).", current: null, base: null }));
  assert.equal(short.valueText, "Insufficient history");
  assert.match(short.reason, /need 5 quarters, have 4/);
});

test("non-finite numbers never render as NaN or Infinity", () => {
  for (const bad of [NaN, Infinity, -Infinity, undefined, null, "12"]) {
    assert.equal(pctText(bad), null);
    const row = growthRow(metric({ value_pct: bad }));
    assert.ok(!/NaN|Infinity/.test(row.valueText));
  }
});

test("rows follow the fixed metric order and skip absent metrics", () => {
  const rows = growthRows({ metrics: { revenue_cagr_3y: metric({ key: "revenue_cagr_3y" }), revenue_qoq: metric({ key: "revenue_qoq" }) } });
  assert.deepEqual(rows.map((r) => r.key), ["revenue_qoq", "revenue_cagr_3y"]);
  assert.deepEqual(growthRows(null), []);
});

test("trend view states the rule and the numbers behind the label", () => {
  const view = trendView({ label: "decelerating", basis: "quarterly", latest_pct: 11.7, previous_average_pct: 16.5, difference_pp: -4.8, rule: "Accelerating: ..." });
  assert.equal(view.label, "Decelerating");
  assert.equal(view.tone, "warning");
  assert.match(view.detail, /-4\.8 pp/);
  const none = trendView({ label: "insufficient_history", reason: "Insufficient history (need 3 year-over-year values, have 1).", rule: "r" });
  assert.equal(none.label, "Insufficient history");
  assert.match(none.detail, /have 1/);
});

test("growth chart series prefers quarterly YoY and drops not-meaningful points", () => {
  const growth = {
    yoy_series: {
      revenue: {
        quarterly: [
          { period_end: "2026-03-31", value_pct: 10, status: "ok", base_period_end: "2025-03-31" },
          { period_end: "2026-06-30", value_pct: null, status: "not_meaningful" },
          { period_end: "2026-09-30", value_pct: 12, status: "ok", base_period_end: "2025-09-30" },
        ],
        annual: [{ period_end: "2026-03-31", value_pct: 5, status: "ok" }],
      },
    },
  };
  const s = yoyChartSeries(growth);
  assert.equal(s.basis, "quarterly");
  assert.equal(s.points.length, 2);
  assert.equal(s.excluded, 1);
  const annualOnly = yoyChartSeries({ yoy_series: { revenue: { quarterly: [], annual: [{ period_end: "a", value_pct: 1, status: "ok" }, { period_end: "b", value_pct: 2, status: "ok" }] } } });
  assert.equal(annualOnly.basis, "annual");
  assert.equal(yoyChartSeries({}).basis, null);
});
