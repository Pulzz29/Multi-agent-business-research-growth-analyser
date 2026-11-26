// India/US parity helpers: money, fiscal labels, fundamentals, logos, overview market, routes.
import assert from "node:assert/strict";
import test from "node:test";

import { fiscalLabel, formatMoney, formatPerShare, moneyScale } from "../src/lib/format.js";
import { buildFundamentalsView, formatFundamental } from "../src/lib/fundamentalsModel.js";
import { initialsFor, logoCandidates, markColor, resetLogoCache, setLogoFetcher, subscribeLogo, validLogoUrl } from "../src/lib/logos.js";
import { forMarket, resolveMarket, sectorBars, storeMarket, readStoredMarket } from "../src/lib/marketOverviewModel.js";
import { buildHash, parseHash } from "../src/lib/router.js";

test("money uses each company's own currency and Indian units for INR", () => {
  assert.equal(formatMoney(8.9e12, "INR"), "₹8.90 L Cr");
  assert.equal(formatMoney(5.2094e11, "INR"), "₹52,094 Cr");
  assert.equal(formatMoney(-1.5e7, "INR"), "-₹1.5 Cr");
  assert.equal(formatMoney(3.1e12, "USD"), "$3.10T");
  assert.equal(formatMoney(94.93e9, "USD"), "$94.93B");
  assert.equal(formatMoney(null, "INR"), "—");
  assert.equal(formatPerShare(4123.5, "INR"), "₹4,123.50");
  assert.equal(moneyScale("INR").unit, "₹ crore");
  assert.equal(moneyScale("USD").divisor, 1e9);
});

test("Indian fiscal years run April-March and are named by the ending year", () => {
  assert.equal(fiscalLabel("2025-03-31", "IN"), "FY2025");
  assert.equal(fiscalLabel("2025-03-31", "IN", "quarterly"), "Q4 FY2025");
  assert.equal(fiscalLabel("2024-06-30", "IN", "quarterly"), "Q1 FY2025");
  assert.equal(fiscalLabel("2024-12-31", "IN", "quarterly"), "Q3 FY2025");
  assert.equal(fiscalLabel("2025-09-27", "US"), "FY ending 2025-09-27");
});

test("fundamentals keep unavailable values explicit and format by unit and currency", () => {
  const data = {
    period_end: "2025-03-31", statement_currency: "INR", quote_currency: "INR", available_count: 2,
    groups: [{ key: "valuation", label: "Valuation" }, { key: "returns", label: "Returns" }, { key: "growth", label: "Growth" }],
    metrics: [
      { key: "market_cap", group: "valuation", unit: "currency", value: 8.9e12, currency: "INR", available: true },
      { key: "ev_to_ebitda", group: "valuation", unit: "ratio", value: null, available: false, reason: "EBITDA is not reported." },
      { key: "roe_pct", group: "returns", unit: "pct", value: 48.7237, currency: "INR", available: true },
    ],
  };
  const view = buildFundamentalsView(data, "IN");
  assert.equal(view.period, "FY2025");
  assert.deepEqual(view.groups.map((g) => g.key), ["valuation", "returns"]); // empty groups hidden
  assert.equal(view.groups[0].metrics[0].display, "₹8.90 L Cr");
  assert.equal(view.groups[0].metrics[1].display, "Not available");
  assert.equal(view.groups[1].metrics[0].display, "48.72%");
  assert.equal(formatFundamental({ available: true, unit: "ratio", value: 0.1052 }), "0.11×");
  assert.equal(buildFundamentalsView(null).groups.length, 0);
});

test("logo candidates: only valid https URLs from the backend, in order, de-duplicated", () => {
  const meta = { candidates: [
    { url: "https://static2.finnhub.io/file/logo/AAPL.png" },
    { url: "javascript:alert(1)" },
    { url: "http://insecure.example.com/x.png" },
    { url: "https://www.apple.com/favicon.ico" },
    { url: "https://www.apple.com/favicon.ico" },
  ] };
  assert.deepEqual(logoCandidates(meta), ["https://static2.finnhub.io/file/logo/AAPL.png", "https://www.apple.com/favicon.ico"]);
  assert.equal(validLogoUrl("https://example.com/"), false);
  assert.deepEqual(logoCandidates(null), []);
});

test("initials fallback is professional and deterministic", () => {
  assert.equal(initialsFor("Tata Consultancy Services Limited", "TCS.NS"), "TC");
  assert.equal(initialsFor("Apple Inc.", "AAPL"), "AP");
  assert.equal(initialsFor("", "INFY.NS"), "IN");
  assert.equal(markColor("TCS.NS"), markColor("TCS.NS"));
  assert.notEqual(markColor("TCS.NS"), markColor("INFY.NS"));
});

test("logo metadata is fetched once per symbol in one batch and never shared across symbols", async () => {
  resetLogoCache();
  const calls = [];
  setLogoFetcher(async (symbols) => {
    calls.push(symbols);
    return { logos: { "TCS.NS": { candidates: [{ url: "https://www.tcs.com/favicon.ico" }] }, AAPL: { candidates: [] } } };
  });
  const got = {};
  await new Promise((resolve) => {
    let pending = 2;
    for (const s of ["TCS.NS", "AAPL"]) subscribeLogo(s, (m) => { got[s] = m; if (--pending === 0) resolve(); });
  });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].sort(), ["AAPL", "TCS.NS"]);
  assert.deepEqual(got["TCS.NS"].candidates, ["https://www.tcs.com/favicon.ico"]);
  assert.deepEqual(got.AAPL.candidates, []); // no borrowed logo
  subscribeLogo("TCS.NS", () => {}); // cached: no second request
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(calls.length, 1);
  setLogoFetcher(null);
});

test("overview market selection: route, then remembered value, then US; data isolated per market", () => {
  assert.equal(resolveMarket("IN", "US"), "IN");
  assert.equal(resolveMarket(undefined, "IN"), "IN");
  assert.equal(resolveMarket(undefined, "xx"), "US");
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  storeMarket("IN", storage);
  assert.equal(readStoredMarket(storage), "IN");
  const us = { market: "US", sectors: [{ sector_key: "technology", label: "Technology", universe_count: 8, loaded_count: 6 }] };
  assert.equal(forMarket(us, "IN"), null);
  assert.equal(forMarket(us, "US"), us);
  assert.deepEqual(sectorBars(us)[0].values.map((v) => v.value), [8, 6]);
});

test("routes: market company tabs, Indian report links and overview market", () => {
  assert.deepEqual(parseHash("#/markets/IN/companies/TCS.NS/fundamentals"), { page: "market-company", market: "IN", symbol: "TCS.NS", tab: "fundamentals" });
  assert.deepEqual(parseHash("#/markets/IN/companies/TCS.NS/reports/abc"), { page: "market-company", market: "IN", symbol: "TCS.NS", tab: "research", runId: "abc" });
  assert.equal(parseHash("#/markets/IN/companies/TCS.NS").tab, "overview");
  assert.equal(buildHash({ page: "market-company", market: "IN", symbol: "TCS.NS", tab: "forecasts" }), "#/markets/IN/companies/TCS.NS/forecasts");
  assert.equal(buildHash({ page: "market-company", market: "IN", symbol: "TCS.NS", tab: "research", runId: "r1" }), "#/markets/IN/companies/TCS.NS/reports/r1");
  assert.deepEqual(parseHash("#/overview?market=IN"), { page: "overview", market: "IN" });
  assert.equal(buildHash({ page: "overview", market: "IN" }), "#/overview?market=IN");
  assert.equal(parseHash("#/companies/AAPL/fundamentals").tab, "fundamentals");
});
