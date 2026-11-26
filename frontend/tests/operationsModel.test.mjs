// Operations panel helpers (node --test).
import assert from "node:assert/strict";
import test from "node:test";

import { latencyRows, ms, tokenRows } from "../src/lib/operationsModel.js";

test("durations are formatted without inventing values", () => {
  assert.equal(ms(null), "—");
  assert.equal(ms(undefined), "—");
  assert.equal(ms(3.456), "3.46 ms");
  assert.equal(ms(250), "250 ms");
  assert.equal(ms(1530), "1.53 s");
});

test("latency rows follow the pipeline order and keep measured numbers", () => {
  const rows = latencyRows({
    by_kind: [
      { kind: "pdf", count: 1, p50_ms: 5, p95_ms: 5 },
      { kind: "api", count: 3, p50_ms: 1, p95_ms: 9 },
      { kind: "custom", count: 1, p50_ms: 2, p95_ms: 2 },
    ],
  });
  assert.deepEqual(rows.map((r) => r.kind), ["api", "pdf", "custom"]);
  assert.equal(rows[0].label, "API requests");
  assert.equal(rows[0].p95_ms, 9);
  assert.deepEqual(latencyRows(null), []);
});

test("token rows label providers from the data", () => {
  const rows = tokenRows({ models: [{ provider: "azure_openai", model: "m-1", calls: 2, sources: ["saved_report"] }] });
  assert.equal(rows[0].label, "Azure OpenAI · m-1");
  assert.deepEqual(tokenRows(undefined), []);
});
