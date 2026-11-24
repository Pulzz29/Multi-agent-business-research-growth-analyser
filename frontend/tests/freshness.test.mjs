// Company data freshness helpers (node --test, clock fixed).

import assert from "node:assert/strict";
import test from "node:test";

import { createOpenGuard, lastUpdatedLabel, relativeAge } from "../src/lib/freshness.js";

const NOW = new Date("2026-11-23T14:00:00Z");

test("relative ages", () => {
  assert.equal(relativeAge("2026-11-23T13:59:40Z", NOW), "just now");
  assert.equal(relativeAge("2026-11-23T13:59:00Z", NOW), "1 minute ago");
  assert.equal(relativeAge("2026-11-23T11:00:00+00:00", NOW), "3 hours ago");
  assert.equal(relativeAge("2026-11-21T14:00:00Z", NOW), "2 days ago");
  assert.equal(relativeAge(null, NOW), null);
  assert.equal(relativeAge("not a date", NOW), null);
});

test("last updated label shows relative and exact time", () => {
  const label = lastUpdatedLabel("2026-11-23T08:00:00+00:00", NOW, "en-GB", "UTC");
  assert.equal(label, "Last updated: 6 hours ago (23 Nov 2026, 08:00)");
  assert.equal(lastUpdatedLabel(null, NOW), "Last updated: never");
});

test("one automatic request per opened company", () => {
  const guard = createOpenGuard();
  // React StrictMode runs the open effect twice for the same company.
  assert.equal(guard("IN:TCS.NS"), true);
  assert.equal(guard("IN:TCS.NS"), false);
  // Re-renders and tab switches for the same company do not re-trigger.
  assert.equal(guard("IN:TCS.NS"), false);
  // Opening another company does.
  assert.equal(guard("US:ORCL"), true);
});
