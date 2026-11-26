import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { csrfHeaders, readCookie, runReportRoute, validateLogin } from "../src/lib/auth.js";

test("login validation", () => {
  assert.deepEqual(validateLogin("user@insightiq.com", "x"), {});
  assert.equal(validateLogin("", "pw").email, "Enter your email address.");
  assert.equal(validateLogin("not-an-email", "pw").email, "Enter a valid email address.");
  assert.equal(validateLogin("a@b.co", "").password, "Enter your password.");
  assert.equal(validateLogin(" a@b.co ", "pw").email, undefined);
});

test("CSRF header only on state-changing requests", () => {
  const cookies = "theme=dark; insightiq_csrf=abc%2D123; other=1";
  assert.equal(readCookie(cookies, "insightiq_csrf"), "abc-123");
  assert.deepEqual(csrfHeaders("GET", cookies), {});
  assert.deepEqual(csrfHeaders("post", cookies), { "X-CSRF-Token": "abc-123" });
  assert.deepEqual(csrfHeaders("DELETE", ""), {});
});

test("View report routes open the exact run", () => {
  const sec = (t) => t === "MSFT";
  assert.equal(runReportRoute({ ticker: "MSFT", run_id: "r1", report_available: false }, sec), null);
  assert.deepEqual(runReportRoute({ ticker: "MSFT", run_id: "r1", report_available: true }, sec), {
    page: "company", ticker: "MSFT", tab: "research", runId: "r1",
  });
  assert.deepEqual(runReportRoute({ ticker: "INFY.NS", run_id: "r2", report_available: true }, sec), {
    page: "market-company", market: "IN", symbol: "INFY.NS", tab: "research", runId: "r2",
  });
  assert.equal(runReportRoute({ ticker: "TSLA", run_id: "r3", report_available: true }, sec).market, "US");
});

test("no API key entry or browser-stored credentials in the UI", () => {
  const app = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
  const api = readFileSync(new URL("../src/lib/api.js", import.meta.url), "utf8");
  assert.ok(!/API_ACCESS_KEY|insightiq-token|sessionStorage/.test(app));
  assert.ok(!/Authorization|Bearer/.test(api));
  assert.match(api, /credentials: "same-origin"/);
});
