import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { formatReportTime } from "../src/lib/format.js";
import { filterSavedReports } from "../src/lib/savedReports.js";
import { priceView } from "../src/lib/reportCharts.js";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

test("report timestamps match the PDF format and are never invented", () => {
  assert.equal(formatReportTime("2026-10-01T09:00:00+00:00"), "01 Oct 2026 09:00 UTC");
  assert.equal(formatReportTime("2026-10-01T14:30:00+05:30"), "01 Oct 2026 09:00 UTC");
  assert.equal(formatReportTime("2026-10-01T09:00:00"), "01 Oct 2026 09:00 UTC");
  assert.equal(formatReportTime(null), "Not recorded");
  assert.equal(formatReportTime("2026-10-01"), "Not recorded");
});

test("saved reports filter by market and company, keeping each historical report", () => {
  const rows = [
    { run_id: "a2", ticker: "AAPL", market: "US" },
    { run_id: "a1", ticker: "AAPL", market: "US" },
    { run_id: "r1", ticker: "RELIANCE.NS", market: "IN" },
  ];
  assert.deepEqual(filterSavedReports(rows).map((r) => r.run_id), ["a2", "a1", "r1"]);
  assert.deepEqual(filterSavedReports(rows, "IN").map((r) => r.run_id), ["r1"]);
  assert.deepEqual(filterSavedReports(rows, "US", "AAPL").map((r) => r.run_id), ["a2", "a1"]);
});

test("price fallback shows only a genuine stored price, labelled as not live", () => {
  const old = priceView({ price: { available: false, reason: "No history.", latest_quote: { price: "182.5", currency: "USD", provider: "FINNHUB", fetched_at: "2026-01-01T00:00:00Z" } } });
  assert.equal(old.latest.currency, "USD");
  assert.equal(old.latest.stale, true);
  assert.equal(priceView({ price: { latest_quote: { price: "0", currency: "USD" } } }).latest, null);
  assert.equal(priceView({ price: {} }).latest, null);
  const charts = read("../src/components/ReportCharts.jsx");
  assert.ok(!charts.includes("Not enough data to chart"));
  assert.match(charts, /Last recorded price/);
  assert.match(charts, /not a live price/);
  assert.match(charts, /Price data unavailable/);
});

test("overview, activity, sidebar and executive summary UI changes", () => {
  const overview = read("../src/pages/OverviewPage.jsx");
  assert.ok(overview.includes("Company Intelligence") && !overview.includes("Company intelligence"));
  for (const label of ["SEC-validated companies", "Saved research reports", "SEC financial facts", "Stored news articles"]) {
    assert.ok(!overview.includes(label), label);
  }
  const activity = read("../src/pages/ActivityPage.jsx");
  for (const label of ['label="Research runs"', '"Completed reports"', '"Failed runs"', '"Traced spans sampled"']) {
    assert.ok(!activity.includes(label), label);
  }
  assert.match(activity, /Previous report/);
  const layout = read("../src/components/Layout.jsx");
  assert.match(layout, /Saved Reports/);
  assert.match(layout, /topCompanies/);
  assert.ok(!layout.includes("COMPANIES.map"));
  const app = read("../src/App.jsx");
  assert.match(app, /data\.market !== sidebarMarket/); // stale responses for another market are ignored
  assert.match(app, /slice\(0, 10\)/);
  const viewer = read("../src/pages/ReportViewer.jsx");
  assert.ok(!viewer.includes("Sections withheld by validation"));
  assert.match(viewer, /Evidence limitation/);
  assert.match(viewer, /<ChatWidget[\s\S]*inline[\s\S]*scope=\{\{ ticker: model\.ticker, run_id: model\.runId/);
  const widget = read("../src/components/ChatWidget.jsx");
  assert.ok(!widget.includes("dangerouslySetInnerHTML"));
  assert.match(widget, /scope_run_id/);
});
