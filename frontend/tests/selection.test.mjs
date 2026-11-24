// Custom comparison selection helpers (node --test).

import assert from "node:assert/strict";
import test from "node:test";

import { filterCompanies, initialSelection, parseSymbols, selectAll, selectionStatus, toggle } from "../src/lib/selection.js";

const MEMBERS = [
  { symbol: "AAPL", name: "Apple Inc." },
  { symbol: "MSFT", name: "Microsoft Corporation" },
  { symbol: "NVDA", name: "NVIDIA Corporation" },
  { symbol: "ORCL", name: "Oracle Corporation" },
  { symbol: "ADBE", name: "Adobe Inc." },
  { symbol: "CSCO", name: "Cisco Systems, Inc." },
  { symbol: "INTC", name: "Intel Corporation" },
];
const LIMITS = { min: 2, max: 6 };

test("filter by name or symbol, case-insensitive", () => {
  assert.deepEqual(filterCompanies(MEMBERS, "corp").map((c) => c.symbol), ["MSFT", "NVDA", "ORCL", "INTC"]);
  assert.deepEqual(filterCompanies(MEMBERS, " adbe ").map((c) => c.symbol), ["ADBE"]);
  assert.equal(filterCompanies(MEMBERS, "").length, MEMBERS.length);
  assert.deepEqual(filterCompanies(MEMBERS, "zzz"), []);
  assert.deepEqual(filterCompanies(null, "a"), []);
});

test("toggle adds and removes without mutating", () => {
  const start = ["AAPL"];
  const added = toggle(start, "MSFT");
  assert.deepEqual(added, ["AAPL", "MSFT"]);
  assert.deepEqual(start, ["AAPL"]);
  assert.deepEqual(toggle(added, "AAPL"), ["MSFT"]);
});

test("select all is capped at the maximum and keeps existing picks", () => {
  assert.deepEqual(selectAll(MEMBERS, [], 6), ["AAPL", "MSFT", "NVDA", "ORCL", "ADBE", "CSCO"]);
  assert.deepEqual(selectAll(MEMBERS, ["INTC"], 3), ["INTC", "AAPL", "MSFT"]);
  assert.deepEqual(selectAll(MEMBERS.slice(0, 2), ["AAPL"], 6), ["AAPL", "MSFT"]);
  assert.deepEqual(selectAll(MEMBERS, ["A", "B"], 2), ["A", "B"]);
});

test("compare is disabled below the minimum and above the maximum", () => {
  const none = selectionStatus([], LIMITS);
  assert.equal(none.valid, false);
  assert.equal(none.label, "0 selected (min 2, max 6)");
  assert.equal(none.reason, "Select at least 2 companies (2 more).");
  assert.equal(selectionStatus(["AAPL"], LIMITS).valid, false);
  assert.equal(selectionStatus(["AAPL", "MSFT"], LIMITS).valid, true);
  assert.equal(selectionStatus(MEMBERS.slice(0, 6).map((c) => c.symbol), LIMITS).valid, true);
  const seven = selectionStatus(MEMBERS.map((c) => c.symbol), LIMITS);
  assert.equal(seven.valid, false);
  assert.equal(seven.reason, "Select at most 6 companies (remove 1).");
  assert.equal(selectionStatus(["AAPL", "MSFT"], LIMITS).reason, "");
});

test("URL symbols are cleaned and checked against the sector", () => {
  assert.deepEqual(parseSymbols("aapl, MSFT,,msft,<x>, tcs.ns"), ["AAPL", "MSFT", "TCS.NS"]);
  assert.deepEqual(parseSymbols(null), []);
  const { selected, ignored } = initialSelection(["MSFT", "JPM", "AAPL", "MSFT"], MEMBERS);
  assert.deepEqual(selected, ["MSFT", "AAPL"]);
  assert.deepEqual(ignored, ["JPM"]);
});
