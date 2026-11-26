import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { awaitingReport, latestSuggestions, pendingJob, reportLabel, safeExternalUrl } from "../src/lib/chatModel.js";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");

test("only http(s) source links are rendered", () => {
  assert.equal(safeExternalUrl("https://news.example.com/a"), "https://news.example.com/a");
  assert.equal(safeExternalUrl("javascript:alert(1)"), null);
  assert.equal(safeExternalUrl("data:text/html,<script>"), null);
  assert.equal(safeExternalUrl("https://user:pw@evil.example/"), null);
  assert.equal(safeExternalUrl("not a url"), null);
});

test("suggestions, job progress and report labels", () => {
  const messages = [
    { role: "assistant", payload: { suggestions: ["A", "A", "B", 5] } },
    { role: "user", payload: {} },
  ];
  assert.deepEqual(latestSuggestions(messages), ["A", "B"]);
  assert.equal(pendingJob([{ payload: { job: { run_id: "r", status: "running" } } }]).run_id, "r");
  assert.equal(pendingJob([{ payload: { job: { run_id: "r", status: "completed" } } }]), null);
  assert.equal(reportLabel({ date: "2026-11-20T10:00:00+00:00" }), "View report (2026-11-20)");
});

test("chat widget is compact, text-only and on the Overview page", () => {
  const widget = read("../src/components/ChatWidget.jsx");
  const css = read("../src/styles.css");
  assert.ok(!widget.includes("dangerouslySetInnerHTML"));
  assert.match(widget, /Hello|chatMessages|newChat/);
  assert.match(css, /\.chat-window \{[^}]*width: min\(380px, calc\(100vw - 24px\)\);[^}]*height: min\(500px, calc\(100vh - 120px\)\);/s);
  assert.ok(!/\.chat-window \{[^}]*inset: 0/s.test(css));
  const overview = read("../src/pages/OverviewPage.jsx");
  assert.equal((overview.match(/<ChatWidget /g) || []).length, 2); // US and India views
});

test("Overview removes count tiles and company summary cards; sidebar collapses", () => {
  const market = read("../src/components/MarketOverview.jsx");
  for (const label of ["Companies in universe", "Companies with data", "Research runs", "Sector breakdown"]) {
    assert.ok(!market.includes(label), label);
  }
  assert.match(market, /top-company-list/);
  const overview = read("../src/pages/OverviewPage.jsx");
  assert.ok(!overview.includes("CompanyCard") && !overview.includes("companies-heading"));
  const layout = read("../src/components/Layout.jsx");
  assert.match(layout, /aria-expanded=\{!collapsed\}/);
  assert.match(layout, /title: collapsed \? label : undefined/);
  const app = read("../src/App.jsx");
  assert.match(app, /insightiq-sidebar/);
});

test("on-demand report generation is polled until the result is posted", () => {
  const generating = { role: "assistant", intent: "generating", ticker: "INFY.NS", payload: { generating: { ticker: "INFY.NS" } } };
  assert.equal(awaitingReport([generating]), "INFY.NS");
  assert.equal(awaitingReport([generating, { role: "user" }]), "INFY.NS");
  assert.equal(awaitingReport([generating, { role: "assistant", intent: "report_ready" }]), null);
  assert.equal(awaitingReport([generating, { role: "assistant", intent: "report_failed" }]), null);
  assert.equal(awaitingReport([{ role: "assistant", intent: "answer" }]), null);
});

test("compact observability strip uses measured values and marks missing ones", async () => {
  const { observabilitySummary } = await import("../src/lib/operationsModel.js");
  const empty = observabilitySummary({ runs: { runs: 0, by_status: {} }, tokens: { totals: { calls_with_usage: 0 } } });
  assert.equal(empty.tokens, null);
  assert.equal(empty.guardrail, null);
  assert.equal(empty.successRate, null);
  const data = observabilitySummary({
    runs: { runs: 10, success_rate: 0.8, by_status: { failed: 2 }, duration_seconds: { p50: 90, p95: 200 } },
    tokens: { totals: { calls_with_usage: 3, total_tokens: 1234 } },
    governance: { events: 5, pass_rate: 0.6, by_outcome: { success: 3, rejected: 1, blocked: 1, failed: 0 } },
  });
  assert.deepEqual(data, { runs: 10, successRate: 0.8, failed: 2, p50: 90, p95: 200, tokens: 1234,
    guardrail: { passRate: 0.6, rejected: 1, blocked: 1, checked: 5 } });
  const panel = read("../src/components/OperationsPanel.jsx");
  assert.match(panel, /obs-strip/);
  assert.ok(!/stack_trace|prompt|password/i.test(panel));
  const api = read("../src/lib/api.js");
  assert.match(api, /REQUEST_TIMEOUT_MS/);
  assert.match(read("../src/App.jsx"), /<ErrorBoundary/);
  assert.match(read("../src/components/ChatWidget.jsx"), /Download PDF/);
});
