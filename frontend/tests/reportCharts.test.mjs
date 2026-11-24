import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import {
  basisOptions,
  formatMoney,
  growthChart,
  marginChart,
  priceView,
  ratioRows,
  revenueNetIncomeChart,
} from "../src/lib/reportCharts.js";

const DIR = new URL("../../reports/research_runs/insights/", import.meta.url);
const load = (run) => {
  const url = new URL(`${run}.json`, DIR);
  return existsSync(url) ? JSON.parse(readFileSync(url, "utf-8")) : null;
};
const MSFT = load("5a25fd13-a894-4b58-ba69-ed8c4422fbe8");
const ITC = load("2c54494c-3744-47df-9508-deb2c52085f4");

test("real SEC dataset (MSFT) gives complete charts in USD billions", { skip: !MSFT && "dataset not generated" }, () => {
  assert.deepEqual(basisOptions(MSFT.revenue_net_income), ["quarterly", "annual"]);
  const rev = revenueNetIncomeChart(MSFT, "quarterly");
  assert.equal(rev.empty, false);
  assert.equal(rev.yTitle, "USD billions");
  assert.ok(rev.data.every((d) => d.values.length === 2));
  assert.ok(rev.data.at(-1).values[0].value > 1 && rev.data.at(-1).values[0].value < 1000, "scaled to billions");
  const margins = marginChart(MSFT, "quarterly");
  assert.deepEqual(margins.series.map((s) => s.id), ["gross", "operating", "net"]);
  const growth = growthChart(MSFT);
  assert.equal(growth.basis, "quarterly");
  assert.equal(ratioRows(MSFT).empty, false);
});

test("real provider dataset (ITC.NS) is INR, has no gross margin and falls back to annual growth", { skip: !ITC && "dataset not generated" }, () => {
  const rev = revenueNetIncomeChart(ITC, "quarterly");
  assert.equal(rev.yTitle, "INR billions");
  const margins = marginChart(ITC, "quarterly");
  assert.ok(!margins.series.some((s) => s.id === "gross"));
  assert.match(margins.notes[0], /Gross profit is not stored/);
  assert.equal(growthChart(ITC).basis, "annual");
  assert.match(formatMoney(201212400000, "INR"), /^₹201B$/);
});

test("missing or short data gives an explicit empty state, never a broken chart", () => {
  const empty = { currency: "USD", revenue_net_income: { quarterly: [{ period_end: "2026-03-31", revenue: 1e9, net_income: 1e8 }], annual: [] }, margins: {}, growth: null, ratios: { rows: [] }, price: {} };
  const rev = revenueNetIncomeChart(empty, "quarterly");
  assert.equal(rev.empty, true);
  assert.equal(rev.title, "Not enough data to chart");
  assert.match(rev.reason, /have 1/);
  assert.equal(marginChart(empty, "quarterly").empty, true);
  assert.equal(growthChart(empty).empty, true);
  assert.equal(ratioRows(empty).empty, true);
  assert.deepEqual(basisOptions(empty.revenue_net_income), []);
  assert.equal(revenueNetIncomeChart(null, "annual").empty, true);
});

test("growth chart explains insufficient history from the growth analysis", () => {
  const data = { growth: { metrics: { revenue_yoy: { reason: "Insufficient history (need 5 quarters, have 4)." } }, yoy_series: { revenue: { quarterly: [], annual: [] } } } };
  const view = growthChart(data);
  assert.equal(view.empty, true);
  assert.match(view.reason, /need 5 quarters, have 4/);
});

test("price is an empty state with the latest stored quote", () => {
  const view = priceView({ price: { available: false, reason: "Price history is not stored.", latest_quote: { price: "499.31", currency: "USD", provider: "FMP", fetched_at: "2026-11-23T17:14:29Z" } } });
  assert.equal(view.available, false);
  assert.equal(view.latest.text, "$499.31");
  assert.equal(priceView({}).latest, null);
});

test("money formatting is signed and never NaN", () => {
  assert.equal(formatMoney(-5e8, "USD"), "-$0.50B");
  assert.equal(formatMoney(NaN, "USD"), "Unavailable");
  assert.equal(formatMoney(undefined, "USD"), "Unavailable");
});

test("ratio rows scale bars per ratio and label missing sector data", () => {
  const view = ratioRows({ ratios: { sector: "Tech", rows: [
    { key: "roe_pct", label: "ROE", unit: "%", company: 30, sector_median: 60, sector_companies: 5 },
    { key: "pe_ratio", label: "P/E", unit: "x", company: 20, sector_median: null, sector_companies: 0 },
  ] } });
  assert.equal(view.rows[0].companyWidth, 50);
  assert.equal(view.rows[0].medianWidth, 100);
  assert.equal(view.rows[1].medianText, "No sector data");
  assert.equal(view.rows[1].companyText, "20.00×");
});
