// Hash routes for sector comparison, including the custom-selection query (node --test).

import assert from "node:assert/strict";
import test from "node:test";

import { buildHash, parseHash } from "../src/lib/router.js";

test("sector routes without a selection are unchanged", () => {
  assert.deepEqual(parseHash("#/markets/US/sectors/technology"), { page: "sectors", market: "US", sector: "technology" });
  assert.deepEqual(parseHash("#/markets/in/sectors"), { page: "sectors", market: "IN", sector: null });
  assert.equal(buildHash({ page: "sectors", market: "US", sector: "technology" }), "#/markets/US/sectors/technology");
});

test("custom selection round-trips through the URL", () => {
  const route = parseHash("#/markets/IN/sectors/it_services?companies=tcs.ns,INFY.NS,tcs.ns,<bad>");
  assert.deepEqual(route, { page: "sectors", market: "IN", sector: "it_services", companies: ["TCS.NS", "INFY.NS"] });
  assert.equal(buildHash(route), "#/markets/IN/sectors/it_services?companies=TCS.NS,INFY.NS");
  // An empty or invalid list means the normal sector view.
  assert.deepEqual(parseHash("#/markets/US/sectors/technology?companies="), { page: "sectors", market: "US", sector: "technology" });
  assert.equal(buildHash({ page: "sectors", market: "US", sector: "technology", companies: [] }), "#/markets/US/sectors/technology");
});
